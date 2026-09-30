#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A heurística que propõe índices candidatos para um cluster.

Ela não escolhe — quem escolhe é o modelo, em modelo.py. O papel daqui é
transformar o que está em ``monitoring.plan_node`` em candidatos **concretos**,
cada um com o número que o justifica, para que a escolha seja entre coisas
medidas e não entre ideias.

Quatro famílias, na ordem em que costumam valer a pena:

    seq_scan_filtro   Seq Scan repetido que descarta muita linha por um
                      filtro estável. O caso clássico, e o que o índice
                      ix_plan_node_candidato_indice de 04-worker-schema.sql
                      existe para achar.
    sort_em_disco     Sort que derramou para disco: a chave de ordenação é
                      candidata, e um índice na ordem certa dispensa o Sort.
    hash_cond         Junção por hash sobre coluna sem índice.
    index_filtro      Index Scan que ainda descarta linha por filtro: o
                      índice existente não cobre o predicado inteiro,
                      candidato a coluna extra ou INCLUDE.

**As colunas vêm do catálogo, não do texto.** Extrair identificador de
``((l_shipdate >= '1994-01-01'::date) AND (l_shipdate < ...))`` com regex
sozinha devolve ``date`` e ``l_shipdate`` no mesmo saco. Por isso cada
identificador candidato é cruzado com as colunas reais da relação, lidas de
pg_attribute no banco observado. O que não é coluna, cai fora.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from .banco import linhas_como_dicts

# Todo índice criado pela bancada carrega este prefixo. É o que torna
# `limpar` possível e o que garante que um índice de experimento nunca seja
# confundido com um índice de produção.
PREFIXO = "brokk_exp_"

# Limite de identificador do Postgres. Nome maior é truncado em silêncio pelo
# servidor, e aí o DROP pelo nome que geramos não acha nada.
MAX_NOME = 63

# Abaixo disto o Seq Scan não descarta o suficiente para justificar índice.
MINIMO_DESCARTADAS = 1000


# --------------------------------------------------------------------------
# A evidência, vinda dos planos
# --------------------------------------------------------------------------
# Um UNION ALL de quatro consultas e não quatro chamadas: o ranking final é
# entre famílias, e comparar ms_total exige que todas cheguem na mesma lista.
#
# position('Disk' in ...) e não ILIKE '%Disk%': um sinal de porcentagem aqui
# vira placeholder para o psycopg e quebra a execução.
SQL_EVIDENCIA = """
WITH nos AS (
    SELECT x.query_ref, n.*
      FROM monitoring.plan_execution x
      JOIN monitoring.plan_node n
        ON n.event_id = x.event_id AND n.event_time = x.event_time
     WHERE x.query_ref = ANY (%s)
),
bruto AS (
    SELECT 'seq_scan_filtro'                      AS familia,
           relation_name                          AS relacao,
           filtro                                 AS expressao,
           NULL::text                             AS indice_atual,
           count(*)                               AS ocorrencias,
           count(DISTINCT query_ref)              AS queries,
           array_agg(DISTINCT query_ref)          AS refs,
           round(avg(linhas_removidas_filtro))    AS descartadas_media,
           round(avg(linhas_reais * loops))       AS linhas_media,
           round(sum(tempo_total_ms))             AS ms_total
      FROM nos
     WHERE node_type = 'Seq Scan'
       AND filtro IS NOT NULL
       AND linhas_removidas_filtro >= %s
     GROUP BY 1, 2, 3, 4

    UNION ALL

    SELECT 'sort_em_disco', relation_name, sort_key, sort_method,
           count(*), count(DISTINCT query_ref), array_agg(DISTINCT query_ref),
           NULL, round(avg(linhas_reais * loops)), round(sum(tempo_total_ms))
      FROM nos
     WHERE node_type = 'Sort'
       AND sort_key IS NOT NULL
       AND sort_method IS NOT NULL
       AND position('Disk' in sort_method) > 0
     GROUP BY 1, 2, 3, 4

    UNION ALL

    SELECT 'hash_cond', relation_name, hash_cond, NULL,
           count(*), count(DISTINCT query_ref), array_agg(DISTINCT query_ref),
           NULL, round(avg(linhas_reais * loops)), round(sum(tempo_total_ms))
      FROM nos
     WHERE hash_cond IS NOT NULL
     GROUP BY 1, 2, 3, 4

    UNION ALL

    SELECT 'index_filtro', relation_name, filtro, index_name,
           count(*), count(DISTINCT query_ref), array_agg(DISTINCT query_ref),
           round(avg(linhas_removidas_filtro)), round(avg(linhas_reais * loops)),
           round(sum(tempo_total_ms))
      FROM nos
     WHERE node_type LIKE 'Index%%'
       AND filtro IS NOT NULL
       AND linhas_removidas_filtro >= %s
     GROUP BY 1, 2, 3, 4
)
SELECT * FROM bruto
 ORDER BY ms_total DESC NULLS LAST, descartadas_media DESC NULLS LAST
"""

SQL_CATALOGO = """
SELECT c.relname AS tabela, a.attname AS coluna, t.typname AS tipo
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
  JOIN pg_type t      ON t.oid = a.atttypid
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm')
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY c.relname, a.attnum
"""

# As colunas-chave de cada índice que já existe, na ordem. É o que permite
# descartar o candidato que já está criado — sem isto a lista que vai para o
# modelo fica dominada por "CREATE INDEX ON partsupp (ps_suppkey)", que é
# literalmente o idx_partsupp_suppkey do baseline.
#
# unnest(indkey) WITH ORDINALITY preserva a ordem das colunas, que é o que
# importa num índice composto. O join com pg_attribute descarta o attnum 0
# das colunas de expressão, que não têm nome.
SQL_INDICES_EXISTENTES = """
SELECT c.relname AS tabela,
       i.relname AS indice,
       array_agg(a.attname ORDER BY k.ord) AS colunas
  FROM pg_index x
  JOIN pg_class c     ON c.oid = x.indrelid
  JOIN pg_class i     ON i.oid = x.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
 WHERE n.nspname = 'public'
 GROUP BY 1, 2
"""


@dataclass
class Candidato:
    familia: str
    relacao: str
    colunas: List[str]
    expressao: str
    queries: List[int]
    ocorrencias: int
    ms_total: float
    descartadas_media: Optional[float] = None
    linhas_media: Optional[float] = None
    indice_atual: Optional[str] = None

    @property
    def ddl(self) -> str:
        cols = ", ".join(self.colunas)
        return f"CREATE INDEX {self.nome} ON {self.relacao} ({cols})"

    @property
    def nome(self) -> str:
        return nome_indice(self.relacao, self.colunas)

    def como_dict(self) -> dict:
        return {
            "familia": self.familia,
            "relacao": self.relacao,
            "colunas": self.colunas,
            "ddl_sugerido": self.ddl,
            "expressao_medida": self.expressao,
            "queries": self.queries,
            "ocorrencias": self.ocorrencias,
            "ms_total": self.ms_total,
            "descartadas_media": self.descartadas_media,
            "linhas_media": self.linhas_media,
            "indice_atual": self.indice_atual,
        }


def nome_indice(relacao: str, colunas: Sequence[str]) -> str:
    bruto = f"{PREFIXO}{relacao}_{'_'.join(colunas)}"
    return re.sub(r"[^a-z0-9_]", "", bruto.lower())[:MAX_NOME]


# --------------------------------------------------------------------------
# O portão do DDL
# --------------------------------------------------------------------------
# A bancada executa DDL que veio do modelo contra o banco observado. Isso
# passa por aqui antes, e o portão é estreito de propósito: uma única
# instrução, CREATE INDEX e nada mais, com nome forçado ao prefixo do
# experimento. Não é desconfiança do modelo — é que um DROP TABLE que chegue
# aqui por qualquer caminho custa o dataset inteiro, e o custo de conferir é
# uma regex.

_RE_CREATE_INDEX = re.compile(
    r"""^\s*CREATE\s+(?P<unico>UNIQUE\s+)?INDEX\s+
        (?P<concur>CONCURRENTLY\s+)?
        (?:IF\s+NOT\s+EXISTS\s+)?
        (?P<nome>"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)\s+
        ON\s+(?P<resto>.+)$""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def validar_ddl(ddl: str) -> str:
    """Devolve o DDL saneado, ou levanta ValueError dizendo o que reprovou."""
    limpo = (ddl or "").strip().rstrip(";").strip()
    if not limpo:
        raise ValueError("DDL vazio")
    if ";" in limpo:
        raise ValueError("mais de uma instrução no DDL")
    # Comentário SQL é o jeito mais fácil de esconder texto do resto da regex.
    if "--" in limpo or "/*" in limpo:
        raise ValueError("comentário no DDL")

    m = _RE_CREATE_INDEX.match(limpo)
    if not m:
        raise ValueError("não é um CREATE INDEX de uma instrução só")
    if m.group("concur"):
        # CONCURRENTLY não roda em transação e leva muito mais tempo; a
        # bancada não precisa dele, e ele complicaria o rollback.
        raise ValueError("CONCURRENTLY não é aceito na bancada")

    nome = m.group("nome").strip('"')
    if not nome.startswith(PREFIXO):
        nome = re.sub(r"[^a-z0-9_]", "", f"{PREFIXO}{nome}".lower())[:MAX_NOME]
    unico = m.group("unico") or ""
    return f"CREATE {unico}INDEX {nome} ON {m.group('resto').strip()}"


def nome_do_ddl(ddl: str) -> str:
    m = _RE_CREATE_INDEX.match(ddl.strip())
    if not m:
        raise ValueError(f"não consegui achar o nome do índice em: {ddl[:80]}")
    return m.group("nome").strip('"')


# --------------------------------------------------------------------------
# Catálogo e extração de colunas
# --------------------------------------------------------------------------

def catalogo(conn_observado) -> Dict[str, List[str]]:
    """{tabela: [colunas na ordem do atributo]} do banco observado."""
    with conn_observado.cursor() as cur:
        cur.execute(SQL_CATALOGO)
        linhas = cur.fetchall()
    cat: Dict[str, List[str]] = {}
    for tabela, coluna, _tipo in linhas:
        cat.setdefault(tabela, []).append(coluna)
    return cat


def extrair_colunas(expressao: str, relacao: Optional[str],
                    cat: Dict[str, List[str]]) -> List[str]:
    """Identificadores da expressão que são coluna de verdade, em ordem.

    Quando a relação é conhecida, só as colunas dela. Quando não é (o nó de
    Hash Join não carrega relation_name), procura em todas as tabelas e só
    aceita o identificador se ele pertencer a exatamente uma — um `l_orderkey`
    resolve; um `id` que existe em cinco tabelas seria chute.
    """
    if not expressao:
        return []

    candidatas: Set[str]
    if relacao and relacao in cat:
        candidatas = set(cat[relacao])
    else:
        contagem: Dict[str, int] = {}
        for colunas in cat.values():
            for c in colunas:
                contagem[c] = contagem.get(c, 0) + 1
        candidatas = {c for c, n in contagem.items() if n == 1}

    vistas: List[str] = []
    for ident in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expressao):
        alvo = ident.lower()
        if alvo in candidatas and alvo not in vistas:
            vistas.append(alvo)
    return vistas


def relacao_da_coluna(coluna: str, cat: Dict[str, List[str]]) -> Optional[str]:
    donas = [t for t, cols in cat.items() if coluna in cols]
    return donas[0] if len(donas) == 1 else None


def indices_existentes(conn_observado) -> Dict[str, List[Tuple[str, List[str]]]]:
    """{tabela: [(nome do índice, colunas na ordem)]}."""
    with conn_observado.cursor() as cur:
        cur.execute(SQL_INDICES_EXISTENTES)
        linhas = cur.fetchall()
    por_tabela: Dict[str, List[Tuple[str, List[str]]]] = {}
    for tabela, indice, colunas in linhas:
        por_tabela.setdefault(tabela, []).append((indice, list(colunas)))
    return por_tabela


def ja_coberto(relacao: str, colunas: Sequence[str],
               existentes: Dict[str, List[Tuple[str, List[str]]]]) -> Optional[str]:
    """Nome do índice que já cobre estas colunas, se houver.

    Cobre significa prefixo: um índice em (a, b) já atende uma busca por (a),
    porque o B-tree do Postgres pode varrer pelo prefixo da chave. O
    contrário não vale — (a) não atende (a, b).
    """
    alvo = list(colunas)
    for nome, cols in existentes.get(relacao, []):
        if cols[:len(alvo)] == alvo:
            return nome
    return None


# --------------------------------------------------------------------------
# Propor
# --------------------------------------------------------------------------

def propor(conn_brokk, conn_observado, refs: Sequence[int],
           limite: int = 12,
           minimo_descartadas: int = MINIMO_DESCARTADAS,
           manter_redundantes: bool = False) -> Tuple[List[Candidato], int]:
    """Os candidatos de um cluster, do mais caro para o mais barato.

    Devolve (candidatos, quantos foram descartados por já existirem).
    """
    cat = catalogo(conn_observado)
    existentes = indices_existentes(conn_observado)

    with conn_brokk.cursor() as cur:
        cur.execute(SQL_EVIDENCIA,
                    (list(refs), minimo_descartadas, minimo_descartadas))
        evidencias = linhas_como_dicts(cur)

    propostos: List[Candidato] = []
    vistos: Set[str] = set()
    redundantes = 0
    for e in evidencias:
        relacao = e["relacao"]
        colunas = extrair_colunas(e["expressao"], relacao, cat)
        if not colunas:
            continue
        if relacao is None:
            # Nó sem relation_name (Hash Join, Sort acima de junção): a
            # relação sai da coluna, e só quando ela é inequívoca.
            relacao = relacao_da_coluna(colunas[0], cat)
            if relacao is None:
                continue
            colunas = [c for c in colunas if c in cat[relacao]]
            if not colunas:
                continue
        if relacao not in cat:
            continue

        # Índice com mais de três colunas é quase sempre a expressão inteira
        # virando índice, não uma decisão. Corta na terceira.
        colunas = colunas[:3]

        # O candidato que já existe é ruído na lista. Medido no corpus: sem
        # este corte, os seis candidatos do topo de quase todo cluster são os
        # oito índices de FK de initdb/03-baseline-indexes.sql sendo
        # redescobertos pelos Hash Cond das junções do TPC-H.
        coberto = ja_coberto(relacao, colunas, existentes)
        if coberto and not manter_redundantes:
            redundantes += 1
            continue

        cand = Candidato(
            familia=e["familia"],
            relacao=relacao,
            colunas=colunas,
            expressao=(e["expressao"] or "")[:400],
            queries=sorted(e["refs"] or []),
            ocorrencias=int(e["ocorrencias"] or 0),
            ms_total=float(e["ms_total"] or 0),
            descartadas_media=(float(e["descartadas_media"])
                               if e["descartadas_media"] is not None else None),
            linhas_media=(float(e["linhas_media"])
                          if e["linhas_media"] is not None else None),
            indice_atual=e["indice_atual"] or coberto,
        )
        if cand.nome in vistos:
            continue
        vistos.add(cand.nome)
        propostos.append(cand)
        if len(propostos) >= limite:
            break
    return propostos, redundantes
