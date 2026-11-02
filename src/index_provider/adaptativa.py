#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seleção adaptativa de configurações de índices por benefício marginal.

`otimizar` responde "qual o melhor conjunto que cabe em X MB?", e o X é
escolhido antes, sem critério. Aqui a pergunta é outra: **até onde vale a
pena continuar adicionando, removendo ou trocando índices?** A resposta vem de
configurações completas MEDIDAS, comparadas com a incerteza que as sessões de
medida de fato têm, e a exploração para quando o benefício adicional, mesmo
no melhor caso do intervalo, não paga mais.

Quatro responsabilidades, separadas:

    propor      o que medir em seguida. O modelo da matriz (um índice por
                vez, ``otimizacao.avaliar``) só PRIORIZA: a fronteira prevista
                pela varredura de orçamento, e a vizinhança da configuração
                atual — adição, remoção, substituição e combinação. A
                combinação existe porque o modelo do máximo não vê sinergia,
                e o planner pode escolher planos diferentes quando índices
                coexistem: ela entra como exploração, mesmo sem ganho previsto
    medir       ``otimizacao.validar`` com o conjunto inteiro criado, uma
                sessão independente por vez, alternando a ordem dos blocos
    inferir     ``estatistica``: o modelo de ruído com sessão e repetição
                separadas, censura, bootstrap
    decidir     superioridade para crescer, não inferioridade para encolher,
                remedição quando o intervalo cruza a margem, e parada

Nenhum resultado de índice individual é usado como estimativa de uma
configuração: toda decisão compara configurações completas medidas. A matriz
entra em dois papéis, e só neles: proposta (o que vale medir) e, como cada
índice sozinho é ele mesmo uma configuração medida, evidência sobre a
configuração {i}.

A margem
--------
"Ganho relevante" precisa de um número, e ele é declarado antes da comparação
(depende dos parâmetros e da configuração de referência, nunca do resultado
da própria comparação):

    margem(A) = max( ρ · C(A),  σ_C(A) )        (ou --margem-ms, fixa)

    C(A)    o tempo total ponderado da carga de leitura na configuração A:
            Σ_q w_q T[q, A]. Com ρ = 1%, um ganho menor que 1% do que a carga
            gasta hoje não justifica mudar a configuração
    σ_C(A)  quanto C(A) varia de uma sessão para outra sem mudar nada
            (√Σ (w_q T_q τ_q)²): um ganho menor que isso não se distingue, na
            operação, de um dia bom

Espaço e manutenção entram como custo do movimento, na unidade do benefício,
somados à margem: λ ms por MB (padrão 0) e, só quando houver, o custo de
manutenção medido (``custo_manutencao``) ou declarado (``--custo-entrada``).
Sem custo para o espaço, a preferência por menos espaço é lexicográfica: a
configuração maior só substitui a menor se provar ganho relevante.

Resolução de incerteza antes de parar
-------------------------------------
"Não ordenável" não quer dizer "dispensável". Antes de declarar a parada, toda
configuração completa já medida é comparada com a atual, e é remedida a que
cumpre as três condições:

    1. foi medida, e a comparação com a atual não decidiu
    2. a diferença de espaço (e de manutenção, quando medida) pode ser paga
       por um ganho relevante: crescer passa pelo teste de superioridade com
       o custo descontado; encolher só vale a validação se economizar
       ``economia_minima`` do espaço
    3. a incerteza ainda deixa a alternativa competitiva:
           UCB(ΔB) − custo ≥ δ      merece outra sessão
           UCB(ΔB) − custo < δ      descartada: nem o melhor caso paga a margem

Não ordenável pelo zero (o intervalo contém 0) e inconclusiva pela margem são
coisas diferentes: a primeira pode ser descartada pela regra 3. As remedições
seguem a probabilidade de a alternativa vencer, até ``max_validacoes`` por
configuração e ``resolucao`` no experimento. Foi o caso do TPC-H: o conjunto de
293 MB de `otimizar` ficou a +1,8 s [−3,7; +8,3] da recomendação com UMA
sessão, a previsão da matriz não o favorecia, e a caminhada parou sem remedi-lo;
medido de novo, era o melhor.

Multiplicidade
--------------
Cada decisão é um teste unilateral de nível (1 − confiança)/2, sem correção
para o número de comparações, e o procedimento é sequencial (a cada validação
as mesmas comparações são refeitas). O relatório documenta quantas
configurações foram exploradas, quantas comparações foram feitas, a unidade
independente (a sessão), e se as decisões do caminho sobrevivem a Bonferroni
— como sensibilidade, não como regra. O ganho estimado na seleção é otimista
por construção (a recomendada é a que se saiu melhor); quem o estima sem viés
é a confirmação, em sessões que a seleção não leu.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from psycopg.types.json import Jsonb

from . import estatistica as est
from . import evidencia as evd
from . import manutencao as mnt
from . import otimizacao
from .estatistica import VAZIA

MOVIMENTOS = ("fronteira", "adicao", "remocao", "substituicao", "combinacao",
              "medida", "resolucao")


@dataclass
class Parametros:
    confianca: float = est.CONFIANCA_PADRAO
    relevancia: float = 0.01            # ρ: fração de C(A)
    margem_ms: Optional[float] = None   # margem absoluta, no lugar da relativa
    lambda_mb: float = 0.0              # ms de benefício que um MB precisa pagar
    custo_entrada_ms: Optional[float] = None  # hipótese do DBA: ms por entrada de índice
    paciencia: int = 2                  # crescimentos fracassados seguidos (rejeitados ou não ordenáveis) para parar
    # Aceitar pede pelo menos min_sessoes sessões da candidata, uma delas
    # validação do conjunto inteiro: uma sessão só não separa efeito de um
    # plano que o planner escolheu só naquela sessão.
    min_sessoes: int = 2
    max_validacoes: int = 3             # sessões de seleção por configuração
    # Remoção ou troca sem motivo medido só vale uma validação se economizar
    # pelo menos esta fração do espaço da configuração atual. É o custo do
    # experimento, não do índice: 11 min de bancada para economizar 1 MB de
    # 44 não se paga.
    economia_minima: float = 0.10
    max_medicoes: int = 6               # validações por execução de --executar
    exploracao: int = 2                 # candidatas sem ganho previsto testadas antes de parar
    # Resolução de incerteza: validações, no experimento inteiro, para
    # remedir antes de parar alternativas já medidas que ficaram
    # inconclusivas contra a atual e ainda podem passar da margem.
    resolucao: int = 3
    vizinhos: int = 4                   # candidatas por tipo de movimento
    amostras: int = est.AMOSTRAS_PADRAO
    semente: int = est.SEMENTE_PADRAO
    peso: str = "execucoes"
    modelo: str = "conservador"         # o modelo de interação da PROPOSTA

    def como_dict(self) -> dict:
        return asdict(self)

    def modelo_otim(self) -> otimizacao.Parametros:
        return otimizacao.Parametros(modelo=self.modelo, peso=self.peso)


# --------------------------------------------------------------------------
# A análise: evidência + ruído + amostrador, e o que se deriva deles
# --------------------------------------------------------------------------

@dataclass
class Analise:
    exp: evd.Experimento
    ev: est.Evidencia
    ruido: est.ModeloRuido
    am: est.Amostrador
    par: Parametros
    exposicoes: Dict[FrozenSet[int], mnt.Exposicao] = field(default_factory=dict)
    custos_medidos: Dict[str, float] = field(default_factory=dict)
    escritas: List[mnt.Escrita] = field(default_factory=list)
    # tabela -> colunas de que os índices já existentes dependem
    existentes: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    # índice -> (tabela, colunas de que ele depende)
    deps: Dict[int, Tuple[Optional[str], FrozenSet[str]]] = field(default_factory=dict)
    _cmp: Dict[Tuple[FrozenSet[int], FrozenSet[int]], est.Comparacao] = field(default_factory=dict)
    _carga: Dict[FrozenSet[int], Tuple[float, float, float, bool]] = field(default_factory=dict)

    def sessoes(self, c: FrozenSet[int]) -> int:
        return 0 if c == VAZIA else len(self.ev.sessoes_da(c))

    def comparar(self, a: FrozenSet[int], b: FrozenSet[int]) -> est.Comparacao:
        if (a, b) not in self._cmp:
            self._cmp[(a, b)] = est.comparar(self.am, a, b, self.par.confianca)
        return self._cmp[(a, b)]

    def carga(self, c: FrozenSet[int]) -> Tuple[float, float, float, bool]:
        if c not in self._carga:
            self._carga[c] = est.soma_ponderada(self.am, c, self.par.confianca)
        return self._carga[c]

    def margem(self, a: FrozenSet[int]) -> Tuple[float, str]:
        """(margem de relevância em ms, de onde veio)."""
        if self.par.margem_ms is not None:
            return self.par.margem_ms, "declarada (--margem-ms)"
        c_a = self.carga(a)[0]
        rel = self.par.relevancia * c_a
        flut = est.flutuacao_sessao(self.am, a)
        if flut > rel:
            return flut, f"flutuação entre sessões de C(A) (> {self.par.relevancia:.0%} de C(A))"
        return rel, f"{self.par.relevancia:.0%} de C(A) = {c_a / 1000:,.1f} s"

    def exposicao(self, c: FrozenSet[int]) -> Optional[mnt.Exposicao]:
        return self.exposicoes.get(c)

    def custo(self, a: FrozenSet[int], b: FrozenSet[int]) -> Tuple[float, List[str]]:
        """O custo do movimento a → b, em ms de benefício, e o que dele não
        é conhecido. Espaço por λ; manutenção só se medida ou declarada."""
        mb = self.exp.mb(b) - self.exp.mb(a)
        custo = self.par.lambda_mb * mb
        notas: List[str] = []
        ea, eb = exposicao_de(self, a), exposicao_de(self, b)
        if ea.tem_escrita or eb.tem_escrita:
            medido_a = self._custo_medido(a)
            medido_b = self._custo_medido(b)
            if medido_a is not None and medido_b is not None:
                custo += medido_b - medido_a
                notas.append("manutenção: custo medido (custo_manutencao)")
            elif self.par.custo_entrada_ms is not None:
                custo += self.par.custo_entrada_ms * (eb.maximo - ea.maximo)
                notas.append(f"manutenção: HIPÓTESE declarada de "
                             f"{self.par.custo_entrada_ms:g} ms por entrada, "
                             f"sobre a exposição máxima")
            else:
                notas.append("manutenção não medida: só a leitura foi avaliada")
        return custo, notas

    def _custo_medido(self, c: FrozenSet[int]) -> Optional[float]:
        if not c:
            return 0.0
        chaves = {i: self.exp.inst.indices[i].chave for i in c}
        tabela = {i: (self.deps.get(i, (None,))[0] or "") for i in c}
        por_tabela: Dict[str, float] = {}
        for e in self.escritas:
            if e.tipo != "DELETE":
                por_tabela[e.tabela] = por_tabela.get(e.tabela, 0.0) + e.execucoes
        total, faltam = mnt.custo_medido(self.custos_medidos, chaves, c,
                                         por_tabela, tabela)
        return total if not faltam else None

    def indices_usados(self, c: FrozenSet[int]) -> Dict[int, Set[int]]:
        """índice -> queries cujo plano o citou, nas sessões de seleção de c."""
        saida: Dict[int, Set[int]] = {i: set() for i in c}
        if len(c) == 1 and any(s.startswith("m") for s in self.ev.sessoes_da(c)):
            (i,) = tuple(c)
            for (s, cc, q), usou in self.ev.uso.items():
                if cc == c and usou:
                    saida[i].add(q)
        for v in self.exp.validacoes_de(c):
            for q, linhas in self.exp._medidas.get(v.sessao, {}).items():
                for fase, rep, _t, _c, erro, usados in linhas:
                    if fase == "depois" and rep == 0 and usados:
                        for i in usados:
                            if i in saida:
                                saida[i].add(q)
        return saida


def analisar(exp: evd.Experimento, par: Parametros,
             escritas: Sequence[mnt.Escrita] = (),
             existentes: Optional[Dict[str, FrozenSet[str]]] = None,
             colunas: Optional[Dict[str, List[str]]] = None,
             custos_medidos: Optional[Dict[str, float]] = None,
             ev: Optional[est.Evidencia] = None,
             ruido: Optional[est.ModeloRuido] = None) -> Analise:
    """Monta a análise de seleção. ``colunas`` (o catálogo do banco
    observado, quando acessível) melhora a leitura das dependências de cada
    índice; sem ele, uma lista de palavras que não são coluna faz o papel."""
    ev = ev or exp.evidencia()
    ruido = ruido or est.estimar_ruido(ev)
    am = est.Amostrador(ev, ruido, par.amostras, par.semente)
    deps = {i: mnt.dependencias_do_indice(
        ind.ddl, (colunas or {}).get((ind.relacao or "").lower()))
        for i, ind in exp.inst.indices.items()}
    return Analise(exp, ev, ruido, am, par, custos_medidos=custos_medidos or {},
                   escritas=list(escritas), existentes=existentes or {}, deps=deps)


def exposicao_de(an: Analise, c: FrozenSet[int]) -> mnt.Exposicao:
    if c not in an.exposicoes:
        obs = None
        amb_ref = an.exp.ambiente_matriz or next(
            (v.ambiente for v in an.exp.validacoes if v.ambiente), None)
        if amb_ref:
            obs = amb_ref.get("escrita_observada")
        an.exposicoes[c] = mnt.exposicao(an.deps, c, an.escritas,
                                         an.existentes, obs)
    return an.exposicoes[c]


# --------------------------------------------------------------------------
# Propor
# --------------------------------------------------------------------------

@dataclass
class Candidata:
    chave: FrozenSet[int]
    movimento: str
    origem: FrozenSet[int]
    previsto_ms: float          # ΔB previsto pelo modelo da matriz sobre a origem
    motivo: str
    exploratoria: bool = False
    medida: bool = False        # o motivo vem de medida (regressão, índice sem uso)


def e_exploracao(cf: evd.Configuracao) -> bool:
    """A configuração foi medida por uma exploração (combinação) de `adaptar`."""
    return any("combinacao" in o for o in cf.origens)


def exploracoes_medidas(an: "Analise") -> int:
    """Quantas combinações exploratórias o experimento já mediu. O orçamento
    de exploração é do experimento, não de uma caminhada: a caminhada
    recomeça a cada medição, e contar só dentro dela deixaria medir uma
    combinação nova a cada volta."""
    return sum(1 for c, cf in an.exp.configuracoes.items()
               if c and e_exploracao(cf) and an.exp.validacoes_de(c))


def resolucoes_medidas(an: "Analise") -> int:
    """Validações de seleção que a resolução de incerteza já pediu, no
    experimento inteiro (pelo mesmo motivo do orçamento de exploração)."""
    return sum(1 for v in an.exp.validacoes
               if v.movimento == "resolucao" and an.exp._entra(v, evd.SELECAO))


def _coluna_lider(ind: otimizacao.Indice) -> Optional[str]:
    m = re.search(r"\(\s*\(*\s*\"?([A-Za-z_][\w]*)", ind.curto)
    return m.group(1).lower() if m else None


def variantes(a: otimizacao.Indice, b: otimizacao.Indice) -> bool:
    """Dois índices da mesma tabela com a mesma coluna líder."""
    return (bool(a.relacao) and a.relacao == b.relacao
            and _coluna_lider(a) is not None and _coluna_lider(a) == _coluna_lider(b))


class Propositor:
    """Gera as candidatas a partir da configuração atual. O modelo da matriz
    só ordena; quem decide é a medida."""

    def __init__(self, an: Analise):
        self.an = an
        self.inst = an.exp.inst
        self.par_modelo = an.par.modelo_otim()
        self._bp: Dict[FrozenSet[int], float] = {}
        self.fronteira = self._fronteira()

    def previsto(self, c: FrozenSet[int]) -> float:
        if c not in self._bp:
            self._bp[c] = otimizacao.avaliar(self.inst, self.par_modelo,
                                             sorted(c)).beneficio_ms
        return self._bp[c]

    def _fronteira(self, maximo: int = 60) -> List[FrozenSet[int]]:
        """A fronteira prevista (benefício × espaço) pela varredura de
        orçamento: resolve sem limite, depois logo abaixo do espaço da
        solução, e assim por diante. Cada ponto é o ótimo do MODELO para
        aquele espaço; é a ordem de prioridade, não o espaço de busca."""
        pontos: List[FrozenSet[int]] = []
        orc = None
        for _ in range(maximo):
            par = replace(self.par_modelo, orcamento_mb=orc)
            try:
                av, _status = otimizacao.resolver(self.inst, par)
            except RuntimeError as erro:
                self.an.ruido.avisos.append(f"varredura da fronteira prevista "
                                            f"parou em {orc} MB: {erro}")
                break
            if not av.indices:
                break
            c = frozenset(av.indices)
            if c in pontos:
                break
            pontos.append(c)
            self._bp[c] = av.beneficio_ms
            # 1 KB abaixo: índice ocupa múltiplos de 8 KB, e 1e-6 MB cai
            # dentro da tolerância de viabilidade do HiGHS (erro de solução).
            orc = av.mb - 1e-3
        return sorted(pontos, key=lambda c: self.an.exp.mb(c))

    def _usa(self, i: int) -> Set[int]:
        return {q for (q, j) in self.inst.delta if j == i}

    def candidatas(self, a: FrozenSet[int], margem: float) -> List[Candidata]:
        an, inst = self.an, self.inst
        k = an.par.vizinhos
        base = self.previsto(a)
        saida: Dict[FrozenSet[int], Candidata] = {}

        def por(c: FrozenSet[int], mov: str, motivo: str, explor: bool = False,
                medida: bool = False) -> None:
            if c == a or c in saida:
                return
            saida[c] = Candidata(c, mov, a, self.previsto(c) - base, motivo,
                                 explor, medida)

        # O que já foi medido (por `otimizar`, por uma rodada de clusters, por
        # outra adaptação) é candidata de graça: a evidência já está paga. A
        # combinação que uma exploração anterior mediu continua sendo
        # exploração (é o que conta o orçamento de exploração).
        for c, cf in an.exp.configuracoes.items():
            if c and cf.otimizacoes and an.exp.validacoes_de(c):
                por(c, "medida", f"já medida ({cf.rotulo})",
                    explor=e_exploracao(cf))
        for c in self.fronteira:
            por(c, "fronteira", f"ponto da fronteira prevista, "
                                f"{an.exp.mb(c):,.1f} MB")

        fora = [j for j in inst.indices if j not in a]
        adicoes = sorted(((self.previsto(a | {j}) - base, j) for j in fora),
                         reverse=True)
        for ganho, j in adicoes[:k]:
            if ganho > 0:
                por(a | {j}, "adicao", f"+i{j} {inst.indices[j].curto}")

        # Remoção: só com motivo. Medido (sem uso, regressão) ou previsto
        # (o índice contribui menos que a margem).
        if a:
            usados = an.indices_usados(a) if an.sessoes(a) else {}
            regressoes: Dict[int, List[int]] = {}
            custo_regressao: Dict[int, float] = {}
            if an.sessoes(a):
                for e in an.comparar(VAZIA, a).pioram():
                    for i, qs in usados.items():
                        if e.query in qs:
                            regressoes.setdefault(i, []).append(e.query)
                            custo_regressao[i] = (custo_regressao.get(i, 0.0)
                                                  + abs(e.ganho_ponderado_ms))
            for i in sorted(a, key=lambda i: -inst.indices[i].mb):
                sem = a - {i}
                perda = base - self.previsto(sem)
                if usados and not usados.get(i):
                    por(sem, "remocao", f"−i{i}: nenhuma query usou o índice "
                                        f"nas validações", medida=True)
                elif i in regressoes and perda < custo_regressao[i]:
                    # Só se o que a remoção perde (previsto) é menor que a
                    # regressão que ela corrige (medida). Sem esta triagem a
                    # caminhada mediu tirar lineitem(l_partkey) por uma piora
                    # da Q9 — e perderia Q17 e Q20 inteiras.
                    por(sem, "remocao", f"−i{i}: plano usou o índice nas "
                                        f"queries que pioraram "
                                        f"{sorted(regressoes[i])} "
                                        f"({custo_regressao[i] / 1000:,.1f} s; perda "
                                        f"prevista {perda / 1000:,.1f} s)", medida=True)
                elif perda < margem:
                    por(sem, "remocao", f"−i{i}: contribuição prevista "
                                        f"{perda / 1000:,.1f} s < margem")

        # Substituição: trocar um índice por outro da mesma tabela que atende
        # alguma query em comum. Cobre o simples × composto.
        trocas = []
        for i in a:
            qi = self._usa(i)
            for j in fora:
                if inst.indices[j].relacao != inst.indices[i].relacao:
                    continue
                if not (qi & self._usa(j)):
                    continue
                c = (a - {i}) | {j}
                trocas.append((self.previsto(c) - base, i, j, c))
        for ganho, i, j, c in sorted(trocas, key=lambda t: -t[0])[:k]:
            por(c, "substituicao", f"i{i} → i{j} {inst.indices[j].curto}")
        for ganho, i, j, c in sorted(trocas, key=lambda t: an.exp.mb(t[3]))[:k]:
            por(c, "substituicao", f"i{i} → i{j} {inst.indices[j].curto}")

        # Combinação: pares fora de A que a mesma query usa, e que o modelo
        # do máximo não favorece. O que está em jogo é o tempo das queries em
        # comum na configuração atual.
        pares = []
        for x in range(len(fora)):
            for y in range(x + 1, len(fora)):
                j, l = fora[x], fora[y]
                comuns = self._usa(j) & self._usa(l)
                if not comuns:
                    continue
                if variantes(inst.indices[j], inst.indices[l]):
                    # Mesma tabela, mesma coluna líder: competem pela mesma
                    # chave (orders(o_orderdate) INCLUDE em duas ordens, das
                    # rodadas 1 e 2), não se combinam.
                    continue
                c = a | {j, l}
                if self.previsto(c) - base >= margem:
                    continue                    # o modelo já favorece: não é exploração
                em_jogo = sum(an.ev.peso.get(q, 0) * an.am.termo(q, a).ponto
                              for q in comuns if not math.isnan(an.am.termo(q, a).ponto))
                pares.append((em_jogo, j, l, c, comuns))
        for em_jogo, j, l, c, comuns in sorted(pares, key=lambda t: -t[0])[:k]:
            por(c, "combinacao", f"+i{j}+i{l}: usados juntos por "
                                 f"{sorted(comuns)}, {em_jogo / 1000:,.1f} s em jogo",
                explor=True)
        return list(saida.values())


# --------------------------------------------------------------------------
# Decidir
# --------------------------------------------------------------------------

@dataclass
class Passo:
    tipo: str                   # 'comparacao' | 'medicao' | 'parada'
    movimento: Optional[str] = None
    de: Optional[FrozenSet[int]] = None
    para: Optional[FrozenSet[int]] = None
    teste: Optional[str] = None
    decisao: Optional[str] = None
    delta_ms: Optional[float] = None
    li_ms: Optional[float] = None
    ls_ms: Optional[float] = None
    margem_ms: Optional[float] = None
    custo_ms: Optional[float] = None
    delta_mb: Optional[float] = None
    previsto_ms: Optional[float] = None
    texto: str = ""
    detalhe: dict = field(default_factory=dict)


@dataclass
class Medicao:
    """A próxima validação que a decisão pede."""
    configuracao: FrozenSet[int]
    motivo: str
    ordem: str
    movimento: str


@dataclass
class Trajetoria:
    atual: FrozenSet[int]
    caminho: List[FrozenSet[int]]
    passos: List[Passo]
    proxima: Optional[Medicao]
    motivo: str
    concluida: bool
    examinadas: List[FrozenSet[int]]
    nao_ordenaveis: List[Tuple[FrozenSet[int], FrozenSet[int]]] = field(default_factory=list)
    # A última resolução de incerteza: cada alternativa medida contra a atual
    # e o que se fez com ela. E quantas comparações as resoluções fizeram ao
    # todo (para a contagem de multiplicidade).
    pendencias: List["Pendencia"] = field(default_factory=list)
    comparacoes_resolucao: int = 0


@dataclass
class Pendencia:
    """Uma alternativa já medida, comparada com a configuração atual no
    momento de parar, e o que a resolução de incerteza fez com ela."""
    chave: FrozenSet[int]
    teste: str
    decisao: str
    delta_ms: float
    li_ms: float
    ls_ms: float
    margem_ms: float
    custo_ms: float
    delta_mb: float
    # P(ΔB − custo passa do limiar do teste), pelas amostras do bootstrap:
    # só ordena as remedições, não decide nada
    prob_vencer: Optional[float]
    sessoes: Tuple[int, int]            # (atual, alternativa)
    # 'remedir' | 'aceitar' | 'descartada' | 'sem_orcamento' | 'nao_resolve'
    acao: str
    motivo: str
    alvo: Optional[FrozenSet[int]] = None   # quem medir, quando 'remedir'


def ordem_da_validacao(an: Analise, c: FrozenSet[int]) -> str:
    """Alterna a ordem dos blocos entre as validações de uma configuração."""
    n = len([v for v in an.exp.validacoes if v.configuracao == c])
    return "base_primeiro" if n % 2 == 0 else "conjunto_primeiro"


def sessoes_para_aceitar(an: Analise, c: FrozenSet[int]) -> bool:
    sess = an.ev.sessoes_da(c)
    return (len(sess) >= an.par.min_sessoes
            and any(s.startswith("v") for s in sess))


def decidir_movimento(an: Analise, a: FrozenSet[int], b: FrozenSet[int]
                      ) -> Tuple[str, str, est.Comparacao, float, float, List[str]]:
    """(teste, decisão, comparação, margem, custo, notas) de a → b."""
    cmp = an.comparar(a, b)
    margem, _fonte = an.margem(a)
    custo, notas = an.custo(a, b)
    li, ls = cmp.li_ms - custo, cmp.ls_ms - custo
    if an.exp.mb(b) >= an.exp.mb(a):
        return "superioridade", est.superioridade(li, ls, margem), cmp, margem, custo, notas
    return "nao_inferioridade", est.nao_inferioridade(li, ls, margem), cmp, margem, custo, notas


def pendencias(an: Analise, atual: FrozenSet[int],
               fora: Set[FrozenSet[int]] = frozenset()) -> List[Pendencia]:
    """Cada configuração completa já medida, contra a atual, com o que a
    resolução de incerteza faz com ela (ver o cabeçalho do módulo):

      aceitar        a comparação já decide, com sessões suficientes
      descartada     rejeitada (UCB(ΔB) − custo < δ, ou perda relevante), ou
                     compacta demais para a economia pagar uma validação
      remedir        inconclusiva e ainda competitiva: outra sessão de quem
                     tem menos, até ``max_validacoes``
      sem_orcamento  inconclusiva, e as duas já têm ``max_validacoes``
      nao_resolve    o intervalo é aberto dos dois lados (timeout nas duas
                     configurações): outra sessão não muda isso

    A comparação é a da caminhada: configurações completas, cada uma contra
    a linha de base da sua própria sessão, e a diferença entre as duas com o
    deslocamento de sessão pareado (β) e o resto como variância. Nunca
    intervalos de cada uma, lado a lado. ``fora``: as que já estão no
    caminho (a decisão entre elas já foi tomada)."""
    par = an.par
    mb_a = an.exp.mb(atual)
    saida: List[Pendencia] = []
    for c in sorted(an.exp.configuracoes, key=lambda c: (an.exp.mb(c), sorted(c))):
        if c == atual or c == VAZIA or c in fora or an.sessoes(c) == 0:
            continue
        teste, dec, cmp, margem, custo, _notas = decidir_movimento(an, atual, c)
        limiar = margem if teste == "superioridade" else -margem
        prob = (None if cmp.amostras is None
                else float(((cmp.amostras - custo) > limiar).mean()))
        ses = (an.sessoes(atual), an.sessoes(c))

        def anota(acao: str, motivo: str, alvo: Optional[FrozenSet[int]] = None) -> None:
            saida.append(Pendencia(c, teste, dec, cmp.delta_ms, cmp.li_ms, cmp.ls_ms,
                                   margem, custo, an.exp.mb(c) - mb_a, prob, ses,
                                   acao, motivo, alvo))

        if dec == est.REJEITADA:
            anota("descartada",
                  "UCB(ΔB) − custo < δ: nem o melhor caso do intervalo paga a margem"
                  if teste == "superioridade" else
                  "perda relevante mesmo no melhor caso do intervalo")
            continue
        if dec == est.ACEITA and sessoes_para_aceitar(an, c):
            anota("aceitar", f"{teste} decide sem nova medida")
            continue
        economia = mb_a - an.exp.mb(c)
        if teste == "nao_inferioridade" and economia < par.economia_minima * mb_a:
            anota("descartada",
                  f"economiza {economia:,.1f} MB, menos que {par.economia_minima:.0%} "
                  f"de {mb_a:,.1f} MB: provar a não inferioridade não paga a validação")
            continue
        if math.isinf(cmp.li_ms) and math.isinf(cmp.ls_ms):
            anota("nao_resolve", "intervalo aberto dos dois lados (as duas no timeout "
                                 "em alguma query): outra sessão não resolve; aumente "
                                 "--timeout")
            continue
        if dec == est.ACEITA:
            # O intervalo já diz "aceita", com sessões de menos para aceitar:
            # quem precisa de sessão é a alternativa.
            opcoes = [c] if an.sessoes(c) < par.max_validacoes else []
        else:
            opcoes = [x for x in (c, atual)
                      if x != VAZIA and an.sessoes(x) < par.max_validacoes]
        if not opcoes:
            anota("sem_orcamento", f"inconclusiva com {ses[0]} e {ses[1]} sessões: "
                                   f"limite de {par.max_validacoes} por configuração")
            continue
        alvo = min(opcoes, key=lambda x: (an.sessoes(x), x != c))
        if dec == est.ACEITA:
            anota("remedir", f"o intervalo já passa da margem, com sessões de menos "
                             f"para aceitar: nova sessão de {_c(alvo)}", alvo)
        else:
            anota("remedir", f"inconclusiva ({teste}) e ainda competitiva: "
                             f"UCB − custo = {_s(cmp.ls_ms - custo)} s ≥ "
                             f"{_s(limiar)} s; nova sessão de {_c(alvo)}", alvo)
    # Aceitar: a compacta primeiro, depois a de menos espaço (a preferência
    # lexicográfica). Remedir: a mesma regra de eficiência do crescimento,
    # com a probabilidade de vencer no lugar do ganho previsto: P(vencer) por
    # MB alterado, com 1 MB de piso. Pela preferência por menos espaço,
    # resolver primeiro a alternativa mais perto da atual pode tornar as
    # maiores desnecessárias (se ela for aceita, as outras passam a ser
    # comparadas com ela), e o contrário não acontece. No TPC-H, P(vencer)
    # sozinha poria o conjunto de 795 MB (30%) antes do de 293 MB (25%).
    ordem = {"aceitar": 0, "remedir": 1, "sem_orcamento": 2, "nao_resolve": 3,
             "descartada": 4}

    def chave(p: Pendencia):
        if p.acao == "aceitar":
            return (0, p.teste != "nao_inferioridade", an.exp.mb(p.chave))
        return (ordem[p.acao], 0,
                -(p.prob_vencer or 0.0) / max(abs(p.delta_mb), 1.0),
                an.exp.mb(p.chave))
    saida.sort(key=chave)
    return saida


def _passo_resolucao(atual: FrozenSet[int], p: Pendencia) -> Passo:
    decisao = {"aceitar": est.ACEITA, "remedir": est.INCONCLUSIVA,
               "descartada": p.decisao}.get(p.acao, "nao_ordenavel")
    return Passo("comparacao", "resolucao", atual, p.chave, p.teste, decisao,
                 p.delta_ms, p.li_ms, p.ls_ms, p.margem_ms, p.custo_ms, p.delta_mb,
                 None, f"resolução de incerteza: {p.motivo}",
                 {"sessoes": list(p.sessoes), "prob_vencer": p.prob_vencer,
                  "acao": p.acao})


def caminhar(an: Analise, prop: Optional[Propositor] = None,
             limite_passos: int = 200) -> Trajetoria:
    """A exploração inteira, a partir da linha de base, sobre a evidência que
    existe. Função pura da evidência: rodar de novo dá o mesmo caminho.

    Ordem de prioridade das candidatas, a cada passo:

      1. correção: remoção ou troca por menos espaço com motivo MEDIDO
         (índice sem uso, índice no plano de uma query que piorou)
      2. parcimônia: remoção ou troca por menos espaço com perda prevista
         menor que a margem
      3. crescimento: a de menor espaço entre as que o modelo prevê acima da
         margem
      4. exploração: até ``exploracao`` combinações que o modelo não favorece

    Parada: ``paciencia`` crescimentos fracassados seguidos — rejeitados (o
    ganho, mesmo no melhor caso do intervalo, ficou abaixo da margem) ou não
    ordenáveis (as sessões se esgotaram sem provar o ganho) —, ou nenhuma
    candidata restante nos quatro grupos. Decisão inconclusiva pede nova sessão de
    quem tem menos (até ``max_validacoes``); esgotado, o par fica "não
    ordenável" e a configuração atual se mantém — o ônus da prova é de quem
    pede mais espaço.

    Antes de parar, a resolução de incerteza (``pendencias``): uma
    alternativa já medida que decide sem medir muda a atual e a exploração
    continua dela; uma inconclusiva e competitiva pede nova sessão (até
    ``resolucao`` no experimento); só então a caminhada para.
    """
    par = an.par
    prop = prop or Propositor(an)
    atual: FrozenSet[int] = VAZIA
    caminho = [VAZIA]
    passos: List[Passo] = []
    examinadas: Set[FrozenSet[int]] = {VAZIA}
    ordem_exam: List[FrozenSet[int]] = []
    nao_ord: List[Tuple[FrozenSet[int], FrozenSet[int]]] = []
    rejeicoes = 0
    pend: List[Pendencia] = []
    n_resolucao = 0

    def fim(proxima: Optional[Medicao], motivo: str, concluida: bool) -> Trajetoria:
        return Trajetoria(atual, caminho, passos, proxima, motivo, concluida,
                          ordem_exam, nao_ord, pend, n_resolucao)

    def resolver() -> Tuple[str, object]:
        """('mover', configuração) | ('medir', Medicao) | ('parar', None)."""
        nonlocal pend, n_resolucao
        pend = pendencias(an, atual, set(caminho))
        n_resolucao += len(pend)
        aceitas = [p for p in pend if p.acao == "aceitar"]
        if aceitas:
            passos.append(_passo_resolucao(atual, aceitas[0]))
            examinadas.add(aceitas[0].chave)
            ordem_exam.append(aceitas[0].chave)
            return "mover", aceitas[0].chave
        remedir = [p for p in pend if p.acao == "remedir"]
        usadas = resolucoes_medidas(an)
        if remedir and usadas < par.resolucao:
            p = remedir[0]
            passos.append(_passo_resolucao(atual, p))
            passos.append(Passo("medicao", "resolucao", atual, p.alvo,
                                texto=f"resolução de incerteza ({usadas + 1} de "
                                      f"{par.resolucao}): {_c(p.chave)} contra "
                                      f"{_c(atual)}"))
            return "medir", Medicao(p.alvo, f"resolução de incerteza: {_c(p.chave)} "
                                            f"contra {_c(atual)}, {p.motivo}",
                                    ordem_da_validacao(an, p.alvo), "resolucao")
        for p in remedir:
            p.acao = "sem_orcamento"
            p.motivo = (f"ainda competitiva e sem decisão, mas o orçamento de "
                        f"resolução acabou ({usadas} de {par.resolucao} validações)")
        for p in pend:
            if p.acao in ("sem_orcamento", "nao_resolve"):
                passos.append(_passo_resolucao(atual, p))
                if (atual, p.chave) not in nao_ord:
                    nao_ord.append((atual, p.chave))
        return "parar", None

    if not an.ruido.suficiente:
        # Sem bloco repetido não há variação entre sessões estimada: nenhuma
        # comparação é possível, por maior que pareça a diferença.
        alvo = next((c for c in prop.fronteira), None)
        medidas = [c for c in an.exp.configuracoes
                   if c != VAZIA and an.sessoes(c) > 0]
        alvo = medidas[0] if medidas else alvo
        if alvo is None:
            return Trajetoria(atual, caminho, passos, None,
                              "nenhuma candidata: a matriz não tem índice útil",
                              True, [])
        return Trajetoria(atual, caminho, passos,
                          Medicao(alvo, "variação entre sessões desconhecida: "
                                  "uma validação dá a segunda linha de base",
                                  ordem_da_validacao(an, alvo), "fronteira"),
                          "aguardando medição: " + "; ".join(an.ruido.avisos),
                          False, [])

    for _ in range(limite_passos):
        margem, fonte = an.margem(atual)
        cands = [c for c in prop.candidatas(atual, margem)
                 if c.chave not in examinadas]
        mb_a = an.exp.mb(atual)
        menores = [c for c in cands if an.exp.mb(c.chave) < mb_a]

        def economiza(c: Candidata) -> bool:
            return (mb_a - an.exp.mb(c.chave)) >= par.economia_minima * mb_a

        def pronta(c: Candidata) -> int:
            """0 se a evidência que existe já basta para decidir."""
            return 0 if sessoes_para_aceitar(an, c.chave) else 1

        # Regressão medida corrige sempre; índice sem uso, só se a economia
        # pagar a medição (ou se a evidência já existe).
        correcao = sorted((c for c in menores if c.medida
                           and ("piora" in c.motivo or "pioraram" in c.motivo
                                or economiza(c) or not pronta(c))),
                          key=lambda c: (pronta(c), an.exp.mb(c.chave)))
        parcimonia = sorted((c for c in menores if not c.medida
                             and -c.previsto_ms < margem
                             and (economiza(c) or not pronta(c))),
                            key=lambda c: (pronta(c), an.exp.mb(c.chave)))
        # Crescimento: primeiro o que já se pode decidir sem medir; depois a
        # maior eficiência prevista (ΔB/ΔMB, com 1 MB de piso no denominador
        # para índices minúsculos não dominarem por divisão).
        crescimento = sorted((c for c in cands if an.exp.mb(c.chave) >= mb_a
                              and not c.exploratoria and c.previsto_ms >= margem),
                             key=lambda c: (pronta(c),
                                            -c.previsto_ms / max(an.exp.mb(c.chave) - mb_a, 1.0),
                                            an.exp.mb(c.chave)))
        # Exploração: a já medida se compara de graça; uma nova só enquanto o
        # experimento não gastou o orçamento.
        gastas = exploracoes_medidas(an)
        exploracao = sorted((c for c in cands if c.exploratoria
                             and (an.sessoes(c.chave) > 0 or gastas < par.exploracao)),
                            key=lambda c: (an.sessoes(c.chave) == 0, an.exp.mb(c.chave)))
        grupo = (correcao or parcimonia or crescimento or exploracao)
        if not grupo:
            acao, x = resolver()
            if acao == "mover":
                atual = x
                caminho.append(atual)
                rejeicoes = 0
                continue
            if acao == "medir":
                return fim(x, "aguardando medição para resolver incerteza", False)
            passos.append(Passo("parada", texto=(
                f"nenhuma candidata restante: o modelo não prevê ganho acima da "
                f"margem ({margem / 1000:,.1f} s, {fonte}) e a exploração "
                f"({gastas} de {par.exploracao} combinações medidas) se esgotou")))
            return fim(None, "nenhuma candidata promete ganho relevante", True)
        cand = grupo[0]

        if an.sessoes(cand.chave) == 0:
            passos.append(Passo("medicao", cand.movimento, atual, cand.chave,
                                previsto_ms=cand.previsto_ms,
                                texto=f"{cand.motivo}: sem medida"))
            return fim(Medicao(cand.chave, f"{cand.movimento}: {cand.motivo}",
                               ordem_da_validacao(an, cand.chave), cand.movimento),
                       "aguardando medição da candidata", False)

        teste, dec, cmp, margem, custo, notas = decidir_movimento(an, atual, cand.chave)
        dmb = an.exp.mb(cand.chave) - mb_a
        p = Passo("comparacao", cand.movimento, atual, cand.chave, teste, dec,
                  cmp.delta_ms, cmp.li_ms, cmp.ls_ms, margem, custo, dmb,
                  cand.previsto_ms, cand.motivo,
                  {"fonte_margem": fonte, "notas": notas,
                   "insuficiencias": cmp.insuficiencias,
                   "sessoes": [an.sessoes(atual), an.sessoes(cand.chave)],
                   "prob_positivo": cmp.prob_positivo})
        if dec == est.ACEITA and not sessoes_para_aceitar(an, cand.chave):
            # O intervalo diz "aceita", mas com uma sessão só: o que parece
            # efeito pode ser o plano que o planner escolheu só ali (a matriz
            # e as validações já discordaram assim na y15 do YCSB e na Q3
            # do TPC-H). Rejeitar com uma sessão é o lado conservador;
            # aceitar, não.
            if an.sessoes(cand.chave) < par.max_validacoes:
                p.texto += " — aceitação pede outra sessão independente"
                passos.append(p)
                passos.append(Passo("medicao", cand.movimento, atual, cand.chave,
                                    texto="segunda sessão antes de aceitar"))
                return fim(Medicao(cand.chave, f"confirmar a aceitação de "
                                   f"{_c(cand.chave)} numa sessão independente",
                                   ordem_da_validacao(an, cand.chave), cand.movimento),
                           "aguardando segunda sessão para aceitar", False)
            dec = p.decisao = est.INCONCLUSIVA
        if dec == est.INCONCLUSIVA:
            opcoes = [c for c in (cand.chave, atual)
                      if c != VAZIA and an.sessoes(c) < par.max_validacoes]
            if opcoes:
                alvo = min(opcoes, key=lambda c: an.sessoes(c))
                passos.append(p)
                passos.append(Passo("medicao", cand.movimento, atual, alvo,
                                    texto="intervalo cruza a margem: nova sessão"))
                return fim(Medicao(alvo, f"inconclusivo ({teste}): "
                                   f"{_c(cand.chave)} contra {_c(atual)}",
                                   ordem_da_validacao(an, alvo), cand.movimento),
                           "aguardando nova sessão para decidir", False)
            p.decisao = "nao_ordenavel"
            nao_ord.append((atual, cand.chave))
        passos.append(p)
        examinadas.add(cand.chave)
        ordem_exam.append(cand.chave)
        if p.decisao == est.ACEITA:
            atual = cand.chave
            caminho.append(atual)
            rejeicoes = 0
        elif p.decisao in (est.REJEITADA, "nao_ordenavel") and teste == "superioridade":
            # Não ordenável conta como crescimento fracassado: o conjunto maior
            # esgotou as sessões sem provar o ganho. Sem isso, a caminhada
            # passa de variante em variante quase idêntica (o mesmo núcleo,
            # um índice pequeno trocado), cada uma com sessões próprias.
            rejeicoes += 1
            if rejeicoes >= par.paciencia:
                acao, x = resolver()
                if acao == "mover":
                    atual = x
                    caminho.append(atual)
                    rejeicoes = 0
                    continue
                if acao == "medir":
                    return fim(x, "aguardando medição para resolver incerteza", False)
                passos.append(Passo("parada", texto=(
                    f"{rejeicoes} crescimentos seguidos sem ganho provado: "
                    f"rejeitados (nem o limite superior do intervalo passou da "
                    f"margem) ou não ordenáveis (sessões esgotadas sem decidir)")))
                return fim(None, "paciência esgotada: ganho adicional não provado",
                           True)
    return fim(None, "limite de passos", True)


def _c(c: FrozenSet[int]) -> str:
    return "∅" if not c else "{" + ",".join(f"i{i}" for i in sorted(c)) + "}"


# --------------------------------------------------------------------------
# O relatório para o DBA
# --------------------------------------------------------------------------

def _num(v: Optional[float]) -> Optional[float]:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return v


def _ic(li: float, ls: float) -> dict:
    return {"li_ms": _num(li), "ls_ms": _num(ls),
            "aberto_abaixo": math.isinf(li), "aberto_acima": math.isinf(ls)}


def descrever(an: Analise, c: FrozenSet[int], rotulo: Dict[int, str]) -> dict:
    """Uma configuração para o DBA: índices, DDL, espaço, benefício, quem
    melhora e quem piora, evidência e limitações."""
    inst, exp = an.exp.inst, an.exp
    nome = lambda q: rotulo.get(q, f"#{q}")                          # noqa: E731
    cf = exp.configuracoes.get(c)
    d = {"chave": sorted(c), "rotulo": _c(c),
         "otimizacoes": cf.otimizacoes if cf else [],
         "origem": cf.rotulo if cf else "adaptar",
         "mb": round(exp.mb(c), 1),
         "indices": [{"id": i, "ddl": inst.indices[i].ddl, "curto": inst.indices[i].curto,
                      "mb": round(inst.indices[i].mb, 1)} for i in sorted(c)],
         "sessoes": an.sessoes(c),
         "validacoes": [v.sessao for v in exp.validacoes_de(c)],
         "limitacoes": []}
    if c == VAZIA:
        return d
    if an.sessoes(c) == 0:
        d["limitacoes"].append("sem medida")
        return d
    if not an.ruido.suficiente:
        d["limitacoes"].append("variação entre sessões desconhecida: sem bloco "
                               "repetido, nenhuma comparação tem intervalo")
        return d
    cmp = an.comparar(VAZIA, c)
    carga = an.carga(c)
    d.update({
        "beneficio_ms": cmp.delta_ms, **{f"beneficio_{k}": v for k, v in
                                         _ic(cmp.li_ms, cmp.ls_ms).items()},
        "carga_ms": carga[0], "carga_e_piso": carga[3],
        "melhoram": [{"query": nome(e.query), "ganho_pct": _num(e.ganho_pct),
                      "ganho_ponderado_ms": e.ganho_ponderado_ms,
                      "piso": e.piso_antes} for e in cmp.melhoram()],
        "pioram": [{"query": nome(e.query), "ganho_pct": _num(e.ganho_pct),
                    "ganho_ponderado_ms": e.ganho_ponderado_ms,
                    **_ic(e.li_ms, e.ls_ms)} for e in cmp.pioram()],
        "indefinidas": [nome(e.query) for e in cmp.por_query
                        if e.mudanca == "indefinida"],
        "plano_instavel": [nome(e.query) for e in cmp.por_query if e.plano_instavel],
        "sessoes_discordam": [nome(e.query) for e in cmp.por_query if e.dispersa],
    })
    if d["sessoes_discordam"]:
        d["limitacoes"].append(
            "as sessões discordam mais do que o ruído da query explica em "
            + ", ".join(d["sessoes_discordam"])
            + " (plano instável?): o intervalo usa a variância delas")
    usados = an.indices_usados(c)
    d["sem_uso"] = [i for i in sorted(c) if not usados.get(i)]
    if d["sem_uso"]:
        d["limitacoes"].append(
            f"índice(s) {', '.join(f'i{i}' for i in d['sem_uso'])} sem uso nas "
            f"validações: candidatos a remoção "
            f"({sum(inst.indices[i].mb for i in d['sem_uso']):,.1f} MB)")
    if cmp.aberto_acima:
        d["limitacoes"].append("benefício é piso: a linha de base estourou o "
                               "timeout em alguma query")
    if d["plano_instavel"]:
        d["limitacoes"].append("plano mudou entre sessões em "
                               + ", ".join(d["plano_instavel"]))
    if an.sessoes(c) < 2:
        d["limitacoes"].append("uma sessão só de medida")
    exp_c = exposicao_de(an, c)
    if exp_c.tem_escrita:
        d["exposicao"] = {"entradas_min": exp_c.minimo, "entradas_max": exp_c.maximo,
                          "entradas_hot_observado": exp_c.observada,
                          "certas": exp_c.certas, "provaveis": exp_c.provaveis,
                          "possiveis_hot": exp_c.possiveis, "deletes": exp_c.deletes,
                          "hot_observado": exp_c.hot_observado}
        if an._custo_medido(c) is None:
            d["limitacoes"].append("custo de manutenção não medido: só a leitura "
                                   "foi avaliada")
            if exp_c.maximo > 0:
                d["equilibrio_ms_por_entrada"] = {
                    "com_exposicao_max": mnt.equilibrio(cmp.delta_ms, exp_c.maximo),
                    "com_exposicao_min": mnt.equilibrio(cmp.delta_ms, exp_c.minimo),
                    "com_li_e_exposicao_max": (None if cmp.aberto_abaixo else
                                               mnt.equilibrio(cmp.li_ms, exp_c.maximo))}
    return d


def relatorio(an: Analise, traj: Trajetoria, rotulo: Dict[int, str]) -> dict:
    exp = an.exp
    rec = traj.atual
    mostradas: List[FrozenSet[int]] = []
    for c in ([VAZIA] + list(traj.caminho) + list(traj.examinadas)
              + [c for c, cf in exp.configuracoes.items()
                 if cf.otimizacoes and exp.validacoes_de(c)]):
        if c not in mostradas and (c == VAZIA or an.sessoes(c) > 0):
            mostradas.append(c)
    mostradas.sort(key=lambda c: (exp.mb(c), sorted(c)))
    margem_rec, fonte_rec = an.margem(rec)

    configs = []
    pontos = []
    for c in mostradas:
        d = descrever(an, c, rotulo)
        exp_c = exposicao_de(an, c) if c else None
        if c and "beneficio_ms" in d:
            pontos.append(est.Ponto(c, d["beneficio_ms"], exp.mb(c),
                                    exp_c.maximo if exp_c and exp_c.tem_escrita else None))
        configs.append(d)
    pontos.append(est.Ponto(VAZIA, 0.0, 0.0, 0.0 if any(p.exposicao is not None
                                                        for p in pontos) else None))
    fronteira = {p.chave for p in est.fronteira_pareto(pontos)}
    por_chave = {p.chave: p for p in pontos}

    suficiente = an.ruido.suficiente
    for d in configs:
        c = frozenset(d["chave"])
        d["fronteira_pareto"] = c in fronteira
        if not suficiente:
            d["dominada_por"] = None
            d["situacao"] = "sem intervalo (variação entre sessões desconhecida)"
            continue
        d["dominada_por"] = None
        for o in pontos:
            if o.chave != c and c in por_chave and est.domina(o, por_chave[c]):
                cmp = an.comparar(c, o.chave)
                if est.dominada_com_evidencia(o, por_chave[c], cmp, margem_rec):
                    d["dominada_por"] = _c(o.chave)
                    break
        d["situacao"] = _situacao(an, c, rec, d, margem_rec, traj.concluida)
        if c != rec and c != VAZIA and an.sessoes(c) and an.sessoes(rec) is not None:
            cmp = an.comparar(rec, c)
            d["contra_recomendada"] = {"delta_ms": cmp.delta_ms,
                                       **_ic(cmp.li_ms, cmp.ls_ms),
                                       "ordem": est.ordenar(cmp.li_ms, cmp.ls_ms,
                                                            margem_rec)}

    caminho = []
    for k in range(1, len(traj.caminho)):
        a, b = traj.caminho[k - 1], traj.caminho[k]
        cmp = an.comparar(a, b)
        dmb = exp.mb(b) - exp.mb(a)
        caminho.append({"de": _c(a), "para": _c(b), "delta_ms": cmp.delta_ms,
                        **_ic(cmp.li_ms, cmp.ls_ms), "delta_mb": round(dmb, 1),
                        "eficiencia_ms_por_mb": (cmp.delta_ms / dmb if dmb > 0 else None)})

    # Entre as configurações da fronteira: o que se pode afirmar de cada par,
    # sem forçar ordem onde a evidência não dá.
    fr = [c for c in mostradas if c in fronteira and c != VAZIA] if suficiente else []
    ordens = []
    for x in range(len(fr)):
        for y in range(x + 1, len(fr)):
            cmp = an.comparar(fr[x], fr[y])
            # A margem é a da configuração de referência do par (a menor):
            # "relevante" é relativo ao que a carga gasta nela.
            m, _ = an.margem(fr[x])
            ordens.append({"a": _c(fr[x]), "b": _c(fr[y]), "delta_ms": cmp.delta_ms,
                           **_ic(cmp.li_ms, cmp.ls_ms), "margem_ms": m,
                           "afirmacao": est.ordenar(cmp.li_ms, cmp.ls_ms, m)})

    sessoes = sorted({b.sessao for b in an.ev.blocos})
    # Testes por query que o relatório mostra (as marcas + e −): cada
    # configuração descrita contra a linha de base, query a query.
    testes_q = sum(len(d.get("melhoram", [])) + len(d.get("pioram", []))
                   + len(d.get("indefinidas", [])) for d in configs if d["chave"])
    return {
        "sessao": exp.sessao,
        "parametros": an.par.como_dict(),
        "recomendada": _c(rec),
        "recomendada_otimizacao": (exp.configuracoes[rec].otimizacoes[0]
                                   if rec in exp.configuracoes
                                   and exp.configuracoes[rec].otimizacoes else None),
        "caminho": caminho,
        "caminho_chaves": [sorted(c) for c in traj.caminho],
        "margem_final_ms": margem_rec, "fonte_margem": fonte_rec,
        "motivo": traj.motivo, "concluida": traj.concluida,
        "proxima": (None if traj.proxima is None else
                    {"configuracao": _c(traj.proxima.configuracao),
                     "chave": sorted(traj.proxima.configuracao),
                     "motivo": traj.proxima.motivo, "ordem": traj.proxima.ordem}),
        "passos": [_passo_dict(p) for p in traj.passos],
        "configuracoes": configs,
        "ordem_na_fronteira": ordens,
        "nao_ordenaveis": [[_c(a), _c(b)] for a, b in traj.nao_ordenaveis],
        "resolucao": resumo_resolucao(an, traj),
        "multiplicidade": multiplicidade(an, traj, testes_q) if suficiente else None,
        "evidencia": {
            "sessoes": sessoes,
            "validacoes": {f: sum(1 for v in exp.validacoes if v.finalidade == f)
                           for f in ("selecao", "legado", "confirmacao")},
            "excluidas_por_ambiente": exp.excluidas,
            "sem_ambiente_registrado": exp.sem_ambiente,
            "ambiente_referencia": exp.referencia_hash,
        },
        "ruido": resumo_ruido(an, rotulo),
    }


def resumo_resolucao(an: Analise, traj: Trajetoria) -> dict:
    """A última resolução de incerteza, para o DBA: cada alternativa medida
    contra a atual, o que a evidência diz e o que foi feito."""
    exp = an.exp
    return {
        "orcamento": {"usadas": resolucoes_medidas(an), "limite": an.par.resolucao},
        "referencia": _c(traj.atual),
        "alternativas": [
            {"rotulo": _c(p.chave), "chave": sorted(p.chave),
             "origem": exp.configuracoes[p.chave].rotulo
                       if p.chave in exp.configuracoes else "-",
             "mb": round(exp.mb(p.chave), 1), "delta_mb": round(p.delta_mb, 1),
             "teste": p.teste, "decisao": p.decisao, "delta_ms": _num(p.delta_ms),
             **_ic(p.li_ms, p.ls_ms), "margem_ms": p.margem_ms, "custo_ms": p.custo_ms,
             "prob_vencer": p.prob_vencer, "sessoes": list(p.sessoes),
             "acao": p.acao, "motivo": p.motivo,
             "alvo": None if p.alvo is None else _c(p.alvo)}
            for p in traj.pendencias],
    }


def multiplicidade(an: Analise, traj: Trajetoria, testes_por_query: int = 0) -> dict:
    """O que o leitor precisa para interpretar os intervalos de um
    procedimento adaptativo: quanto se explorou, quantos testes, qual a
    unidade independente, o que a seleção faz com os intervalos e se as
    decisões do caminho sobrevivem a uma correção de Bonferroni.

    A correção é SENSIBILIDADE, não regra: as decisões continuam sendo as do
    nível declarado. Bonferroni sobre as comparações da caminhada final é
    conservador (as comparações são correlacionadas, a mesma configuração
    aparece em várias) e ainda assim não cobre as análises intermediárias
    de um procedimento sequencial. A família é a caminhada final mais uma
    comparação por configuração medida fora do caminho, contra a atual,
    tenha a caminhada chegado à resolução ou não."""
    exp, par = an.exp, an.par
    medidas = [c for c in exp.configuracoes if c and an.sessoes(c) > 0]
    conjuntos = [c for c in medidas if exp.validacoes_de(c)]
    sess = sorted({b.sessao for b in an.ev.blocos})
    val = [s for s in sess if s.startswith("v")]
    por_conjunto = sorted(an.sessoes(c) for c in conjuntos)
    comp = [p for p in traj.passos if p.tipo == "comparacao"]
    na_caminhada = [p for p in comp if p.movimento != "resolucao"]
    # A família não pode depender de onde a última análise parou: se a
    # caminhada ainda não chegou à resolução (parou pedindo medida), conta
    # assim mesmo uma comparação por configuração medida fora do caminho,
    # contra a atual — toda configuração medida entrou em alguma comparação.
    fora = set(traj.caminho)
    contra_atual = sum(1 for c in medidas if c != traj.atual and c not in fora)
    resolucao = max(traj.comparacoes_resolucao, contra_atual)
    familia = max(len(na_caminhada) + resolucao, 1)
    nivel = (1 - par.confianca) / 2
    conf_b = 1 - (1 - par.confianca) / familia
    # O limite de Bonferroni é um quantil extremo: amostras suficientes para
    # 50 na cauda, num amostrador à parte (as decisões não mudam).
    n_b = int(min(max(par.amostras, 50 / ((1 - conf_b) / 2)), 200_000))
    am_b = (an.am if n_b == par.amostras
            else est.Amostrador(an.ev, an.ruido, n_b, par.semente))
    cauda = n_b * (1 - conf_b) / 2

    sensib = []
    for k in range(1, len(traj.caminho)):
        a, b = traj.caminho[k - 1], traj.caminho[k]
        teste, dec, _cmp, margem, custo, _n = decidir_movimento(an, a, b)
        x = est.comparar(am_b, a, b, conf_b)
        f = est.superioridade if teste == "superioridade" else est.nao_inferioridade
        dec_b = f(x.li_ms - custo, x.ls_ms - custo, margem)
        sensib.append({"de": _c(a), "para": _c(b), "teste": teste,
                       "decisao": dec, "li_ms": _num(_cmp.li_ms),
                       "li_bonferroni_ms": _num(x.li_ms), "margem_ms": margem,
                       "decisao_bonferroni": dec_b,
                       "sobrevive": dec_b == est.ACEITA})
    origens: Dict[str, int] = {}
    for v in exp.validacoes:
        if v.configuracao and exp._entra(v, evd.SELECAO):
            chave = v.movimento or (v.origem if v.finalidade != "legado" else "legado")
            origens[chave] = origens.get(chave, 0) + 1
    avisos = []
    if cauda < 10:
        avisos.append(f"com {n_b} amostras, o limite de Bonferroni sai de "
                      f"{cauda:.0f} amostras na cauda: instável")
    if por_conjunto and por_conjunto[len(por_conjunto) // 2] < 3:
        avisos.append("a maioria dos conjuntos tem menos de 3 sessões: a média de "
                      "cada um é imprecisa (a variância entre sessões, não, porque é "
                      "estimada em conjunto)")
    return {
        "configuracoes_medidas": len(medidas),
        "conjuntos_validados": len(conjuntos),
        "indices_sozinhos_so_na_matriz": len(medidas) - len(conjuntos),
        "configuracoes_comparadas_na_caminhada": len({p.para for p in na_caminhada}),
        "comparacoes": {"caminhada": len(na_caminhada),
                        "resolucao": resolucao,
                        "resolucao_feita": traj.comparacoes_resolucao > 0,
                        "familia_bonferroni": familia},
        "testes_por_query": testes_por_query,
        "marcas_por_acaso_esperadas": round(testes_por_query * (1 - par.confianca), 1),
        "sessoes_independentes": len(sess),
        "validacoes_de_selecao": len(val),
        "validacoes_por_origem": origens,
        "sessoes_por_conjunto": ({"min": por_conjunto[0],
                                  "mediana": por_conjunto[len(por_conjunto) // 2],
                                  "max": por_conjunto[-1]} if por_conjunto else None),
        "unidade": ("a sessão de medida: numa validação, a linha de base e a "
                    "configuração medidas na mesma sessão; na matriz, a linha de base "
                    "da matriz e cada índice sozinho. As repetições de um bloco não são "
                    "independentes (só dão a variância dentro da sessão), e as queries "
                    "de uma sessão compartilham o deslocamento γ dela. Duas "
                    "configurações nunca estão na mesma sessão: a diferença entre elas "
                    "passa pela linha de base de cada sessão, com o pareamento β"),
        "nivel": (f"cada decisão é um teste unilateral de nível {nivel:.0%} "
                  f"(intervalo bilateral de {par.confianca:.0%}), sem correção para o "
                  f"número de comparações; cada marca +/− por query é um teste "
                  f"bilateral de {1 - par.confianca:.0%}"),
        "efeito_da_selecao": (
            "os intervalos são condicionais a cada comparação. O procedimento é "
            f"sequencial ({len(val)} validações de seleção, cada uma pedida por uma "
            "análise intermediária sobre a evidência de então) e a recomendada é a "
            "que se saiu melhor entre as comparadas: o ganho dela estimado na "
            "seleção tende a ser otimista. Só a confirmação, em sessões que a "
            "seleção não leu, estima o ganho sem esse viés"),
        "bonferroni": {"confianca": conf_b, "amostras": n_b,
                       "amostras_na_cauda": cauda, "caminho": sensib},
        "confirmacao": "pendente: adaptar --confirmar <adaptação> --executar",
        "avisos": avisos,
    }


def _situacao(an: Analise, c: FrozenSet[int], rec: FrozenSet[int], d: dict,
              margem: float, concluida: bool = True) -> str:
    # Com a exploração em aberto não há recomendação: há a configuração
    # atual, e a comparação é com ela.
    r = "recomendada" if concluida else "atual"
    if c == VAZIA:
        if rec != VAZIA:
            return "referência (linha de base)"
        return ("recomendada: nenhuma configuração provou ganho relevante"
                if concluida else "atual (exploração em aberto)")
    sufixo = ""
    if d.get("pioram"):
        sufixo += "; avaliar regressões"
    if any("só a leitura" in l for l in d.get("limitacoes", [])):
        sufixo += "; só a leitura avaliada"
    if c == rec:
        return (r if concluida else "atual (exploração em aberto)") + sufixo
    if d.get("dominada_por"):
        return f"dominada por {d['dominada_por']}"
    if not an.sessoes(c):
        return "sem medida"
    cmp = an.comparar(rec, c)
    mb_c, mb_r = an.exp.mb(c), an.exp.mb(rec)
    if mb_c > mb_r:
        if cmp.li_ms > margem:
            return f"ganho relevante sobre a {r}" + (
                ", fora do caminho" if concluida else "") + sufixo
        if cmp.li_ms > 0:
            return f"mais espaço, ganho sobre a {r} abaixo da margem; avaliar custo" + sufixo
        if cmp.ls_ms < margem:
            return "mais espaço sem ganho relevante" + sufixo
        return f"não ordenável contra a {r} (evidência insuficiente)" + sufixo
    if est.nao_inferioridade(cmp.li_ms, cmp.ls_ms, margem) == est.ACEITA:
        return f"alternativa compacta: não inferior à {r}" + sufixo
    if cmp.ls_ms < -margem:
        return "alternativa compacta, com perda relevante" + sufixo
    return f"não ordenável contra a {r} (evidência insuficiente)" + sufixo


def _passo_dict(p: Passo) -> dict:
    return {"tipo": p.tipo, "movimento": p.movimento,
            "de": None if p.de is None else _c(p.de),
            "para": None if p.para is None else _c(p.para),
            "teste": p.teste, "decisao": p.decisao, "delta_ms": _num(p.delta_ms),
            **({} if p.li_ms is None else _ic(p.li_ms, p.ls_ms)),
            "margem_ms": p.margem_ms, "custo_ms": p.custo_ms, "delta_mb": p.delta_mb,
            "previsto_ms": p.previsto_ms, "texto": p.texto, "detalhe": p.detalhe}


def resumo_ruido(an: Analise, rotulo: Dict[int, str]) -> dict:
    r = an.ruido
    linhas = []
    for q, rq in sorted(r.por_query.items()):
        if not r.suficiente:
            break
        linhas.append({"query": rotulo.get(q, f"#{q}"), "blocos": rq.blocos,
                       "gl_entre": rq.gl_entre,
                       "sigma_entre_bruto": None if rq.s2_entre is None else math.sqrt(rq.s2_entre),
                       "sigma_eps": rq.sigma_eps, "sigma_alfa": rq.sigma_alfa,
                       "tau_moderado": math.sqrt(rq.tau2),
                       "limiar_matriz_pct": r.limiar_pct(q, an.par.confianca)})
    razoes = [l["sigma_alfa"] / l["sigma_eps"] for l in linhas
              if l["sigma_eps"] and l["sigma_eps"] > 0]
    return {"suficiente": r.suficiente, "d0": r.d0,
            "s0": None if r.s0_2 is None else math.sqrt(r.s0_2),
            "beta": r.beta, "beta_ep": r.beta_ep, "beta_pontos": r.beta_pontos,
            "sigma_gama": r.sigma_gama,
            "deslocamento_por_sessao": r.deslocamento,
            "mediana_sigma_alfa_sobre_sigma_eps": (sorted(razoes)[len(razoes) // 2]
                                                    if razoes else None),
            "por_query": linhas, "avisos": r.avisos}


# --------------------------------------------------------------------------
# Mostrar
# --------------------------------------------------------------------------

def _s(ms: Optional[float]) -> str:
    if ms is None:
        return "?"
    if math.isinf(ms):
        return "+∞" if ms > 0 else "−∞"
    return f"{ms / 1000:+,.1f}"


def _ic_txt(d: dict, pre: str = "") -> str:
    li = -math.inf if d.get(f"{pre}aberto_abaixo") else d.get(f"{pre}li_ms")
    ls = math.inf if d.get(f"{pre}aberto_acima") else d.get(f"{pre}ls_ms")
    return f"[{_s(li)}; {_s(ls)}]"


def mostrar(rel: dict) -> None:
    par = rel["parametros"]
    print(f"\n=== seleção adaptativa, sessão {rel['sessao']} "
          f"(confiança {par['confianca']:.0%}, relevância "
          + (f"{par['margem_ms'] / 1000:g} s fixa" if par.get("margem_ms") is not None
             else f"{par['relevancia']:.0%}")
          + f", λ {par['lambda_mb']:g} ms/MB)")
    ev = rel["evidencia"]
    print(f"  evidência: {len(ev['sessoes'])} sessões de medida "
          f"({', '.join(ev['sessoes'][:12])}{'…' if len(ev['sessoes']) > 12 else ''})")
    print(f"  validações: {ev['validacoes']}")
    if ev["excluidas_por_ambiente"]:
        print("  EXCLUÍDAS por ambiente diferente:")
        for s, difs in ev["excluidas_por_ambiente"].items():
            print(f"    {s}: {'; '.join(difs[:3])}")
    if ev["sem_ambiente_registrado"]:
        print(f"  sem ambiente registrado (anteriores ao registro): "
              f"{', '.join(ev['sem_ambiente_registrado'])}")
    r = rel["ruido"]
    if r["suficiente"]:
        print(f"  ruído: σ_α/σ_ε mediano {r['mediana_sigma_alfa_sobre_sigma_eps'] or 0:.1f} "
              f"(a sessão pesa mais que a repetição); d0 = {r['d0']:.1f}, "
              f"τ típico {r['s0']:.3f} em log")
        print(f"  pareamento: β = {r['beta']:.2f} ± {r['beta_ep']:.2f} "
              f"({r['beta_pontos']} pontos) do γ da sessão chega ao bloco de "
              f"configuração; σ_γ = {r['sigma_gama']:.3f}")
        desloc = ", ".join(f"{s} {g:+.1%}" for s, g in
                           sorted(r["deslocamento_por_sessao"].items())
                           if abs(g) >= 0.02)
        if desloc:
            print(f"  sessões deslocadas (γ ≥ 2%): {desloc}")
    for a in r["avisos"]:
        print(f"  AVISO: {a}")

    print("\n  passos:")
    for k, p in enumerate(rel["passos"], 1):
        if p["tipo"] == "comparacao":
            print(f"   {k:>2}. {p['movimento']:<12} {p['de']} → {p['para']}  "
                  f"ΔB {_s(p['delta_ms'])} s {_ic_txt(p)}  margem "
                  f"{p['margem_ms'] / 1000:,.1f} s"
                  + (f" + custo {p['custo_ms'] / 1000:,.1f}" if p["custo_ms"] else "")
                  + f"  ΔMB {p['delta_mb']:+,.1f}  {p['teste']}: {p['decisao'].upper()}")
            print(f"       {p['texto']}")
            for n in p["detalhe"].get("notas", []) + p["detalhe"].get("insuficiencias", []):
                print(f"       · {n}")
        else:
            print(f"   {k:>2}. {p['tipo']}: {p['texto']}")
    print(f"\n  {('PAROU' if rel['concluida'] else 'EM ABERTO')}: {rel['motivo']}")
    if rel["proxima"]:
        print(f"  próxima medição: {rel['proxima']['configuracao']} "
              f"({rel['proxima']['motivo']}; ordem {rel['proxima']['ordem']})")

    print(f"\n  {'configuração':<26} {'MB':>7} {'benefício s':>12} {'IC 90%':>22} "
          f"{'+':>3} {'−':>3} {'ses':>3}  situação")
    for d in rel["configuracoes"]:
        if not d["chave"]:
            continue
        b = d.get("beneficio_ms")
        print(f"  {d['rotulo'][:26]:<26} {d['mb']:>7,.1f} "
              f"{('≥' if d.get('beneficio_aberto_acima') else '') + _s(b) if b is not None else '-':>12} "
              f"{_ic_txt(d, 'beneficio_') if b is not None else '':>22} "
              f"{len(d.get('melhoram', [])):>3} {len(d.get('pioram', [])):>3} "
              f"{d['sessoes']:>3}  {d['situacao']}")
    print("  (+/− = queries com melhora/piora em que o intervalo exclui zero, "
          "contra a linha de base)")

    rec = next((d for d in rel["configuracoes"] if d["rotulo"] == rel["recomendada"]), None)
    if rec and rec["chave"]:
        titulo = ("RECOMENDADA" if rel["concluida"]
                  else "ATUAL (exploração em aberto, ainda não é recomendação)")
        print(f"\n  {titulo} {rec['rotulo']}, {rec['mb']:,.1f} MB "
              f"(origem: {rec['origem']}):")
        for i in rec["indices"]:
            print(f"    {i['ddl']};   -- {i['mb']:,.1f} MB")
        for d in rel["caminho"]:
            ef = d["eficiencia_ms_por_mb"]
            print(f"    {d['de']} → {d['para']}: ΔB {_s(d['delta_ms'])} s "
                  f"{_ic_txt(d)}, ΔMB {d['delta_mb']:+,.1f}"
                  + (f", {ef / 1000:,.2f} s/MB" if ef else ""))
        if rec.get("melhoram"):
            print("    melhoram: " + ", ".join(
                f"{m['query']} {m['ganho_pct'] or 0:+.0f}%" + ("(piso)" if m["piso"] else "")
                for m in rec["melhoram"]))
        if rec.get("pioram"):
            print("    pioram:   " + ", ".join(
                f"{m['query']} {m['ganho_pct'] or 0:+.0f}%" for m in rec["pioram"]))
        for l in rec["limitacoes"]:
            print(f"    limitação: {l}")
        eq = rec.get("equilibrio_ms_por_entrada")
        if eq and eq.get("com_exposicao_max"):
            print(f"    manutenção: a configuração se paga se cada entrada de índice "
                  f"custar menos que {eq['com_exposicao_max']:,.3f} ms (exposição "
                  f"máxima) a {eq['com_exposicao_min'] or 0:,.3f} ms (mínima)")
    if rel["ordem_na_fronteira"]:
        print("\n  na fronteira de Pareto, o que a evidência permite afirmar:")
        for o in rel["ordem_na_fronteira"]:
            print(f"    {o['a']} × {o['b']}: {o['afirmacao']} "
                  f"(ΔB {_s(o['delta_ms'])} s {_ic_txt(o)})")
    _mostrar_resolucao(rel.get("resolucao"))
    _mostrar_multiplicidade(rel.get("multiplicidade"))


def _mostrar_resolucao(r: Optional[dict]) -> None:
    if not r or not r["alternativas"]:
        return
    o = r["orcamento"]
    print(f"\n  resolução de incerteza contra {r['referencia']} "
          f"(orçamento: {o['usadas']} de {o['limite']} validações usadas):")
    # As descartadas que são só um índice da matriz são muitas e não dizem
    # nada ao DBA; vão no JSON.
    mostrar_ = [a for a in r["alternativas"]
                if a["acao"] != "descartada" or a["origem"] != "matriz (índice sozinho)"]
    escondidas = len(r["alternativas"]) - len(mostrar_)
    for a in mostrar_:
        prob = "" if a["prob_vencer"] is None else f", P(vencer) {a['prob_vencer']:.0%}"
        print(f"    {a['rotulo'][:30]:<30} {a['mb']:>7,.1f} MB ({a['delta_mb']:+,.1f})  "
              f"ΔB {_s(a['delta_ms'])} s {_ic_txt(a)}  sessões {a['sessoes'][1]}"
              f"{prob}  → {a['acao'].upper()}")
        print(f"       {a['motivo']}")
    if escondidas:
        print(f"    (+ {escondidas} índice(s) sozinho(s) da matriz, descartados; no JSON)")


def _mostrar_multiplicidade(m: Optional[dict]) -> None:
    if not m:
        return
    c = m["comparacoes"]
    print(f"\n  multiplicidade: {m['configuracoes_medidas']} configurações medidas "
          f"({m['conjuntos_validados']} conjuntos validados, "
          f"{m['indices_sozinhos_so_na_matriz']} índices só na matriz); "
          f"{c['caminhada']} comparações na caminhada + {c['resolucao']} na "
          f"resolução de incerteza"
          + ("" if c.get("resolucao_feita", True) else
             " (ainda não feita: contadas as que ela faria contra a atual)"))
    s = m["sessoes_por_conjunto"]
    print(f"    {m['sessoes_independentes']} sessões independentes "
          f"({m['validacoes_de_selecao']} validações de seleção"
          + (f"; por conjunto: mín {s['min']}, mediana {s['mediana']}, máx {s['max']}"
             if s else "") + f"); origem das validações: {m['validacoes_por_origem']}")
    print(f"    {m['testes_por_query']} testes por query no relatório: "
          f"~{m['marcas_por_acaso_esperadas']:g} marcas +/− esperadas por acaso se nada "
          f"tivesse mudado")
    b = m["bonferroni"]
    for x in b["caminho"]:
        print(f"    Bonferroni ({c['familia_bonferroni']} comparações, "
              f"{b['confianca']:.2%}): {x['de']} → {x['para']} li "
              f"{_s(x['li_ms'])} → {_s(x['li_bonferroni_ms'])} s, margem "
              f"{x['margem_ms'] / 1000:,.1f} s: "
              + ("sobrevive" if x["sobrevive"] else f"NÃO sobrevive ({x['decisao_bonferroni']})"))
    print(f"    unidade: {m['unidade']}")
    print(f"    {m['nivel']}")
    print(f"    {m['efeito_da_selecao']}")
    print(f"    confirmação: {m['confirmacao']}")
    for a in m["avisos"]:
        print(f"    AVISO: {a}")


# --------------------------------------------------------------------------
# Persistência
# --------------------------------------------------------------------------

SQL_ABRIR = """
INSERT INTO monitoring.adaptacao (sessao_id, parametros, modo)
VALUES (%s, %s, %s) RETURNING id
"""

SQL_FECHAR = """
UPDATE monitoring.adaptacao
   SET estado = %s, medicoes = %s, validacoes_lidas = %s, motivo = %s,
       recomendada = %s, resultado = %s, concluida_em = now()
 WHERE id = %s
"""

SQL_PASSO = """
INSERT INTO monitoring.adaptacao_passo
       (adaptacao_id, passo, tipo, movimento, de_otimizacao, para_otimizacao,
        teste, decisao, delta_b_ms, li_ms, ls_ms, margem_ms, delta_mb, detalhe)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def abrir(conn, sessao: int, par: Parametros, modo: str) -> int:
    with conn.cursor() as cur:
        cur.execute(SQL_ABRIR, (sessao, Jsonb(par.como_dict()), modo))
        aid = cur.fetchone()[0]
    conn.commit()
    return aid


def _otim_de(exp: evd.Experimento, c: Optional[FrozenSet[int]]) -> Optional[int]:
    if c is None:
        return None
    cf = exp.configuracoes.get(c)
    return cf.otimizacoes[0] if cf and cf.otimizacoes else None


def fechar(conn, aid: int, an: Analise, traj: Trajetoria, rel: dict,
           medicoes: int, estado: Optional[str] = None) -> None:
    exp = an.exp
    estado = estado or ("concluida" if traj.concluida else "aguardando_medicao")
    lidas = len({b.sessao for b in an.ev.blocos if b.sessao.startswith("v")})
    with conn.cursor() as cur:
        cur.execute("DELETE FROM monitoring.adaptacao_passo WHERE adaptacao_id = %s",
                    (aid,))
        for k, p in enumerate(traj.passos, 1):
            cur.execute(SQL_PASSO, (
                aid, k, p.tipo, p.movimento, _otim_de(exp, p.de), _otim_de(exp, p.para),
                p.teste, p.decisao, _num(p.delta_ms), _num(p.li_ms), _num(p.ls_ms),
                p.margem_ms, p.delta_mb,
                Jsonb({"texto": p.texto, "de": None if p.de is None else sorted(p.de),
                       "para": None if p.para is None else sorted(p.para),
                       "previsto_ms": p.previsto_ms, "custo_ms": p.custo_ms,
                       **p.detalhe})))
        cur.execute(SQL_FECHAR, (estado, medicoes, lidas, traj.motivo,
                                 _otim_de(exp, traj.atual), Jsonb(_jsonavel(rel)), aid))
    conn.commit()


def _jsonavel(obj):
    import json
    from .banco import como_json
    return json.loads(como_json(obj))


def otimizacao_para(conn, an: Analise, c: FrozenSet[int], aid: int,
                    movimento: str) -> int:
    """A otimização que representa a configuração, criada se preciso (com a
    previsão do modelo, como `otimizar` faz), para `validar` poder medi-la."""
    oid = _otim_de(an.exp, c)
    if oid is not None:
        return oid
    par = an.par.modelo_otim()
    par_g = replace(par)
    av = otimizacao.avaliar(an.exp.inst, par_g, sorted(c))
    d = par_g.como_dict()
    d.update({"adaptacao": aid, "movimento": movimento})
    with conn.cursor() as cur:
        cur.execute(otimizacao.SQL_MESMO_CONJUNTO, (an.exp.sessao, sorted(c)))
        linha = cur.fetchone()
        if linha:
            return linha[0]
        cur.execute(otimizacao.SQL_GRAVAR, (
            an.exp.sessao, Jsonb(d), f"adaptar {aid}: {movimento}", sorted(c),
            sum(an.exp.inst.indices[i].tamanho_bytes for i in c),
            av.beneficio_ms, av.objetivo,
            Jsonb({str(q): v for q, v in av.por_query.items()})))
        oid = cur.fetchone()[0]
    conn.commit()
    return oid


# --------------------------------------------------------------------------
# A confirmação independente
# --------------------------------------------------------------------------

def confirmar(an_sel: Analise, aid: int, rel_sel: dict) -> dict:
    """O veredito da confirmação: o último passo aceito do caminho, medido
    de novo em sessões que a seleção não leu.

    As médias vêm só da confirmação. As variâncias e os deslocamentos de
    sessão vêm de toda a evidência (seleção + confirmação): estimá-los com a
    seleção não enviesa as médias, e com uma ou duas sessões de confirmação
    eles não seriam estimáveis sozinhos.
    """
    exp = an_sel.exp
    chaves = rel_sel.get("caminho_chaves") or []
    if len(chaves) < 2:
        return {"confirmada": None, "motivo": "a seleção não aceitou movimento "
                                              "nenhum: nada a confirmar"}
    a, b = frozenset(chaves[-2]), frozenset(chaves[-1])
    passo = next(p for p in reversed(rel_sel["passos"])
                 if p["tipo"] == "comparacao" and p["decisao"] == est.ACEITA
                 and p["para"] == _c(b))
    ev_conf = exp.evidencia(so_confirmacao_de=aid)
    sess_b = {s for (s, c, _q) in ev_conf.uso if c == b}
    sess_a = {s for (s, c, _q) in ev_conf.uso if c == a} if a else {"base"}
    if not sess_b or not sess_a:
        return {"confirmada": None, "motivo": "faltam sessões de confirmação",
                "faltam": [_c(x) for x, s in ((b, sess_b), (a, sess_a)) if not s]}
    ev_tudo = est.Evidencia(an_sel.ev.blocos + ev_conf.blocos,
                            {**an_sel.ev.uso, **ev_conf.uso}, ev_conf.peso)
    ruido = est.estimar_ruido(ev_tudo)
    am = est.Amostrador(ev_conf, ruido, an_sel.par.amostras, an_sel.par.semente)
    cmp = est.comparar(am, a, b, an_sel.par.confianca)
    margem, custo = passo["margem_ms"], passo["custo_ms"] or 0.0
    teste = passo["teste"]
    f = est.superioridade if teste == "superioridade" else est.nao_inferioridade
    dec = f(cmp.li_ms - custo, cmp.ls_ms - custo, margem)
    sel = an_sel.comparar(a, b)
    # A reprodução, em números: quanto do ganho estimado na seleção a medida
    # independente encontrou (a razão < 1 é o tamanho da maldição do
    # vencedor), e se a estimativa da seleção cabe no intervalo da
    # confirmação.
    reproducao = None
    if sel.delta_ms and math.isfinite(sel.delta_ms) and math.isfinite(cmp.delta_ms):
        reproducao = {
            "razao_confirmacao_sobre_selecao": cmp.delta_ms / sel.delta_ms,
            "mesmo_sinal": (cmp.delta_ms > 0) == (sel.delta_ms > 0),
            "selecao_no_intervalo_da_confirmacao": cmp.li_ms <= sel.delta_ms <= cmp.ls_ms}
    return {"confirmada": {est.ACEITA: True, est.REJEITADA: False}.get(dec),
            "decisao": dec, "teste": teste, "de": _c(a), "para": _c(b),
            "margem_ms": margem, "custo_ms": custo,
            "confirmacao": {"delta_ms": cmp.delta_ms, **_ic(cmp.li_ms, cmp.ls_ms),
                            "sessoes": sorted({bl.sessao for bl in ev_conf.blocos})},
            "selecao": {"delta_ms": sel.delta_ms, **_ic(sel.li_ms, sel.ls_ms)},
            "reproducao": reproducao}


def avaliacao_independente(an_sel: Analise, aid: int, rotulo: Dict[int, str],
                           referencia: FrozenSet[int]) -> dict:
    """Todas as configurações medidas à parte para a adaptação ``aid``
    (finalidade 'confirmacao'), na mesma régua: as médias vêm SÓ dessas
    sessões; variâncias, γ e β, de toda a evidência.

    É a comparação de métodos em condições equivalentes: cada conjunto
    medido de novo, na mesma janela de tempo, com a sua linha de base ao lado.
    O ganho é separado em duas partes, porque elas respondem perguntas
    diferentes: o das queries cuja linha de base é só piso (no TPC-H, Q17 e
    Q20: ≥ 300 s cada, e quem as tira do timeout leva > 99% de qualquer
    total) e o do resto, onde os métodos de fato diferem.
    """
    exp = an_sel.exp
    nome = lambda q: rotulo.get(q, f"#{q}")                          # noqa: E731
    ev_conf = exp.evidencia(so_confirmacao_de=aid)
    ev_tudo = est.Evidencia(an_sel.ev.blocos + ev_conf.blocos,
                            {**an_sel.ev.uso, **ev_conf.uso}, ev_conf.peso)
    ruido = est.estimar_ruido(ev_tudo)
    am = est.Amostrador(ev_conf, ruido, an_sel.par.amostras, an_sel.par.semente)
    conf = an_sel.par.confianca
    configs = sorted({c for (_s, c, _q) in ev_conf.uso},
                     key=lambda c: (exp.mb(c), sorted(c)))
    pisos = [q for q in ev_conf.peso if am.termo(q, VAZIA).piso]
    resto = [q for q in ev_conf.peso if q not in pisos]
    margem, fonte = an_sel.margem(referencia)
    linhas = []
    pontos = {}
    for c in configs:
        total = est.comparar(am, VAZIA, c, conf)
        fora = est.comparar(am, VAZIA, c, conf, queries=resto)
        cf = exp.configuracoes.get(c)
        d = {"rotulo": _c(c), "chave": sorted(c),
             "otimizacoes": cf.otimizacoes if cf else [],
             "origem": cf.rotulo if cf else "-", "mb": round(exp.mb(c), 1),
             "sessoes": sorted({s for (s, cc, _q) in ev_conf.uso if cc == c}),
             "beneficio_ms": total.delta_ms, **{f"beneficio_{k}": v for k, v
                                               in _ic(total.li_ms, total.ls_ms).items()},
             "fora_dos_pisos_ms": fora.delta_ms,
             **{f"fora_{k}": v for k, v in _ic(fora.li_ms, fora.ls_ms).items()},
             "pisos": {nome(q): am.termo(q, c).ponto for q in pisos},
             "pisos_resolvidos": [nome(q) for q in pisos if not am.termo(q, c).piso],
             "melhoram": [(nome(e.query), _num(e.ganho_pct)) for e in total.melhoram()],
             "pioram": [(nome(e.query), _num(e.ganho_pct)) for e in total.pioram()],
             "sessoes_discordam": [nome(e.query) for e in total.por_query if e.dispersa]}
        if c != referencia:
            x = est.comparar(am, referencia, c, conf)
            d["contra_referencia"] = {"delta_ms": x.delta_ms, **_ic(x.li_ms, x.ls_ms),
                                      "afirmacao": est.ordenar(x.li_ms, x.ls_ms, margem)}
        pontos[c] = est.Ponto(c, total.delta_ms, exp.mb(c))
        linhas.append(d)
    for d in linhas:
        c = frozenset(d["chave"])
        d["dominada_por"] = None
        for o, po in pontos.items():
            if o != c and est.domina(po, pontos[c]) and est.dominada_com_evidencia(
                    po, pontos[c], est.comparar(am, c, o, conf), margem):
                d["dominada_por"] = _c(o)
                break
    return {"adaptacao": aid, "referencia": _c(referencia), "margem_ms": margem,
            "fonte_margem": fonte, "queries_com_piso": [nome(q) for q in pisos],
            "pareamento": {"beta": ruido.beta, "ep": ruido.beta_ep},
            "configuracoes": linhas}


def mostrar_avaliacao(av: dict) -> None:
    print(f"\n  avaliação independente (só sessões de confirmação da adaptação "
          f"{av['adaptacao']}; referência {av['referencia']}, margem "
          f"{av['margem_ms'] / 1000:,.1f} s)")
    pis = ", ".join(av["queries_com_piso"]) or "-"
    print(f"  {'configuração':<24} {'MB':>6} {'ses':>3}  {'fora de ' + pis + ' (s)':>28}"
          f"  {pis + ' depois (ms)':>18} {'+':>2} {'−':>2}  contra a referência")
    for d in av["configuracoes"]:
        pisos = sum(d["pisos"].values())
        ref = d.get("contra_referencia")
        txt_ref = ("—" if not ref else
                   f"{_s(ref['delta_ms'])} {_ic_txt(ref)} {ref['afirmacao']}")
        dom = f"; dominada por {d['dominada_por']}" if d["dominada_por"] else ""
        print(f"  {d['rotulo'][:24]:<24} {d['mb']:>6,.1f} {len(d['sessoes']):>3}  "
              f"{_s(d['fora_dos_pisos_ms']) + ' ' + _ic_txt(d, 'fora_'):>28}  "
              f"{pisos:>18,.0f} {len(d['melhoram']):>2} {len(d['pioram']):>2}  "
              f"{txt_ref}{dom}")


# --------------------------------------------------------------------------
# Com banco: preparar, executar, listar
# --------------------------------------------------------------------------

SQL_CUSTOS = """
SELECT DISTINCT ON (chave) chave, ms_por_escrita
  FROM monitoring.custo_manutencao
 WHERE sessao_id = %s AND ms_por_escrita IS NOT NULL
 ORDER BY chave, medido_em DESC
"""

SQL_LISTAR = """
SELECT a.id, a.iniciada_em, a.sessao_id, a.modo, a.estado, a.medicoes,
       a.validacoes_lidas, a.recomendada, a.confirmada, a.motivo
  FROM monitoring.adaptacao a
 ORDER BY a.id DESC LIMIT 20
"""

SQL_INDICES_OBSERVADOS = """
SELECT indexname, indexdef FROM pg_indexes
 WHERE schemaname = 'public' AND left(indexname, %s) <> %s
"""


def carregar_escritas(conn, exp: evd.Experimento) -> List[mnt.Escrita]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT database_name FROM monitoring.query_resumo "
                    " WHERE query_ref = ANY (%s)", (sorted(exp.inst.peso),))
        bancos = [r[0] for r in cur.fetchall()]
        cur.execute(otimizacao.SQL_ESCRITAS, (bancos,))
        linhas = cur.fetchall()
    saida = []
    for texto, execucoes in linhas:
        e = mnt.classificar(texto or "", float(execucoes or 0))
        if e:
            saida.append(e)
    return saida


def _catalogo(exp: evd.Experimento, obs=None
              ) -> Tuple[Dict[str, FrozenSet[str]], Optional[Dict[str, List[str]]], List[str]]:
    """(dependências dos índices existentes por tabela, colunas por tabela,
    avisos). Do ambiente de referência gravado, ou do banco observado."""
    from .ambiente import PREFIXO_EXPERIMENTO
    avisos: List[str] = []
    amb_ref = exp.ambiente_matriz or next(
        (v.ambiente for v in exp.validacoes
         if v.ambiente_hash and v.ambiente_hash == exp.referencia_hash), None)
    defs: Dict[str, str] = {}
    colunas = None
    if amb_ref:
        defs = (amb_ref.get("dados") or {}).get("indices_existentes") or {}
    if obs is not None:
        try:
            from . import candidatos
            colunas = candidatos.catalogo(obs)
            if not defs:
                with obs.cursor() as cur:
                    cur.execute(SQL_INDICES_OBSERVADOS,
                                (len(PREFIXO_EXPERIMENTO), PREFIXO_EXPERIMENTO))
                    defs = dict(cur.fetchall())
                obs.commit()
                avisos.append("índices existentes lidos do banco observado AGORA, "
                              "não do ambiente da medição")
        except Exception as erro:                   # noqa: BLE001
            obs.rollback()
            avisos.append(f"catálogo do banco observado inacessível ({erro})")
    if not defs:
        avisos.append("índices existentes desconhecidos: a exposição à escrita "
                      "ignora quais UPDATE já não são HOT hoje")
    existentes: Dict[str, Set[str]] = {}
    for ddl in defs.values():
        tabela = mnt.dependencias_do_indice(ddl)[0] or ""
        t, deps = mnt.dependencias_do_indice(ddl, (colunas or {}).get(tabela))
        if t:
            existentes.setdefault(t, set()).update(deps)
    return {t: frozenset(d) for t, d in existentes.items()}, colunas, avisos


def limiares_de_ruido(ruido: est.ModeloRuido, confianca: float
                      ) -> Optional[Dict[int, float]]:
    """O corte de ruído da PROPOSTA, por query, no lugar dos 10% fixos: a
    diferença entre um bloco com o índice e um sem que a variação entre
    sessões daquela query já explica. None sem modelo de ruído (aí fica o
    limiar fixo de `otimizar`)."""
    if not ruido.suficiente:
        return None
    return {q: ruido.limiar_pct(q, confianca) for q in ruido.por_query}


def preparar(conn, sessao: int, par: Parametros, obs=None) -> Analise:
    """A análise de seleção da sessão, com o corte de ruído da proposta por
    query derivado do próprio modelo de ruído."""
    exp0 = evd.carregar(conn, sessao, par.peso)
    ruido = est.estimar_ruido(exp0.evidencia())
    exp = exp0
    limiares = limiares_de_ruido(ruido, par.confianca)
    if limiares:
        exp = evd.carregar(conn, sessao, par.peso, limiar_por_query=limiares)
    existentes, colunas, avisos = _catalogo(exp, obs)
    with conn.cursor() as cur:
        cur.execute(SQL_CUSTOS, (sessao,))
        custos = {k: float(v) for k, v in cur.fetchall()}
    an = analisar(exp, par, carregar_escritas(conn, exp), existentes, colunas,
                  custos, ev=exp.evidencia(), ruido=ruido)
    an.ruido.avisos.extend(avisos)
    return an


def executar(conn, b, queries, sessao: int, par: Parametros, rotulo: Dict[int, str],
             obs=None, forcar: bool = False, modo: str = "execucao",
             medir: bool = True) -> Tuple[Analise, Trajetoria, dict, int]:
    """A exploração com a bancada: decide, mede o que a decisão pede, decide
    de novo, até parar ou acabar o orçamento de medições. Sem ``medir``,
    só decide sobre a evidência existente e diz qual seria a próxima."""
    aid = abrir(conn, sessao, par, modo)
    medicoes = 0
    an = traj = None
    try:
        while True:
            an = preparar(conn, sessao, par, obs)
            traj = caminhar(an)
            if traj.proxima is None or not medir or medicoes >= par.max_medicoes:
                break
            px = traj.proxima
            oid = otimizacao_para(conn, an, px.configuracao, aid, px.movimento)
            print(f"\n>>> medição {medicoes + 1} de no máximo {par.max_medicoes}: "
                  f"otimização {oid} {_c(px.configuracao)} — {px.motivo}")
            otimizacao.validar(conn, b, oid, queries, nova=True,
                               finalidade="selecao", ordem=px.ordem,
                               origem="adaptar", adaptacao_id=aid, forcar=forcar,
                               movimento=px.movimento)
            medicoes += 1
    except KeyboardInterrupt:
        if an is not None and traj is not None:
            fechar(conn, aid, an, traj, relatorio(an, traj, rotulo), medicoes,
                   estado="interrompida")
        raise
    rel = relatorio(an, traj, rotulo)
    if traj.proxima is not None:
        oid = otimizacao_para(conn, an, traj.proxima.configuracao, aid,
                              traj.proxima.movimento)
        rel["proxima"]["otimizacao"] = oid
        rel["proxima"]["comando"] = (
            f"validar --otimizacao {oid} --nova-validacao --ordem {traj.proxima.ordem}"
            f" --movimento {traj.proxima.movimento}"
            f"   (ou: adaptar --sessao {sessao} --executar)")
        if medir and medicoes >= par.max_medicoes:
            rel["motivo"] += f" (limite de {par.max_medicoes} medições desta execução)"
    rel["adaptacao"] = aid
    rel["medicoes_nesta_execucao"] = medicoes
    if rel.get("multiplicidade"):
        rel["multiplicidade"]["confirmacao"] = (
            f"pendente: adaptar --sessao {sessao} --confirmar {aid} --executar"
            if rel["caminho"] else "nada a confirmar: nenhum movimento aceito")
    fechar(conn, aid, an, traj, rel, medicoes)
    return an, traj, rel, aid


def listar(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(SQL_LISTAR)
        linhas = cur.fetchall()
    if not linhas:
        print("nenhuma adaptação ainda. Rode: adaptar --sessao N")
        return 0
    print(f"{'id':>4}  {'quando':<16} {'sessão':>6} {'modo':<11} {'estado':<19} "
          f"{'med.':>4} {'lidas':>5} {'recom.':>6} {'confirmada':<10} motivo")
    for aid, quando, sessao, modo, estado, med, lidas, rec, conf, motivo in linhas:
        confirmada = "-" if conf is None else ("sim" if conf else "NÃO")
        print(f"{aid:>4}  {str(quando)[:16]:<16} {sessao:>6} {modo:<11} {estado:<19} "
              f"{med:>4} {lidas or 0:>5} {rec or '-':>6} {confirmada:<10} "
              f"{(motivo or '')[:60]}")
    return 0


def mostrar_ruido(an: Analise, rotulo: Dict[int, str]) -> None:
    r = resumo_ruido(an, rotulo)
    print(f"\nruído da sessão {an.exp.sessao}: "
          f"{len({b.sessao for b in an.ev.blocos})} sessões de medida")
    if not r["suficiente"]:
        for a in r["avisos"]:
            print(f"  {a}")
        return
    print(f"  d0 = {r['d0']:.1f} (quanto cada query empresta da carga), "
          f"τ típico = {r['s0']:.3f}")
    print(f"  pareamento β = {r['beta']:.2f} ± {r['beta_ep']:.2f} "
          f"({r['beta_pontos']} pontos), σ_γ = {r['sigma_gama']:.3f}")
    print(f"\n  {'query':<7} {'blocos':>6} {'gl':>4} {'σ_ε':>7} {'σ_α':>7} "
          f"{'σ_α/σ_ε':>8} {'τ mod.':>7} {'corte matriz':>12}")
    for l in r["por_query"]:
        razao = (l["sigma_alfa"] / l["sigma_eps"]) if l["sigma_eps"] else None
        txt_razao = f"{razao:.1f}" if razao is not None else "-"
        print(f"  {l['query']:<7} {l['blocos']:>6} {l['gl_entre']:>4.0f} "
              f"{(l['sigma_eps'] or 0):>7.3f} {l['sigma_alfa']:>7.3f} "
              f"{txt_razao:>8} {l['tau_moderado']:>7.3f} "
              f"{l['limiar_matriz_pct']:>11.1f}%")
    print("  σ em escala log (0,10 ≈ 10%). σ_ε: entre repetições do mesmo bloco; "
          "σ_α: o que a sessão acrescenta.")
    print("  corte matriz: abaixo disto, a diferença entre UM bloco com o índice e "
          "UM sem não se distingue de ruído")
    for s, g in sorted(r["deslocamento_por_sessao"].items()):
        print(f"  γ {s:<8} {g:+.1%}")
