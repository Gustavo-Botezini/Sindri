#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inferência sobre configurações completas medidas: ruído, censura,
intervalos e decisões.

Este módulo não fala com banco nenhum. Recebe blocos de medida (ver o
contrato em esquema.sql, seção 9) e devolve estimativas com incerteza. É o
que torna os cálculos testáveis com dados sintéticos de verdade conhecida.

O modelo
--------
Para a query q, num bloco b (uma fase de uma sessão, numa configuração c) e
na repetição r:

    log T[q,b,r] = μ[q,c] + γ[s(b)] + α[q,b] + ε[q,b,r]

    μ[q,c]   o desempenho da query na configuração: o que se quer estimar
    γ[s]     o deslocamento comum da sessão: quando a sessão inteira roda
             mais devagar (cache, vizinho na VM), todas as queries sentem.
             Medido no PGTune: a linha de base da matriz ficou +15% em 15 de
             19 queries; as das validações das 18h às 19h30, entre −6% e
             +1%, e as das 20h45 às 21h30 do mesmo dia, +13% a +18%. O bloco
             de configuração de uma validação sente uma fração β disso
             (estimada: 0,57)
    α[q,b]   o efeito da sessão sobre esta query: o que sobra entre blocos
             depois de γ. Variância σ²_α por query
    ε[q,b,r] a variação entre repetições do mesmo bloco. Variância σ²_ε por
             query

A escala é log porque o ruído é multiplicativo (uma query de 2 s varia em
centenas de ms, uma de 2 ms em décimos) e a distribuição dos tempos é
assimétrica à direita.

**A unidade de inferência é o bloco, não a repetição.** O centro de um bloco
é a mediana do log das repetições úteis (a primeira é aquecimento). A
variância de um centro é σ²_α + κ·σ²_ε/r: mais repetições no mesmo bloco só
encolhem a segunda parcela. Medido nos dados do TPC-H com PGTune e do YCSB: a
variação entre blocos é ~2× a de dentro do bloco, então dez repetições numa
sessão estão longe de valer dez sessões. Um intervalo feito só com as
repetições seria otimista na mesma proporção.

**Poucas sessões, muitas queries.** Com 2 a 8 blocos por query, a variância
entre blocos de cada query é mal estimada sozinha. Ela é moderada por Bayes
empírico (Smyth, 2004, o ``limma``): cada query fica entre a sua variância e
a variância típica da carga, pesadas pelos graus de liberdade de cada uma, e
o peso da carga (d0) é estimado dos próprios dados. Query muito diferente das
outras fica com a sua; query com pouco dado empresta das outras.

**Censura.** Um bloco que estourou o timeout diz T ≥ limite, não T = limite.
A média do grupo é estimada pela verossimilhança censurada à direita (Tobit
em log): blocos medidos entram pela densidade, censurados pela cauda. Com
todos os blocos censurados não há limite superior identificável: o tempo da
configuração é "≥ limite", e toda comparação que dependa dele fica com o
intervalo aberto desse lado. O limite nunca vira valor medido.

**Bootstrap paramétrico.** As quantidades de decisão são não lineares (somas
de exponenciais, ponderadas pela frequência) e misturam grupos com e sem
censura. Em vez de fórmulas aproximadas, sorteia-se: para cada query uma
variância (da posterior moderada, que carrega a incerteza de estimá-la com
poucos blocos), e com ela a média de cada grupo. A mesma amostra de uma query
é usada em todas as configurações, então a covariância entre B(S₁) e B(S₂)
vem de graça, e uma query que é a mesma nas duas configurações cancela
exatamente.

**Plano que muda entre sessões.** Se numa validação a query usou um índice da
configuração e noutra não, a configuração é uma mistura: com frequência p a
query roda no plano novo, com 1 − p no plano da linha de base. O relatório
marca essas queries. Quem não usou índice nenhum da configuração em sessão
nenhuma tem o tempo da linha de base, e não uma diferença de medidas: é a
regra de matriz_resultado.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import (Dict, FrozenSet, Iterable, List, Optional, Sequence, Set,
                    Tuple)

import numpy as np

VAZIA: FrozenSet[int] = frozenset()

CONFIANCA_PADRAO = 0.90       # bilateral: cada limite é unilateral a 95%
AMOSTRAS_PADRAO = 4000
SEMENTE_PADRAO = 42
TEMPO_MINIMO_MS = 1e-3        # log(0) não existe; 1 µs é abaixo da resolução
# d0 quando não há queries suficientes para estimá-lo: o valor típico da
# carga conta como 4 blocos a mais. Só é usado com menos de 3 queries com
# bloco repetido.
D0_SEM_DADOS = 4.0
D0_MAXIMO = 1000.0            # acima disto, "todas as queries têm a mesma variância"
# Queries por bloco de linha de base abaixo das quais o deslocamento comum da
# sessão não é estimado (ver deslocamentos()).
Q_MINIMO_DESLOCAMENTO = 8
PONTOS_GRADE = 400            # resolução da posterior censurada

# κ_r: variância da mediana de r normais, em unidades de σ²/r. Para r ≥ 9,
# a assintótica π/2.
_KAPPA_MEDIANA = {1: 1.0, 2: 1.0, 3: 1.3473, 4: 1.1930, 5: 1.4339,
                  6: 1.2878, 7: 1.4847, 8: 1.3412}


def kappa_mediana(r: int) -> float:
    return _KAPPA_MEDIANA.get(r, math.pi / 2)


# --------------------------------------------------------------------------
# O contrato de entrada
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Bloco:
    """As repetições úteis de uma query numa fase de uma sessão.

    ``tempos`` em ms, sem a repetição de aquecimento. As censuradas entram
    com o valor do limite e são contadas em ``censuradas``.
    """
    sessao: str
    configuracao: FrozenSet[int]
    query: int
    tempos: Tuple[float, ...]
    censuradas: int = 0
    limite_ms: Optional[float] = None

    @property
    def censurado(self) -> bool:
        """A mediana do bloco é só um piso?

        A mediana de r valores usa a estatística de ordem ⌈(r+1)/2⌉ (a de
        cima, com r par). Ela é censurada quando há pelo menos ⌈r/2⌉
        censuradas, que ficam no topo da ordenação.
        """
        r = len(self.tempos)
        return self.censuradas > 0 and self.censuradas >= math.ceil(r / 2)

    @property
    def logs(self) -> np.ndarray:
        return np.log(np.maximum(np.asarray(self.tempos, float), TEMPO_MINIMO_MS))

    @property
    def centro(self) -> float:
        """Mediana do log das repetições; com censura, o log do limite."""
        if self.censurado:
            return math.log(max(self.limite_ms or max(self.tempos), TEMPO_MINIMO_MS))
        return float(np.median(self.logs))


@dataclass
class Evidencia:
    """Tudo o que a inferência lê.

    ``uso[(sessao, configuracao, query)]`` diz se, naquela sessão, o plano da
    query usou algum índice da configuração (EXPLAIN com o conjunto
    criado). False = a query não foi remedida: ela roda no plano da linha
    de base. A chave só existe para configurações não vazias, nas sessões
    em que a configuração foi medida.
    """
    blocos: List[Bloco]
    uso: Dict[Tuple[str, FrozenSet[int], int], bool]
    peso: Dict[int, float]

    def sessoes_da(self, c: FrozenSet[int]) -> List[str]:
        return sorted({s for (s, cc, _q) in self.uso if cc == c}
                      | {b.sessao for b in self.blocos if b.configuracao == c})

    def configuracoes(self) -> List[FrozenSet[int]]:
        cs = {b.configuracao for b in self.blocos} | {c for (_s, c, _q) in self.uso}
        return sorted(cs, key=lambda c: (len(c), sorted(c)))


# --------------------------------------------------------------------------
# O ruído
# --------------------------------------------------------------------------

def _queries_por_sessao(blocos: Sequence[Bloco]) -> Dict[str, int]:
    n: Dict[str, int] = {}
    for b in blocos:
        if b.configuracao == VAZIA and not b.censurado:
            n[b.sessao] = n.get(b.sessao, 0) + 1
    return n


def deslocamentos(blocos: Sequence[Bloco], iteracoes: int = 4) -> Dict[str, float]:
    """γ por sessão, por median polish sobre os blocos de linha de base.

    Só a linha de base serve: ela é a mesma configuração em toda sessão, e
    nela todas as queries rodam, então a diferença comum entre sessões é só
    sessão. Os blocos censurados ficam de fora (o centro deles não é valor).
    Normalizado para a mediana dos γ ser 0: a escala é "uma sessão típica".

    Só com Q_MINIMO_DESLOCAMENTO queries por bloco: com poucas, a mediana
    das queries da sessão É praticamente o desvio de uma delas, e remover γ
    removeria a variação própria das queries, que é o que se quer medir.

    Nos blocos de configuração só as queries afetadas rodam, e γ não é
    estimável neles. Numa validação (sessão curta, linha de base ao lado), a
    fração β do γ que chega ao bloco de configuração é estimada dos dados
    (``estimar_pareamento``) e aplicada; na matriz (linha de base medida no
    começo, horas antes do bloco de cada índice), não, e γ entra como efeito
    aleatório (``ModeloRuido.sigma_gama``).
    """
    n = _queries_por_sessao(blocos)
    base = [b for b in blocos if b.configuracao == VAZIA and not b.censurado
            and n.get(b.sessao, 0) >= Q_MINIMO_DESLOCAMENTO]
    sessoes = sorted({b.sessao for b in base})
    if len(sessoes) < 2:
        return {}
    y = {(b.query, b.sessao): b.centro for b in base}
    queries = sorted({q for q, _ in y})
    gama = {s: 0.0 for s in sessoes}
    mu: Dict[int, float] = {}
    for _ in range(iteracoes):
        for q in queries:
            vals = [y[(q, s)] - gama[s] for s in sessoes if (q, s) in y]
            mu[q] = float(np.median(vals))
        for s in sessoes:
            res = [y[(q, s)] - mu[q] for q in queries if (q, s) in y]
            gama[s] = float(np.median(res)) if res else 0.0
        centro = float(np.median(list(gama.values())))
        gama = {s: g - centro for s, g in gama.items()}
    return gama


def trigamma_inversa(x: float) -> float:
    """y tal que ψ'(y) = x, por Newton (o algoritmo do limma)."""
    from scipy.special import polygamma
    if x > 1e7:
        return 1.0 / math.sqrt(x)
    if x < 1e-6:
        return 1.0 / x
    y = 0.5 + 1.0 / x
    for _ in range(50):
        tri = float(polygamma(1, y))
        dif = tri * (1.0 - tri / x) / float(polygamma(2, y))
        y += dif
        if -dif / y < 1e-8:
            break
    return y


def moderar(s2: Sequence[float], gl: Sequence[float]) -> Tuple[float, float]:
    """(s0², d0) do Bayes empírico sobre variâncias com gl graus de liberdade.

    Método dos momentos sobre log s², como o ``fitFDist`` do limma: se as
    variâncias verdadeiras das queries seguem uma qui-quadrado inversa
    escalada com d0 graus e escala s0², então
    Var(log s²_q) = ψ'(gl_q/2) + ψ'(d0/2). O que sobra da variância
    observada depois do ruído de estimação diz quanto as queries diferem de
    verdade; pouca diferença dá d0 grande (todas emprestam muito umas das
    outras), muita dá d0 pequeno.
    """
    from scipy.special import digamma, polygamma
    s2a = np.maximum(np.asarray(s2, float), 1e-10)
    gla = np.asarray(gl, float)
    if len(s2a) < 3:
        return float(np.median(s2a)), D0_SEM_DADOS
    e = np.log(s2a) - digamma(gla / 2) + np.log(gla / 2)
    media = float(np.mean(e))
    resto = float(np.var(e, ddof=1) - np.mean(polygamma(1, gla / 2)))
    if resto <= 0:
        return math.exp(media), D0_MAXIMO
    d0 = min(2.0 * trigamma_inversa(resto), D0_MAXIMO)
    s0 = math.exp(media + float(digamma(d0 / 2)) - math.log(d0 / 2))
    return s0, d0


@dataclass
class RuidoQuery:
    """O ruído de uma query, nas duas escalas que importam."""
    query: int
    blocos: int                 # blocos não censurados usados
    gl_entre: float             # graus de liberdade da variância entre blocos
    s2_entre: Optional[float]   # variância entre blocos (dos centros), crua
    s2_dentro: Optional[float]  # σ²_ε, entre repetições
    repeticoes_tipicas: float
    tau2: float                 # variância moderada de UM centro de bloco
    gl: float                   # graus de liberdade de tau2 (d0 + gl_entre)

    @property
    def var_dentro_no_centro(self) -> float:
        """A parte de σ²_ε que chega ao centro de um bloco: κ·σ²_ε/r."""
        if self.s2_dentro is None:
            return 0.0
        r = max(int(round(self.repeticoes_tipicas)), 1)
        return kappa_mediana(r) * self.s2_dentro / r

    @property
    def sigma_alfa(self) -> float:
        """σ_α estimado: o que a sessão acrescenta além das repetições."""
        return math.sqrt(max(self.tau2 - self.var_dentro_no_centro, 0.0))

    @property
    def sigma_eps(self) -> Optional[float]:
        return None if self.s2_dentro is None else math.sqrt(self.s2_dentro)


@dataclass
class ModeloRuido:
    por_query: Dict[int, RuidoQuery]
    deslocamento: Dict[str, float]
    s0_2: Optional[float]
    d0: Optional[float]
    # Há bloco repetido (mesma query, mesma configuração, sessões
    # diferentes) em algum lugar? Sem isso a variação entre sessões não é
    # estimável, e nenhuma decisão pode ser tomada.
    suficiente: bool
    avisos: List[str] = field(default_factory=list)
    # Desvio padrão do deslocamento comum de sessão, medido nas linhas de
    # base (descontado o ruído de estimá-lo). Entra nos blocos de
    # configuração sem ajuste (os da matriz) como efeito aleatório comum às
    # queries da mesma sessão: é o que impede a soma sobre queries de supor
    # erros independentes quando a sessão inteira andou junto.
    sigma_gama: float = 0.0
    # Pareamento: que fração β do γ da linha de base chega ao bloco de
    # configuração da MESMA validação, e o erro padrão dela. Estimado dos
    # dados (ver estimar_pareamento); 0 quando não estimável.
    beta: float = 0.0
    beta_ep: float = 0.0
    beta_pontos: int = 0

    def tau(self, q: int) -> float:
        return math.sqrt(self.por_query[q].tau2)

    def limiar_pct(self, q: int, confianca: float = CONFIANCA_PADRAO) -> float:
        """A variação relativa que uma comparação de UM bloco contra UM bloco
        (a matriz) não distingue de ruído: |log(a/b)| < z·τ·√2."""
        z = NormalDist().inv_cdf(0.5 + confianca / 2)
        return 100.0 * (1.0 - math.exp(-z * self.tau(q) * math.sqrt(2.0)))


PONTOS_MINIMOS_PAREAMENTO = 8


def pareado(b: Bloco) -> bool:
    """O bloco de configuração é de uma validação (sessão curta, com a sua
    linha de base ao lado), e não da matriz (sessão longa, linha de base
    medida no começo, horas antes do bloco de cada índice)."""
    return b.configuracao != VAZIA and b.sessao.startswith("v")


def _ajustado(b: Bloco, gama: Dict[str, float], beta: float = 0.0) -> float:
    """O centro do bloco na escala da sessão típica.

    Linha de base: menos γ da sessão. Bloco de configuração de uma
    validação: menos β·γ, a parte do deslocamento que os dados mostram chegar
    ao bloco ao lado. Bloco de índice da matriz: sem ajuste.
    """
    g = gama.get(b.sessao, 0.0)
    if b.configuracao == VAZIA:
        return b.centro - g
    if pareado(b):
        return b.centro - beta * g
    return b.centro


def estimar_pareamento(blocos: Sequence[Bloco], gama: Dict[str, float]
                       ) -> Tuple[float, float, int]:
    """(β, erro padrão, pontos): quanto do γ da linha de base de uma validação
    aparece no bloco de configuração da mesma validação.

    Para cada (query, configuração) medida em validações de γ diferentes, os
    desvios dos centros em relação à média do grupo contra os desvios dos γ:
    a inclinação sem intercepto é β. Medido no TPC-H com PGTune: 0,57 em 115
    pontos — as validações de um fim de tarde rodaram 13–18% mais lentas que
    as de duas horas antes na linha de base, e os blocos de configuração ao
    lado sentiram cerca de metade disso. β fica em [0, 1]; com poucos pontos,
    0 (sem ajuste, e o γ vira efeito aleatório).
    """
    grupos: Dict[Tuple[int, FrozenSet[int]], List[Bloco]] = {}
    for b in blocos:
        if pareado(b) and not b.censurado and b.sessao in gama:
            grupos.setdefault((b.query, b.configuracao), []).append(b)
    xs: List[float] = []
    ys: List[float] = []
    for bs in grupos.values():
        if len(bs) < 2:
            continue
        g = np.array([gama[b.sessao] for b in bs])
        z = np.array([b.centro for b in bs])
        xs.extend((g - g.mean()).tolist())
        ys.extend((z - z.mean()).tolist())
    x, y = np.array(xs), np.array(ys)
    sxx = float(np.sum(x * x)) if len(x) else 0.0
    if len(x) < PONTOS_MINIMOS_PAREAMENTO or sxx < 1e-6:
        return 0.0, 0.0, len(x)
    beta = float(np.sum(x * y) / sxx)
    res = y - beta * x
    ep = math.sqrt(float(np.sum(res ** 2)) / max(len(x) - 1, 1) / sxx)
    return min(max(beta, 0.0), 1.0), ep, len(x)


def estimar_ruido(ev: Evidencia) -> ModeloRuido:
    """σ²_ε, σ²_α e a variância moderada de um centro de bloco, por query."""
    gama = deslocamentos(ev.blocos)
    # Estimar γ de uma sessão gasta, em média, 1/Q do grau de liberdade de
    # cada query daquela linha de base (ANOVA de dois fatores: (K−1)(Q−1)
    # graus em KQ células). Sem esta correção a variância entre sessões das
    # linhas de base sairia otimista.
    n_q = _queries_por_sessao(ev.blocos)
    q_tipico = float(np.median([n_q[s] for s in gama])) if gama else 0.0
    fator_gl = (1.0 - 1.0 / q_tipico) if gama else 1.0
    beta, beta_ep, beta_pontos = estimar_pareamento(ev.blocos, gama)
    grupos: Dict[Tuple[int, FrozenSet[int]], List[Bloco]] = {}
    for b in ev.blocos:
        grupos.setdefault((b.query, b.configuracao), []).append(b)

    queries = sorted(ev.peso)
    brutos: Dict[int, dict] = {}
    for q in queries:
        ss_entre = gl_entre = 0.0
        desvios_dentro: List[float] = []
        gl_dentro = 0
        reps: List[int] = []
        n_blocos = 0
        for (qq, c), bs in grupos.items():
            if qq != q:
                continue
            uteis = [b for b in bs if not b.censurado]
            for b in uteis:
                lg = b.logs[: len(b.tempos) - b.censuradas]
                if len(lg) >= 2:
                    desvios_dentro.extend(np.abs(lg - np.median(lg)).tolist())
                    gl_dentro += len(lg) - 1
                reps.append(len(b.tempos))
            n_blocos += len(uteis)
            if len(uteis) >= 2:
                z = np.array([_ajustado(b, gama, beta) for b in uteis])
                ss_entre += float(np.sum((z - z.mean()) ** 2))
                ajustados = sum(1 for b in uteis if c == VAZIA and b.sessao in gama)
                gl_entre += (len(uteis) - 1) * (fator_gl if ajustados else 1.0)
        # Escala robusta das repetições: MAD em torno da mediana de cada
        # bloco, corrigida para o desvio em torno de um centro estimado.
        s2_dentro = None
        if desvios_dentro and gl_dentro > 0:
            n_d = len(desvios_dentro)
            mad = 1.4826 * float(np.median(desvios_dentro))
            s2_dentro = (mad ** 2) * n_d / max(gl_dentro, 1)
        brutos[q] = {"ss": ss_entre, "gl": gl_entre, "s2_dentro": s2_dentro,
                     "reps": float(np.median(reps)) if reps else 1.0,
                     "blocos": n_blocos}

    com_gl = [q for q in queries if brutos[q]["gl"] > 0]
    avisos: List[str] = []
    if not com_gl:
        avisos.append("nenhuma query tem bloco repetido em sessões diferentes: "
                      "a variação entre sessões não é estimável")
        por_query = {}
        for q in queries:
            d = brutos[q]
            rq = RuidoQuery(q, d["blocos"], 0.0, None, d["s2_dentro"], d["reps"],
                            tau2=float("nan"), gl=0.0)
            por_query[q] = rq
        return ModeloRuido(por_query, gama, None, None, False, avisos)

    s2 = [brutos[q]["ss"] / brutos[q]["gl"] for q in com_gl]
    gl = [brutos[q]["gl"] for q in com_gl]
    s0_2, d0 = moderar(s2, gl)
    if len(com_gl) < 3:
        avisos.append(f"só {len(com_gl)} query(s) com bloco repetido: d0 fixado "
                      f"em {D0_SEM_DADOS:g}, não estimado")

    por_query = {}
    for q in queries:
        d = brutos[q]
        s2_q = d["ss"] / d["gl"] if d["gl"] > 0 else None
        tau2 = (d0 * s0_2 + d["ss"]) / (d0 + d["gl"])
        rq = RuidoQuery(q, d["blocos"], d["gl"], s2_q, d["s2_dentro"], d["reps"],
                        tau2=tau2, gl=d0 + d["gl"])
        # A variância de um centro não pode ser menor que a parte que vem
        # só das repetições: se a moderação a puxou para baixo disso, o
        # piso é o que as repetições já mostram.
        rq.tau2 = max(rq.tau2, rq.var_dentro_no_centro)
        por_query[q] = rq

    # σ_γ: a variância dos γ estimados, menos o ruído de estimar cada um (a
    # mediana de Q resíduos de variância τ² tem variância ≈ (π/2)τ²/Q).
    sigma_gama = 0.0
    if len(gama) >= 2:
        g = np.array(list(gama.values()))
        tau_tipico = float(np.median([rq.tau2 for rq in por_query.values()]))
        sigma_gama = math.sqrt(max(float(np.var(g, ddof=1))
                                   - (math.pi / 2) * tau_tipico / q_tipico, 0.0))
    elif len(_queries_por_sessao(ev.blocos)) >= 2:
        avisos.append(f"menos de {Q_MINIMO_DESLOCAMENTO} queries por linha de "
                      f"base: o deslocamento comum de sessão não é separado da "
                      f"variação de cada query")
    if gama and beta_pontos < PONTOS_MINIMOS_PAREAMENTO:
        avisos.append(f"pareamento não estimável ({beta_pontos} pontos): os blocos "
                      f"de configuração não são ajustados pelo γ da sessão, e ele "
                      f"entra como efeito aleatório")
    return ModeloRuido(por_query, gama, s0_2, d0, True, avisos, sigma_gama,
                       beta, beta_ep, beta_pontos)


# --------------------------------------------------------------------------
# O tempo de uma configuração, com incerteza
# --------------------------------------------------------------------------

@dataclass
class Termo:
    """T[q, c] em ms: amostras, estimativa pontual, e se é só um piso."""
    amostras: np.ndarray
    ponto: float
    piso: bool                    # T ≥ ponto, sem limite superior
    fracao_uso: float = 0.0       # p: em que fração das sessões o plano mudou
    sessoes: int = 0              # sessões em que a configuração foi medida
    blocos: int = 0               # blocos medidos que entraram
    sem_medida: bool = False      # a query usou a configuração e não tem bloco
    chave: Tuple = ()


class Amostrador:
    """Sorteia T[q, c] para toda query e configuração, de forma reprodutível.

    Cada sorteio é semeado pela (semente, query, configuração), e não pela
    ordem das chamadas: a mesma evidência dá os mesmos números em qualquer
    ordem de consulta.
    """

    def __init__(self, ev: Evidencia, ruido: ModeloRuido,
                 amostras: int = AMOSTRAS_PADRAO, semente: int = SEMENTE_PADRAO):
        self.ev = ev
        self.ruido = ruido
        self.n = amostras
        self.semente = semente
        self._grupos: Dict[Tuple[int, FrozenSet[int]], List[Bloco]] = {}
        for b in ev.blocos:
            self._grupos.setdefault((b.query, b.configuracao), []).append(b)
        self._tau: Dict[int, np.ndarray] = {}
        self._gama: Dict[str, np.ndarray] = {}
        # Grupos (query, configuração) cujas sessões discordam mais do que o
        # ruído da query explica (desvio próprio > 2τ).
        self.dispersos: Set[Tuple[int, FrozenSet[int]]] = set()
        self._mu: Dict[Tuple[int, FrozenSet[int]], Tuple[np.ndarray, float, bool]] = {}
        self._termos: Dict[Tuple[int, FrozenSet[int]], Termo] = {}

    def _rng(self, *chave: int) -> np.random.Generator:
        return np.random.default_rng(np.random.SeedSequence(
            [self.semente] + [int(k) & 0x7FFFFFFF for k in chave]))

    def gama_amostras(self, sessao: str) -> np.ndarray:
        """O deslocamento comum da sessão nos blocos de configuração, um
        sorteio por sessão, o MESMO para todas as queries dela."""
        if sessao not in self._gama:
            import zlib
            rng = self._rng(zlib.crc32(sessao.encode()), 0x7FFFFFF2)
            self._gama[sessao] = self.ruido.sigma_gama * rng.standard_normal(self.n)
        return self._gama[sessao]

    def tau_amostras(self, q: int) -> np.ndarray:
        """σ da query, da posterior: gl·τ²/χ²_gl. Compartilhado por todas as
        configurações da query num mesmo sorteio."""
        if q not in self._tau:
            rq = self.ruido.por_query[q]
            rng = self._rng(q, 0x7FFFFFF0)
            gl = max(rq.gl, 1.0)
            self._tau[q] = np.sqrt(gl * rq.tau2 / rng.chisquare(gl, self.n))
        return self._tau[q]

    def _media(self, q: int, c: FrozenSet[int]) -> Optional[Tuple[np.ndarray, float, bool]]:
        """(amostras de μ, μ pontual, só piso?) do grupo (q, c). None sem bloco."""
        chave = (q, c)
        if chave in self._mu:
            return self._mu[chave]
        blocos = self._grupos.get(chave, [])
        if not blocos:
            return None
        gama = self.ruido.deslocamento
        beta = self.ruido.beta
        medidos = np.array([_ajustado(b, gama, beta) for b in blocos if not b.censurado])
        cens = np.array([_ajustado(b, gama, beta) for b in blocos if b.censurado])
        rng = self._rng(q, *sorted(c), 0x7FFFFFF1)
        tau = self.tau_amostras(q)
        # Variância própria do grupo. O modelo supõe a mesma variância da
        # query em toda configuração; um plano novo pode ser bem mais instável
        # (medido: a Q2 deu −31% e +3% nas duas sessões do mesmo conjunto de
        # 44 MB, uma diferença que o τ da linha de base dela não explica). O
        # grupo usa o MAIOR dos dois: conservador quando o plano oscila, quase
        # neutro quando não.
        if len(medidos) >= 2:
            s_grupo = float(np.std(medidos, ddof=1))
            if s_grupo > 2 * math.sqrt(self.ruido.por_query[q].tau2):
                self.dispersos.add(chave)
            tau = np.maximum(tau, s_grupo)
        if len(medidos) == 0:
            # Só piso: nenhum valor acima do limite é excluído pelos dados.
            piso = float(cens.max())
            res = (np.full(self.n, piso), piso, True)
        elif len(cens) == 0:
            m = float(medidos.mean())
            res = (m + tau * rng.standard_normal(self.n) / math.sqrt(len(medidos)),
                   m, False)
        else:
            res = self._tobit(medidos, cens, tau, rng)
        if c != VAZIA and not res[2]:
            # A linha de base está na escala da sessão típica. O bloco de
            # configuração pareado foi trazido para ela por β̂·γ, e o que
            # sobra de incerteza é a de β; o não pareado (matriz) continua na
            # escala da sessão em que foi medido, e γ entra como sorteio.
            r = self.ruido
            correcoes = []
            for b in blocos:
                if pareado(b) and b.sessao in gama and r.beta_pontos >= PONTOS_MINIMOS_PAREAMENTO:
                    correcoes.append(-(self.beta_amostras() - r.beta) * gama[b.sessao])
                elif r.sigma_gama > 0:
                    correcoes.append(self.gama_amostras(b.sessao))
            if correcoes:
                res = (res[0] + np.mean(correcoes, axis=0), res[1], res[2])
        self._mu[chave] = res
        return res

    def beta_amostras(self) -> np.ndarray:
        """β da posterior aproximada N(β̂, ep²), em [0, 1]; o mesmo sorteio
        para todas as queries e sessões."""
        if not hasattr(self, "_beta"):
            rng = self._rng(0x7FFFFFF3)
            r = self.ruido
            self._beta = np.clip(r.beta + r.beta_ep * rng.standard_normal(self.n), 0, 1)
        return self._beta

    def _tobit(self, medidos: np.ndarray, cens: np.ndarray, tau: np.ndarray,
               rng: np.random.Generator) -> Tuple[np.ndarray, float, bool]:
        """Posterior de μ com censura à direita, prior plano, numa grade.

        log L(μ) = Σ_medidos log φ((z − μ)/τ) + Σ_censurados log Q((c − μ)/τ).
        Com pelo menos um bloco medido a posterior é própria; a grade vai de
        bem abaixo do menor valor a bem acima do maior piso.
        """
        from scipy.special import log_ndtr
        tau_ponto = float(np.median(tau))
        lo = min(medidos.min(), cens.min()) - 8 * tau_ponto
        hi = max(medidos.max(), cens.max()) + 8 * tau_ponto
        grade = np.linspace(lo, hi, PONTOS_GRADE)

        def logl(t: np.ndarray) -> np.ndarray:
            t = t[:, None]
            ll = (-0.5 * ((medidos[None, None, :] - grade[None, :, None]) / t[..., None]) ** 2
                  ).sum(axis=2) - len(medidos) * np.log(t)
            ll += log_ndtr((grade[None, :, None] - cens[None, None, :]) / t[..., None]).sum(axis=2)
            return ll

        ll0 = logl(np.array([tau_ponto]))[0]
        ponto = float(grade[int(np.argmax(ll0))])
        amostras = np.empty(self.n)
        lote = 500
        for i in range(0, self.n, lote):
            t = tau[i:i + lote]
            ll = logl(t)
            ll -= ll.max(axis=1, keepdims=True)
            p = np.exp(ll)
            cdf = np.cumsum(p, axis=1)
            cdf /= cdf[:, -1:]
            u = rng.random(len(t))[:, None]
            idx = (cdf < u).sum(axis=1)
            passo = grade[1] - grade[0]
            amostras[i:i + lote] = grade[np.minimum(idx, len(grade) - 1)] + \
                (rng.random(len(t)) - 0.5) * passo
        return amostras, ponto, False

    def termo(self, q: int, c: FrozenSet[int]) -> Termo:
        """T[q, c]. Com c vazia, a linha de base."""
        chave = (q, c)
        if chave in self._termos:
            return self._termos[chave]
        if c == VAZIA:
            m = self._media(q, VAZIA)
            if m is None:
                t = Termo(np.full(self.n, np.nan), float("nan"), False,
                          sem_medida=True, chave=chave)
            else:
                am, mu, piso = m
                t = Termo(np.exp(am), math.exp(mu), piso,
                          blocos=len(self._grupos.get(chave, [])), chave=chave)
            self._termos[chave] = t
            return t

        base = self.termo(q, VAZIA)
        sessoes = self.ev.sessoes_da(c)
        usou = [s for s in sessoes if self.ev.uso.get((s, c, q))]
        if not sessoes or not usou:
            # A query nunca rodou num plano com índice da configuração: o
            # tempo dela é o da linha de base, e é o MESMO termo — numa
            # comparação, cancela exatamente.
            self._termos[chave] = base
            return base
        m = self._media(q, c)
        if m is None:
            t = Termo(base.amostras, base.ponto, base.piso, sem_medida=True,
                      sessoes=len(sessoes), chave=chave)
            self._termos[chave] = t
            return t
        am, mu, piso = m
        p = len(usou) / len(sessoes)
        novo = np.exp(am)
        if p >= 1.0:
            amostras, ponto, e_piso = novo, math.exp(mu), piso
        else:
            amostras = p * novo + (1 - p) * base.amostras
            ponto = p * math.exp(mu) + (1 - p) * base.ponto
            e_piso = piso or base.piso
        t = Termo(amostras, ponto, e_piso, fracao_uso=p, sessoes=len(sessoes),
                  blocos=len(self._grupos.get(chave, [])), chave=chave)
        self._termos[chave] = t
        return t


# --------------------------------------------------------------------------
# Comparar configurações
# --------------------------------------------------------------------------

@dataclass
class EfeitoQuery:
    query: int
    peso: float
    antes_ms: float               # T[q, a]
    depois_ms: float              # T[q, b]
    ganho_ms: float               # por execução: T_a − T_b
    ganho_ponderado_ms: float     # peso × ganho
    li_ms: float                  # do ganho ponderado
    ls_ms: float
    razao_li: float               # do ganho relativo, log(T_a/T_b)
    razao_ls: float
    mudanca: str                  # 'igual' | 'melhora' | 'piora' | 'indefinida'
    piso_antes: bool = False
    piso_depois: bool = False
    plano_instavel: bool = False
    sem_medida: bool = False
    # As sessões de algum dos lados discordam mais do que o ruído da query
    # explica: o intervalo usa a variância do próprio grupo.
    dispersa: bool = False

    @property
    def ganho_pct(self) -> Optional[float]:
        if not self.antes_ms or math.isnan(self.antes_ms):
            return None
        return 100.0 * self.ganho_ms / self.antes_ms


@dataclass
class Comparacao:
    """ΔB = Σ_q w_q (T[q,a] − T[q,b]): quanto b poupa em relação a a.

    Na unidade do benefício: ms de execução somados sobre a janela do
    corpus (peso = execuções). Positivo, b é melhor.
    """
    a: FrozenSet[int]
    b: FrozenSet[int]
    delta_ms: float
    li_ms: float
    ls_ms: float
    confianca: float
    prob_positivo: float
    por_query: List[EfeitoQuery]
    # Os dois lados de alguma query só têm piso: a diferença dela não é
    # identificável, e o intervalo fica aberto dos dois lados.
    insuficiencias: List[str] = field(default_factory=list)
    amostras: Optional[np.ndarray] = None

    @property
    def aberto_acima(self) -> bool:
        return math.isinf(self.ls_ms)

    @property
    def aberto_abaixo(self) -> bool:
        return math.isinf(self.li_ms)

    def melhoram(self) -> List[EfeitoQuery]:
        return [e for e in self.por_query if e.mudanca == "melhora"]

    def pioram(self) -> List[EfeitoQuery]:
        return [e for e in self.por_query if e.mudanca == "piora"]


def classificar_mudanca(li: float, ls: float) -> str:
    """Mudança de UMA query, pelo intervalo do ganho relativo, contra zero.

    É um teste bilateral de diferença, não de relevância: diz se há evidência
    de que a query mudou, e para que lado. A relevância é da decisão sobre o
    conjunto (as margens), não daqui.
    """
    if li > 0:
        return "melhora"
    if ls < 0:
        return "piora"
    return "indefinida"


def comparar(am: Amostrador, a: FrozenSet[int], b: FrozenSet[int],
             confianca: float = CONFIANCA_PADRAO,
             queries: Optional[Iterable[int]] = None) -> Comparacao:
    """O benefício marginal de ir de a para b, com intervalo."""
    if not am.ruido.suficiente:
        raise ValueError("modelo de ruído insuficiente: sem bloco repetido, "
                         "a variação entre sessões não é estimável")
    alfa = (1 - confianca) / 2
    total = np.zeros(am.n)
    ponto = 0.0
    abre_acima = abre_abaixo = False
    efeitos: List[EfeitoQuery] = []
    insuf: List[str] = []
    for q in sorted(queries if queries is not None else am.ev.peso):
        w = am.ev.peso.get(q, 0.0)
        ta, tb = am.termo(q, a), am.termo(q, b)
        if ta is tb:
            efeitos.append(EfeitoQuery(q, w, ta.ponto, tb.ponto, 0.0, 0.0, 0.0,
                                       0.0, 0.0, 0.0, "igual",
                                       piso_antes=ta.piso, piso_depois=tb.piso))
            continue
        if ta.sem_medida or tb.sem_medida:
            insuf.append(f"query {q}: o plano usou a configuração e o bloco não "
                         f"foi medido (erro); fora da soma")
            efeitos.append(EfeitoQuery(q, w, ta.ponto, tb.ponto, float("nan"),
                                       float("nan"), -math.inf, math.inf,
                                       -math.inf, math.inf, "indefinida",
                                       sem_medida=True))
            continue
        d = w * (ta.amostras - tb.amostras)
        total += d
        ponto += w * (ta.ponto - tb.ponto)
        li, ls = np.quantile(d, [alfa, 1 - alfa])
        r = np.log(ta.amostras) - np.log(tb.amostras)
        rli, rls = np.quantile(r, [alfa, 1 - alfa])
        if ta.piso:
            ls, rls, abre_acima = math.inf, math.inf, True
        if tb.piso:
            li, rli, abre_abaixo = -math.inf, -math.inf, True
        if ta.piso and tb.piso:
            insuf.append(f"query {q}: as duas configurações ≥ timeout; a "
                         f"diferença não é identificável (aumente --timeout)")
        efeitos.append(EfeitoQuery(
            q, w, ta.ponto, tb.ponto, ta.ponto - tb.ponto,
            w * (ta.ponto - tb.ponto), float(li), float(ls), float(rli),
            float(rls), classificar_mudanca(rli, rls), piso_antes=ta.piso,
            piso_depois=tb.piso,
            plano_instavel=(0 < ta.fracao_uso < 1) or (0 < tb.fracao_uso < 1),
            dispersa=bool({(q, a), (q, b)} & am.dispersos)))
    li, ls = np.quantile(total, [alfa, 1 - alfa])
    if abre_acima:
        ls = math.inf
    if abre_abaixo:
        li = -math.inf
    return Comparacao(a, b, ponto, float(li), float(ls), confianca,
                      float(np.mean(total > 0)), efeitos, insuf, total)


# --------------------------------------------------------------------------
# As decisões, cada uma com o seu teste
# --------------------------------------------------------------------------
# Quatro perguntas diferentes, e nenhuma é "não deu significância":
#
#   superioridade     b é melhor que a por MAIS que a margem?   li > +m
#   não inferioridade b não é pior que a por mais que a margem? li > −m
#   equivalência      a diferença está dentro de ±m? (TOST)     −m < li, ls < m
#   mudança           a query mudou, para algum lado?           0 ∉ [li, ls]
#
# Os limites são do intervalo bilateral de confiança `confianca`; cada um é
# um teste unilateral de nível (1 − confianca)/2. A margem é declarada antes
# da comparação (vem dos parâmetros e da configuração de referência, nunca do
# resultado da própria comparação). Ausência de significância NÃO é
# equivalência: sem o intervalo inteiro dentro da margem, a resposta é
# "inconclusiva".

ACEITA, REJEITADA, INCONCLUSIVA = "aceita", "rejeitada", "inconclusiva"


def superioridade(li: float, ls: float, margem: float) -> str:
    """Crescimento: o ganho de b sobre a passa da margem?

    aceita       li > m: ganho relevante, mesmo no pior caso do intervalo
    rejeitada    ls < m: ganho insuficiente, mesmo no melhor caso
    inconclusiva o intervalo cruza a margem: medir mais
    """
    if li > margem:
        return ACEITA
    if ls < margem:
        return REJEITADA
    return INCONCLUSIVA


def nao_inferioridade(li: float, ls: float, margem: float) -> str:
    """Remoção ou troca por menos espaço: b perde no máximo a margem?

    aceita       li > −m: a perda, se houver, é menor que a margem
    rejeitada    ls < −m: a perda é relevante
    inconclusiva o intervalo cruza −m
    """
    if li > -margem:
        return ACEITA
    if ls < -margem:
        return REJEITADA
    return INCONCLUSIVA


def equivalencia(li: float, ls: float, margem: float) -> bool:
    """TOST: as duas configurações são equivalentes dentro de ±m."""
    return -margem < li and ls < margem


def ordenar(li: float, ls: float, margem: float) -> str:
    """Para o relatório: o que se pode dizer entre a e b, sem forçar ordem.

    'equivalentes' pelo TOST: o intervalo inteiro dentro de ±m (pode haver
    diferença detectável, mas ela é menor que o que importa); 'b melhor' /
    'a melhor' quando o intervalo exclui 0 e não cabe na margem; o resto é
    'não ordenável' — e não "empate".
    """
    if equivalencia(li, ls, margem):
        return "equivalentes"
    if li > 0:
        return "b melhor"
    if ls < 0:
        return "a melhor"
    return "não ordenável"


# --------------------------------------------------------------------------
# Pareto
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ponto:
    """Uma configuração nos eixos da fronteira. ``exposicao`` é a contagem
    estática de escritas que podem manter os índices (não é custo medido);
    None quando a carga não tem escrita nas tabelas."""
    chave: FrozenSet[int]
    beneficio_ms: float
    mb: float
    exposicao: Optional[float] = None


def domina(p: Ponto, q: Ponto) -> bool:
    """p domina q pelas estimativas pontuais: não é pior em eixo nenhum e é
    melhor em pelo menos um."""
    ep, eq = p.exposicao or 0.0, q.exposicao or 0.0
    nao_pior = p.beneficio_ms >= q.beneficio_ms and p.mb <= q.mb and ep <= eq
    melhor = p.beneficio_ms > q.beneficio_ms or p.mb < q.mb or ep < eq
    return nao_pior and melhor


def fronteira_pareto(pontos: Sequence[Ponto]) -> List[Ponto]:
    """Os não dominados, em ordem de espaço."""
    return sorted((p for p in pontos if not any(domina(o, p) for o in pontos
                                                if o is not p)),
                  key=lambda p: (p.mb, -p.beneficio_ms))


def dominada_com_evidencia(p: Ponto, q: Ponto, cmp_q_para_p: Comparacao,
                           margem: float) -> bool:
    """q é dominada por p com evidência, e não só pela estimativa pontual.

    p não usa mais espaço nem tem mais exposição, e o intervalo de
    ΔB(q → p) mostra que p não perde mais que a margem para q (não
    inferioridade); com espaço igual, p precisa ser melhor com evidência.
    ``cmp_q_para_p`` é comparar(am, q, p).
    """
    ep, eq = p.exposicao or 0.0, q.exposicao or 0.0
    if p.mb > q.mb or ep > eq:
        return False
    if p.mb < q.mb or ep < eq:
        return nao_inferioridade(cmp_q_para_p.li_ms, cmp_q_para_p.ls_ms,
                                 margem) == ACEITA
    return cmp_q_para_p.li_ms > 0


# --------------------------------------------------------------------------
# Auxiliares para o relatório
# --------------------------------------------------------------------------

def soma_ponderada(am: Amostrador, c: FrozenSet[int],
                   confianca: float = CONFIANCA_PADRAO) -> Tuple[float, float, float, bool]:
    """C(c) = Σ w_q T[q, c]: o tempo total ponderado da carga de leitura na
    configuração. (ponto, li, ls, é piso?)."""
    alfa = (1 - confianca) / 2
    total = np.zeros(am.n)
    ponto = 0.0
    piso = False
    for q, w in am.ev.peso.items():
        t = am.termo(q, c)
        if t.sem_medida and math.isnan(t.ponto):
            continue
        total += w * t.amostras
        ponto += w * t.ponto
        piso = piso or t.piso
    li, ls = np.quantile(total, [alfa, 1 - alfa])
    return ponto, float(li), (math.inf if piso else float(ls)), piso


def flutuacao_sessao(am: Amostrador, c: FrozenSet[int]) -> float:
    """O desvio padrão de C(c) medido numa sessão só: Σ (w_q T_q τ_q)².

    É quanto o total da carga varia de uma sessão para outra sem mudar nada:
    um ganho menor que isso não se distingue, na operação, de um dia bom.
    Termos que são só piso (timeout) ficam fora: a variação de um tempo que
    não foi medido não é conhecida, e o piso × τ inventaria uma.
    """
    soma = 0.0
    for q, w in am.ev.peso.items():
        t = am.termo(q, c)
        if math.isnan(t.ponto) or t.piso:
            continue
        soma += (w * t.ponto * am.ruido.tau(q)) ** 2
    return math.sqrt(soma)
