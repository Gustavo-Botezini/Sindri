#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Do banco para o contrato de dados de estatistica.py.

Um experimento é uma sessão da matriz e tudo o que foi medido sobre ela:

    sessão "m<s>"     a matriz: a linha de base (configuração vazia) e cada
                      índice sozinho (a configuração {i}) — que é, ela também,
                      uma configuração completa medida, e entra como tal
    sessão "v<o>.<k>" a validação k da otimização o: a linha de base e o
                      conjunto da otimização, na mesma sessão

Configuração é o conjunto de matriz_indice.id. Duas otimizações com o mesmo
conjunto (a 4 e a 8 do TPC-H com PGTune) são a mesma configuração, e as
validações das duas somam como sessões distintas dela.

O que entra numa análise é escolhido aqui, e nada se mistura sem dizer:

  - validação interrompida (concluida_em NULL) não entra nunca;
  - sessão com ambiente registrado diferente do de referência é excluída, e
    a diferença aparece no relatório;
  - sessão sem ambiente registrado (anterior a este registro) entra, marcada;
  - 'confirmacao' não entra na seleção, a não ser que a confirmação tenha
    falhado (aí ela já foi usada para decidir, e vira evidência de seleção).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from . import ambiente as amb
from . import otimizacao
from .estatistica import VAZIA, Bloco, Evidencia

SELECAO = ("selecao", "legado")


@dataclass
class Validacao:
    otimizacao_id: int
    validacao: int
    configuracao: FrozenSet[int]
    finalidade: str
    origem: str
    ordem: str
    ambiente: Optional[dict]
    ambiente_hash: Optional[str]
    adaptacao_id: Optional[int]
    confirma_adaptacao: Optional[int]
    limitacoes: Optional[str]
    concluida_em: object
    # o movimento da caminhada que pediu a validação ('resolucao', ...);
    # None nas validações à mão e nas anteriores ao registro
    movimento: Optional[str] = None

    @property
    def sessao(self) -> str:
        return f"v{self.otimizacao_id}.{self.validacao}"


@dataclass
class Configuracao:
    chave: FrozenSet[int]
    mb: float
    otimizacoes: List[int] = field(default_factory=list)
    origens: List[str] = field(default_factory=list)

    @property
    def rotulo(self) -> str:
        return ", ".join(dict.fromkeys(self.origens)) or "-"


@dataclass
class Experimento:
    sessao: int
    inst: otimizacao.Instancia
    ambiente_matriz: Optional[dict]
    validacoes: List[Validacao]
    configuracoes: Dict[FrozenSet[int], Configuracao]
    # As linhas cruas, por sessão: (sessao, query) -> [(fase, repeticao,
    # tempo_ms, censurado, erro, indices_usados)]
    _medidas: Dict[str, Dict[int, list]]
    # Confirmações que falharam: as validações delas viram seleção.
    confirmacoes_falhas: Set[int] = field(default_factory=set)
    excluidas: Dict[str, List[str]] = field(default_factory=dict)
    sem_ambiente: List[str] = field(default_factory=list)
    referencia_hash: Optional[str] = None

    def mb(self, c: FrozenSet[int]) -> float:
        return sum(self.inst.indices[i].mb for i in c if i in self.inst.indices)

    def validacoes_de(self, c: FrozenSet[int],
                      finalidades: Sequence[str] = SELECAO) -> List[Validacao]:
        return [v for v in self.validacoes if v.configuracao == c
                and self._entra(v, finalidades)]

    def _entra(self, v: Validacao, finalidades: Sequence[str]) -> bool:
        if v.sessao in self.excluidas:
            return False
        if v.finalidade in finalidades:
            return True
        return (v.finalidade == "confirmacao" and "selecao" in finalidades
                and v.confirma_adaptacao in self.confirmacoes_falhas)

    def evidencia(self, finalidades: Sequence[str] = SELECAO,
                  com_matriz: bool = True,
                  so_confirmacao_de: Optional[int] = None) -> Evidencia:
        """O que a inferência lê. Seleção por padrão; com
        ``so_confirmacao_de``, só as validações que confirmam aquela
        adaptação (as médias da confirmação não podem vir da seleção)."""
        blocos: List[Bloco] = []
        uso: Dict[Tuple[str, FrozenSet[int], int], bool] = {}
        if com_matriz and so_confirmacao_de is None:
            self._blocos_matriz(blocos, uso)
        for v in self.validacoes:
            if so_confirmacao_de is not None:
                if not (v.finalidade == "confirmacao"
                        and v.confirma_adaptacao == so_confirmacao_de
                        and v.sessao not in self.excluidas):
                    continue
            elif not self._entra(v, finalidades):
                continue
            self._blocos_validacao(v, blocos, uso)
        return Evidencia(blocos=blocos, uso=uso, peso=dict(self.inst.peso))

    # -- montagem dos blocos ------------------------------------------------

    @staticmethod
    def _bloco(sessao: str, c: FrozenSet[int], q: int, linhas: list,
               fase: str) -> Optional[Bloco]:
        """As repetições úteis: a 1 é aquecimento, a não ser censurada (a
        bancada para no primeiro timeout, e ela pode ser a única)."""
        medidos, cens = [], []
        for f, rep, tempo, censurado, erro, _usados in linhas:
            if f != fase or tempo is None or rep == 0:
                continue
            if censurado:
                cens.append(float(tempo))
            elif rep > 1 and erro is None:
                medidos.append(float(tempo))
        if not medidos and not cens:
            return None
        return Bloco(sessao, c, q, tuple(medidos + cens), censuradas=len(cens),
                     limite_ms=max(cens) if cens else None)

    def _blocos_matriz(self, blocos: List[Bloco], uso: dict) -> None:
        sessao = f"m{self.sessao}"
        for chave, por_query in self._medidas.items():
            if not chave.startswith(sessao + "/"):
                continue
            indice = chave.split("/", 1)[1]
            c = VAZIA if indice == "base" else frozenset([int(indice)])
            for q, linhas in por_query.items():
                if q not in self.inst.peso:
                    continue
                if c != VAZIA:
                    marca = [u for f, rep, _t, _c, erro, u in linhas if rep == 0]
                    if not marca or marca[0] is None:
                        continue
                    uso[(sessao, c, q)] = bool(marca[0])
                b = self._bloco(sessao, c, q, linhas, "antes" if c == VAZIA else "depois")
                if b and (c == VAZIA or uso.get((sessao, c, q))):
                    blocos.append(b)

    def _blocos_validacao(self, v: Validacao, blocos: List[Bloco], uso: dict) -> None:
        for q, linhas in self._medidas.get(v.sessao, {}).items():
            if q not in self.inst.peso:
                continue
            b = self._bloco(v.sessao, VAZIA, q, linhas, "antes")
            if b:
                blocos.append(b)
            marca = [(erro, u) for f, rep, _t, _c, erro, u in linhas
                     if f == "depois" and rep == 0]
            if not marca or marca[0][0] is not None:
                continue            # o EXPLAIN falhou: não se sabe o plano
            usou = bool(marca[0][1])
            uso[(v.sessao, v.configuracao, q)] = usou
            if usou:
                b = self._bloco(v.sessao, v.configuracao, q, linhas, "depois")
                if b:
                    blocos.append(b)


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------

SQL_SESSAO = """
SELECT ambiente, ambiente_hash, timeout_ms FROM monitoring.matriz_sessao WHERE id = %s
"""

SQL_MATRIZ = """
SELECT coalesce(indice_id::text, 'base'), query_ref, repeticao, tempo_ms,
       usou_indice, censurado, erro
  FROM monitoring.matriz_medida
 WHERE sessao_id = %s
"""

SQL_OTIMIZACOES = """
SELECT id, indices, parametros, status FROM monitoring.otimizacao
 WHERE sessao_id = %s ORDER BY id
"""

SQL_VALIDACOES = """
SELECT v.otimizacao_id, v.validacao, v.finalidade, v.origem, v.ordem,
       v.ambiente, v.ambiente_hash, v.adaptacao_id, v.confirma_adaptacao,
       v.limitacoes, v.concluida_em, v.movimento
  FROM monitoring.otimizacao_validacao v
  JOIN monitoring.otimizacao o ON o.id = v.otimizacao_id
 WHERE o.sessao_id = %s AND v.concluida_em IS NOT NULL
 ORDER BY v.otimizacao_id, v.validacao
"""

SQL_MEDIDAS = """
SELECT m.otimizacao_id, m.validacao, m.query_ref, m.fase, m.repeticao,
       m.tempo_ms, m.censurado, m.erro, m.indices_usados
  FROM monitoring.otimizacao_medida m
  JOIN monitoring.otimizacao o ON o.id = m.otimizacao_id
 WHERE o.sessao_id = %s
"""

SQL_CONFIRMACOES_FALHAS = """
SELECT id FROM monitoring.adaptacao WHERE sessao_id = %s AND confirmada IS FALSE
"""


def _origem(par: dict, status: str) -> str:
    if par.get("rodada") is not None:
        return f"clusters (rodada {par['rodada']})"
    if par.get("adaptacao") is not None:
        return f"adaptar ({par.get('movimento', 'proposta')})"
    if par.get("orcamento_mb") is not None:
        return f"otimizar ≤ {par['orcamento_mb']:,.0f} MB"
    return f"otimizar ({par.get('modelo', '?')}, sem orçamento)"


def carregar(conn, sessao: int, peso: str = "execucoes",
             limiar_por_query: Optional[Dict[int, float]] = None) -> Experimento:
    inst = otimizacao.carregar(conn, sessao, peso, limiar_por_query=limiar_por_query)
    with conn.cursor() as cur:
        cur.execute(SQL_SESSAO, (sessao,))
        amb_m, hash_m, _timeout = cur.fetchone()
        cur.execute(SQL_MATRIZ, (sessao,))
        matriz = cur.fetchall()
        cur.execute(SQL_OTIMIZACOES, (sessao,))
        otims = cur.fetchall()
        cur.execute(SQL_VALIDACOES, (sessao,))
        vals = cur.fetchall()
        cur.execute(SQL_MEDIDAS, (sessao,))
        medidas = cur.fetchall()
        cur.execute(SQL_CONFIRMACOES_FALHAS, (sessao,))
        falhas = {r[0] for r in cur.fetchall()}

    configuracoes: Dict[FrozenSet[int], Configuracao] = {}

    def config(c: FrozenSet[int]) -> Configuracao:
        if c not in configuracoes:
            configuracoes[c] = Configuracao(
                c, sum(inst.indices[i].mb for i in c if i in inst.indices))
        return configuracoes[c]

    config(VAZIA).origens.append("linha de base (só os índices existentes)")
    conjunto_de: Dict[int, FrozenSet[int]] = {}
    for oid, indices, par, status in otims:
        c = frozenset(int(i) for i in indices or [])
        conjunto_de[oid] = c
        cf = config(c)
        cf.otimizacoes.append(oid)
        cf.origens.append(_origem(par or {}, status or ""))

    por_sessao: Dict[str, Dict[int, list]] = {}
    for indice, q, rep, tempo, usou, cens, erro in matriz:
        fase = "antes" if indice == "base" else "depois"
        por_sessao.setdefault(f"m{sessao}/{indice}", {}).setdefault(q, []).append(
            (fase, rep, tempo, cens, erro, usou))
        if indice != "base":
            config(frozenset([int(indice)])).origens.append("matriz (índice sozinho)")
    for oid, v, q, fase, rep, tempo, cens, erro, usados in medidas:
        por_sessao.setdefault(f"v{oid}.{v}", {}).setdefault(q, []).append(
            (fase, rep, tempo, cens, erro,
             None if usados is None else [int(i) for i in usados]))

    validacoes = [Validacao(oid, v, conjunto_de.get(oid, VAZIA), fin, ori, ordem,
                            a, h, aid, conf, lim, conc, mov)
                  for oid, v, fin, ori, ordem, a, h, aid, conf, lim, conc, mov in vals]

    exp = Experimento(sessao, inst, amb_m, validacoes, configuracoes, por_sessao,
                      confirmacoes_falhas=falhas)
    _filtrar_ambiente(exp, hash_m)
    return exp


def _filtrar_ambiente(exp: Experimento, hash_matriz: Optional[str]) -> None:
    """Fixa o ambiente de referência e exclui o que foi medido em outro.

    Referência: o da matriz, se registrado; senão o mais comum entre as
    validações que o registraram.
    """
    conhecidos = [v.ambiente_hash for v in exp.validacoes if v.ambiente_hash]
    ref = hash_matriz or (Counter(conhecidos).most_common(1)[0][0]
                          if conhecidos else None)
    exp.referencia_hash = ref
    ref_amb = exp.ambiente_matriz
    if ref and not ref_amb:
        ref_amb = next((v.ambiente for v in exp.validacoes
                        if v.ambiente_hash == ref), None)
    if not hash_matriz:
        exp.sem_ambiente.append(f"m{exp.sessao}")
    for v in exp.validacoes:
        if not v.ambiente_hash:
            exp.sem_ambiente.append(v.sessao)
        elif ref and v.ambiente_hash != ref:
            exp.excluidas[v.sessao] = (amb.diferencas(ref_amb, v.ambiente)
                                       or [f"hash {v.ambiente_hash} ≠ {ref}"])
