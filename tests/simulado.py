"""Uma bancada sintética, com verdade conhecida.

Produz as mesmas linhas que a matriz e `validar` gravam no banco, com o
ruído do modelo de estatistica.py: deslocamento comum por sessão (γ), efeito
da sessão por query (α) e variação entre repetições (ε), em escala log, e
censura no timeout como a bancada faz (para no primeiro estouro).

A verdade é uma função ``efeito(configuração, query) -> (fator, usados)``:
o tempo da query na configuração é ``base[q] × fator``, e ``usados`` são os
índices que o plano cita. Ela pode ser qualquer coisa, inclusive o que o
modelo do máximo não prevê (sinergia, regressão só em conjunto).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

from src.index_provider import evidencia as evd
from src.index_provider import otimizacao
from src.index_provider.estatistica import VAZIA

MB = 1048576.0
Efeito = Callable[[FrozenSet[int], int], Tuple[float, Set[int]]]


@dataclass
class Verdade:
    base: Dict[int, float]
    peso: Dict[int, float]
    tamanho_mb: Dict[int, float]
    efeito: Efeito
    relacao: Dict[int, str] = field(default_factory=dict)
    ddl: Dict[int, str] = field(default_factory=dict)
    sigma_alfa: float = 0.08
    sigma_eps: float = 0.03
    sigma_gama: float = 0.03
    repeticoes: int = 5
    timeout_ms: Optional[float] = None
    # A matriz pode ver outra coisa (ruído de uma sessão só, plano diferente).
    efeito_matriz: Optional[Efeito] = None


def efeito_por_indice(tabela: Dict[int, Dict[int, float]]) -> Efeito:
    """O modelo simples: cada query fica com o melhor índice que a atende
    (menor fator), e é ele que o plano cita."""
    def efeito(c: FrozenSet[int], q: int) -> Tuple[float, Set[int]]:
        opcoes = [(tabela[i][q], i) for i in c if q in tabela.get(i, {})]
        if not opcoes:
            return 1.0, set()
        f, i = min(opcoes)
        return f, {i}
    return efeito


class Bancada:
    def __init__(self, v: Verdade, semente: int = 0):
        self.v = v
        self.rng = np.random.default_rng(semente)
        self.medidas: Dict[str, Dict[int, list]] = {}
        self.validacoes: List[evd.Validacao] = []
        self.oid_de: Dict[FrozenSet[int], int] = {}
        # o movimento que propôs cada configuração, como otimizacao_para grava
        self.movimento_de: Dict[FrozenSet[int], str] = {}
        self._matriz()

    def _reps(self, t: float, gama: float, fase: str) -> list:
        v = self.v
        a = self.rng.normal(0, v.sigma_alfa)
        linhas = []
        for r in range(1, v.repeticoes + 1):
            x = t * math.exp(gama + a + self.rng.normal(0, v.sigma_eps))
            if v.timeout_ms and x >= v.timeout_ms:
                linhas.append((fase, r, float(v.timeout_ms), True, None, None))
                break
            linhas.append((fase, r, x, False, None, None))
        return linhas

    def _matriz(self) -> None:
        v = self.v
        efeito = v.efeito_matriz or v.efeito
        g = self.rng.normal(0, v.sigma_gama)
        self.medidas["m1/base"] = {q: self._reps(t, g, "antes") for q, t in v.base.items()}
        for i in v.tamanho_mb:
            por_q: Dict[int, list] = {}
            for q, t in v.base.items():
                fator, usados = efeito(frozenset([i]), q)
                usou = i in usados
                linhas = [("depois", 0, None, False, None, usou)]
                if usou:
                    linhas += self._reps(t * fator, g, "depois")
                por_q[q] = linhas
            self.medidas[f"m1/{i}"] = por_q

    def validar(self, c: FrozenSet[int], ordem: str = "base_primeiro",
                finalidade: str = "selecao",
                confirma: Optional[int] = None,
                movimento: str = "proposta") -> str:
        v = self.v
        oid = self.oid_de.setdefault(c, len(self.oid_de) + 1)
        self.movimento_de.setdefault(c, movimento)
        k = 1 + sum(1 for x in self.validacoes if x.otimizacao_id == oid)
        sid = f"v{oid}.{k}"
        g = self.rng.normal(0, v.sigma_gama)
        por_q: Dict[int, list] = {}
        for q, t in v.base.items():
            linhas = self._reps(t, g, "antes")
            fator, usados = v.efeito(c, q)
            usados = sorted(usados & set(c))
            linhas.append(("depois", 0, None, False, None, usados))
            if usados:
                linhas += self._reps(t * fator, g, "depois")
            por_q[q] = linhas
        self.medidas[sid] = por_q
        self.validacoes.append(evd.Validacao(
            oid, k, c, finalidade, "adaptar", ordem, None, None, None, confirma,
            None, "agora", movimento))
        return sid

    def instancia(self, limiar_por_query: Optional[Dict[int, float]] = None,
                  limiar: float = 10.0) -> otimizacao.Instancia:
        v = self.v
        indices = {i: otimizacao.Indice(
            i, f"k{i}", v.ddl.get(i, f"CREATE INDEX brokk_exp_i{i} ON "
                                     f"{v.relacao.get(i, 't')} (c{i})"),
            v.relacao.get(i, "t"), int(mb * MB), None, "heuristica")
            for i, mb in v.tamanho_mb.items()}
        inst = otimizacao.Instancia(1, "execucoes", limiar, indices,
                                    dict(v.peso), {})

        def med(linhas, fase):
            xs = [t for f, r, t, cens, _e, _u in linhas
                  if f == fase and t is not None and (cens or r > 1)]
            cens = any(c for f, _r, _t, c, _e, _u in linhas if f == fase)
            return (float(np.median(xs)) if xs else None), cens

        base = self.medidas["m1/base"]
        for i in v.tamanho_mb:
            for q, linhas in self.medidas[f"m1/{i}"].items():
                if not linhas[0][5]:
                    continue
                a, ac = med(base[q], "antes")
                d, dc = med(linhas, "depois")
                lim = (limiar_por_query or {}).get(q, limiar)
                delta, piso = otimizacao.delta_medido(a, d, ac, dc, lim)
                if delta is None:
                    inst.desconhecidos.add((q, i))
                    delta = 0.0
                if piso:
                    inst.pisos.add((q, i))
                inst.delta[(q, i)] = delta
        return inst

    def experimento(self, limiar_por_query: Optional[Dict[int, float]] = None
                    ) -> evd.Experimento:
        inst = self.instancia(limiar_por_query)
        configs = {VAZIA: evd.Configuracao(VAZIA, 0.0, [], ["linha de base"])}
        for c, oid in self.oid_de.items():
            configs[c] = evd.Configuracao(
                c, sum(self.v.tamanho_mb[i] for i in c), [oid],
                [f"adaptar ({self.movimento_de.get(c, 'proposta')})"])
        for i in self.v.tamanho_mb:
            configs.setdefault(frozenset([i]), evd.Configuracao(
                frozenset([i]), self.v.tamanho_mb[i], [], ["matriz"]))
        return evd.Experimento(1, inst, None, list(self.validacoes), configs,
                               self.medidas)


def explorar(bancada: Bancada, par, escritas=(), max_medicoes: int = 15):
    """O laço de `adaptar --executar`, com a bancada sintética. Devolve
    (análise, trajetória, medições feitas)."""
    from src.index_provider import adaptativa, estatistica as est
    medicoes = 0
    while True:
        exp = bancada.experimento()
        ruido = est.estimar_ruido(exp.evidencia())
        lim = adaptativa.limiares_de_ruido(ruido, par.confianca)
        if lim:
            exp = bancada.experimento(lim)
        an = adaptativa.analisar(exp, par, escritas, ruido=ruido)
        traj = adaptativa.caminhar(an)
        if traj.proxima is None or medicoes >= max_medicoes:
            return an, traj, medicoes
        bancada.validar(traj.proxima.configuracao, traj.proxima.ordem,
                        movimento=traj.proxima.movimento)
        medicoes += 1
