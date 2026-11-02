#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A exposição de uma configuração à carga de escrita, e o que se sabe do custo.

A bancada só reexecuta leitura. O benefício de um índice é medido; o custo de
mantê-lo, não. No YCSB isso pesa: 186.221 escritas em ``usertable`` no
corpus, e o GIN de trigrama de 263 MB entrou pela leitura sem que a escrita
fosse confrontada.

Este módulo separa três coisas que não podem virar uma só:

1. **Exposição estática** (o que o SQL permite deduzir). O parser (pglast, o
   do próprio Postgres) diz o tipo da escrita, a tabela e, no UPDATE, as
   colunas atribuídas. Cruzado com as colunas de que cada índice depende
   (chave, expressão, INCLUDE e predicado), isso classifica cada escrita
   como:

     certa      INSERT: toda linha nova entra em todo índice da tabela (menos
                os parciais cujo predicado a exclua, o que não se sabe aqui)
     provável   UPDATE que atribui coluna de que algum índice depende: não
                pode ser HOT se o valor mudar, e aí mantém todos os índices
     possível   UPDATE que não toca coluna indexada: é elegível a HOT, e só
                mantém os índices se não couber na mesma página (fillfactor,
                espaço livre). HOT é comportamento observado, não deduzível
                do SQL

   DELETE não insere entrada de índice; as entradas mortas são removidas
   depois, pelo VACUUM (uma passada por índice) e pela limpeza oportunista
   das varreduras. Esse custo existe e cresce com o número de índices, mas
   não é estimável daqui: é contado à parte e dito.

2. **Comportamento observado**: ``n_tup_hot_upd / n_tup_upd`` da tabela,
   lido de ``pg_stat_user_tables`` (gravado no ambiente de cada sessão). É a
   fração de UPDATE que foi HOT na carga observada, com os índices de então.
   Serve para situar a exposição "possível" entre os extremos, e é rotulada
   como observação, não como previsão.

3. **Custo medido**: só o que vier de ``monitoring.custo_manutencao``,
   gravado por um experimento A/B de escrita (a mesma carga com e sem o
   índice: latência, throughput, WAL). Sem isso, a resposta honesta é o
   ponto de equilíbrio: quanto cada escrita pode custar antes de o índice
   deixar de se pagar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

# Palavras que aparecem na definição de um índice e não são coluna.
_NAO_COLUNA = {
    "asc", "desc", "nulls", "first", "last", "collate", "using", "include",
    "where", "and", "or", "not", "is", "null", "true", "false", "on", "only",
    "btree", "hash", "gin", "gist", "brin", "spgist", "text", "varchar",
    "integer", "int", "bigint", "date", "numeric", "like", "ilike", "in",
    "between", "lower", "upper", "left", "right", "substr", "substring",
    "coalesce", "md5", "length", "cast", "as", "with", "fillfactor",
}
_RE_IDENT = re.compile(r'"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b')
_RE_LITERAL = re.compile(r"'(?:[^']|'')*'")


@dataclass(frozen=True)
class Escrita:
    """Um comando de escrita do corpus, já classificado."""
    tipo: str                         # 'INSERT' | 'UPDATE' | 'DELETE' | 'MERGE'
    tabela: str
    # UPDATE: as colunas atribuídas (SET e ON CONFLICT DO UPDATE). None:
    # não deu para saber, e conta como se atribuísse todas.
    colunas: Optional[FrozenSet[str]]
    execucoes: float
    upsert: bool = False


def classificar(texto: str, execucoes: float) -> Optional[Escrita]:
    """O comando pelo parser do Postgres. None se não for escrita."""
    try:
        from pglast import parse_sql
        stmt = parse_sql(texto)[0].stmt
    except Exception:                           # noqa: BLE001
        return _classificar_regex(texto, execucoes)
    nome = type(stmt).__name__
    if nome not in ("InsertStmt", "UpdateStmt", "DeleteStmt", "MergeStmt"):
        return None
    rel = getattr(stmt, "relation", None)
    tabela = (getattr(rel, "relname", None) or "").lower()
    if not tabela:
        return None
    if nome == "UpdateStmt":
        cols = frozenset((t.name or "").lower() for t in (stmt.targetList or ())
                         if getattr(t, "name", None))
        return Escrita("UPDATE", tabela, cols or None, execucoes)
    if nome == "InsertStmt":
        conflito = getattr(stmt, "onConflictClause", None)
        alvo = getattr(conflito, "targetList", None) if conflito else None
        if alvo:
            cols = frozenset((t.name or "").lower() for t in alvo
                             if getattr(t, "name", None))
            return Escrita("INSERT", tabela, cols or None, execucoes, upsert=True)
        return Escrita("INSERT", tabela, None, execucoes)
    if nome == "MergeStmt":
        return Escrita("MERGE", tabela, None, execucoes)
    return Escrita("DELETE", tabela, None, execucoes)


def _classificar_regex(texto: str, execucoes: float) -> Optional[Escrita]:
    m = re.match(r"""^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+
                     (?:ONLY\s+)?(?:(?:"[^"]+"|\w+)\.)?("[^"]+"|\w+)""",
                 texto or "", re.IGNORECASE | re.VERBOSE)
    if not m:
        return None
    tipo = m.group(1).split()[0].upper()
    tabela = m.group(2).strip('"').lower()
    # Sem parser, as colunas do UPDATE ficam desconhecidas: conta como se
    # atribuísse todas, que é o lado conservador da exposição.
    return Escrita(tipo, tabela, None, execucoes)


def dependencias_do_indice(ddl: str,
                           colunas_da_tabela: Optional[Iterable[str]] = None
                           ) -> Tuple[Optional[str], FrozenSet[str]]:
    """(tabela, colunas de que o índice depende): chave, expressões, INCLUDE e
    predicado. Qualquer uma delas mudando impede o HOT.

    Com o catálogo, os identificadores são cruzados com as colunas reais
    (o que tira nome de função e de operador); sem ele, uma lista de
    palavras que não são coluna faz o papel, com menos garantia.
    """
    m = re.search(r"\bON\s+(?:ONLY\s+)?((?:\"[^\"]+\"|[\w.])+)\s*(.*)$", ddl,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return None, frozenset()
    tabela = m.group(1).split(".")[-1].strip('"').lower()
    resto = _RE_LITERAL.sub(" ", m.group(2))
    resto = re.sub(r"(?i)\bUSING\s+\w+", " ", resto)
    reais = {c.lower() for c in colunas_da_tabela} if colunas_da_tabela else None
    cols: Set[str] = set()
    for aspas, nome in _RE_IDENT.findall(resto):
        ident = (aspas or nome).lower()
        if reais is not None:
            if ident in reais:
                cols.add(ident)
        elif ident not in _NAO_COLUNA and not ident.endswith("_ops"):
            cols.add(ident)
    return tabela, frozenset(cols)


@dataclass
class Exposicao:
    """A exposição de UMA configuração à escrita da carga.

    Em entradas de índice, somando os índices novos: um INSERT numa tabela
    com dois índices novos conta 2. Faixa: [certa+provável, certa+provável+
    possível]. É contagem, não custo.
    """
    certas: float = 0.0
    provaveis: float = 0.0
    possiveis: float = 0.0
    deletes: float = 0.0
    # O ponto da faixa pela fração de HOT observada, quando há.
    observada: Optional[float] = None
    por_indice: Dict[int, Dict[str, float]] = field(default_factory=dict)
    hot_observado: Dict[str, Optional[float]] = field(default_factory=dict)
    sem_parser: int = 0

    @property
    def minimo(self) -> float:
        return self.certas + self.provaveis

    @property
    def maximo(self) -> float:
        return self.certas + self.provaveis + self.possiveis

    @property
    def tem_escrita(self) -> bool:
        return self.maximo > 0 or self.deletes > 0


def fracao_hot(escrita_observada: Optional[dict], tabela: str) -> Optional[float]:
    """n_tup_hot_upd / n_tup_upd da tabela, do ambiente gravado."""
    if not escrita_observada:
        return None
    t = escrita_observada.get(tabela)
    if not t or not t.get("upd"):
        return None
    return float(t["hot_upd"]) / float(t["upd"])


def exposicao(indices: Dict[int, Tuple[str, FrozenSet[str]]],
              configuracao: Iterable[int],
              escritas: Sequence[Escrita],
              existentes: Optional[Dict[str, FrozenSet[str]]] = None,
              escrita_observada: Optional[dict] = None) -> Exposicao:
    """A exposição da configuração, índice a índice.

    ``indices[i] = (tabela, dependências)``; ``existentes[tabela]`` = as
    colunas de que os índices JÁ existentes dependem (um UPDATE que já não
    é HOT hoje passa a manter também os índices novos).
    """
    existentes = existentes or {}
    novos_por_tabela: Dict[str, List[int]] = {}
    for i in configuracao:
        if i in indices and indices[i][0]:
            novos_por_tabela.setdefault(indices[i][0], []).append(i)
    exp = Exposicao()
    for tabela, ids in novos_por_tabela.items():
        dependem = set(existentes.get(tabela, frozenset()))
        for i in ids:
            dependem |= set(indices[i][1])
        hot = fracao_hot(escrita_observada, tabela)
        exp.hot_observado[tabela] = hot
        for e in escritas:
            if e.tabela != tabela:
                continue
            for i in ids:
                d = exp.por_indice.setdefault(i, {"certas": 0.0, "provaveis": 0.0,
                                                  "possiveis": 0.0, "deletes": 0.0})
                if e.tipo == "INSERT" and not e.upsert:
                    d["certas"] += e.execucoes
                elif e.tipo == "DELETE":
                    d["deletes"] += e.execucoes
                elif e.colunas is None or e.tipo == "MERGE":
                    d["provaveis"] += e.execucoes
                elif e.colunas & dependem:
                    d["provaveis"] += e.execucoes
                else:
                    d["possiveis"] += e.execucoes
    for d in exp.por_indice.values():
        exp.certas += d["certas"]
        exp.provaveis += d["provaveis"]
        exp.possiveis += d["possiveis"]
        exp.deletes += d["deletes"]
    if exp.possiveis and all(h is not None for h in exp.hot_observado.values()):
        obs = exp.minimo
        for i, d in exp.por_indice.items():
            tabela = indices[i][0]
            obs += d["possiveis"] * (1.0 - exp.hot_observado[tabela])
        exp.observada = obs
    elif not exp.possiveis:
        exp.observada = exp.minimo
    return exp


def equilibrio(delta_beneficio_ms: float, delta_exposicao: float) -> Optional[float]:
    """ms por entrada de índice a partir dos quais o movimento deixa de se
    pagar. None quando a exposição não aumenta."""
    if delta_exposicao <= 0:
        return None
    return delta_beneficio_ms / delta_exposicao


def custo_medido(custos: Dict[str, float], chaves: Dict[int, str],
                 configuracao: Iterable[int],
                 escritas_por_tabela: Dict[str, float],
                 tabela_de: Dict[int, str]) -> Tuple[Optional[float], List[int]]:
    """(custo em ms, índices sem custo medido).

    ``custos[chave]`` = ms a mais por escrita da carga na tabela do índice,
    medido em A/B. Custo de uma configuração = Σ_i custo_i × escritas na
    tabela de i. Com algum índice exposto sem medida, o custo é None: soma
    parcial seria um número que parece completo.
    """
    total = 0.0
    faltam: List[int] = []
    for i in configuracao:
        n = escritas_por_tabela.get(tabela_de.get(i, ""), 0.0)
        if not n:
            continue
        ms = custos.get(chaves.get(i, ""))
        if ms is None:
            faltam.append(i)
        else:
            total += ms * n
    return (None if faltam else total), faltam
