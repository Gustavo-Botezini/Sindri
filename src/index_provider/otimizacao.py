#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seleção de índices como problema de otimização, sem agrupamento.

O modo de clusters decide um índice por cluster, e cada decisão é local: o
índice do cluster 1 não sabe que o do cluster 3 já atende metade das queries
dele, nem que piora a Q5 do cluster 2. A matriz mostrou o preço disso no
TPC-H: lineitem(l_partkey) ajudou cinco queries espalhadas por três
clusters, e orders(o_custkey) entrou pela Q22 (+59%) levando junto a Q5
(−74%) e a Q8 (−90%).

Aqui não há partição. O conjunto inteiro é escolhido de uma vez, para a carga
inteira:

    max   Σ_q w_q · Δ(q, S)  −  λ · MB(S)  −  μ · Escritas(S)
    S ⊆ C
    sujeito a  MB(S) ≤ orçamento

    C   os índices de uma sessão da matriz: os que as rodadas propuseram, os
        que a heurística propõe para o corpus inteiro (`matriz --globais`),
        ou os dois
    w_q execuções da query no corpus (ou 1, com --peso uniforme)
    Δ   ms poupados por execução, MEDIDOS pela matriz
    MB  tamanho real do índice, medido no CREATE INDEX da matriz
    μ   ms de manutenção por escrita na tabela do índice. 0 por padrão,
        porque ninguém mediu; com --peso execucoes, as escritas contam
        execuções, com uniforme, comandos distintos

**A matriz mede Δ(q, i), um índice por vez; a função pede Δ(q, S).** O que
liga uma coisa à outra é uma hipótese, e há duas (``--modelo``):

    conservador  Δ(q,S) = o melhor ganho entre os índices de S, menos TODAS as
                 regressões que índices de S causaram em q. Supõe que o
                 planner pode usar justamente o índice que piora.
    otimista     Δ(q,S) = o Δ do melhor índice de S que q usa: o planner
                 escolhe bem. Regressão só conta quando todo índice de S que
                 a query usa piora.

Nenhuma das duas é o que acontece. Por isso existe ``validar``, que cria o
conjunto escolhido inteiro, remede a carga e põe o Δ(q,S) medido ao lado do
previsto. A distância entre os dois é o erro do modelo, e qual hipótese erra
menos é parte do experimento.

**É exato, não guloso.** Com Δ(q,S) assim, o problema é um programa linear
inteiro do tipo localização de facilidades: x_i diz se o índice entra, y_qi
qual índice atende a query. Com dezenas de índices e de queries, o
``scipy.optimize.milp`` (HiGHS) resolve em milissegundos, e o ótimo é o ótimo
do modelo, não de uma busca. A explosão combinatória da formulação geral some
porque Δ(q,S) passa a ser consultado numa tabela medida uma vez por índice, e
não medido na bancada a cada combinação.

**Ruído não é ganho.** Δ entre −limiar% e +limiar% vira 0, com o mesmo
``--limiar`` de ``comparar`` (10%). No YCSB as OLAP variaram até 10% sem
tocar o índice; sem esse corte o solver compraria um índice de 486 MB para
ganhar 2%.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from psycopg.types.json import Jsonb

from . import bancada as bc
from . import candidatos
from .corpus import Query
from .matriz import LIMIAR_PADRAO, indices_das_rodadas

MODELOS = ("conservador", "otimista")
PESOS = ("execucoes", "uniforme")
MB = 1048576.0

# Desempate: entre dois conjuntos de mesmo valor, o menor. Sem isto, com λ = 0
# um índice de ganho zero custa zero, e o solver o leva ou não, ao acaso.
DESEMPATE_POR_MB = 1e-6


# --------------------------------------------------------------------------
# A instância, lida da matriz
# --------------------------------------------------------------------------

SQL_INDICES = """
SELECT id, chave, ddl, relacao, tamanho_bytes, build_ms, origem
  FROM monitoring.matriz_indice
 WHERE sessao_id = %s AND erro IS NULL AND tamanho_bytes IS NOT NULL
 ORDER BY id
"""

# As queries são as que têm linha de base na sessão, e não as do corpus de
# agora: o corpus pode ter crescido depois da medição.
SQL_QUERIES = """
SELECT DISTINCT m.query_ref, r.execucoes, r.database_name, r.query_text
  FROM monitoring.matriz_medida m
  JOIN monitoring.query_resumo r ON r.query_ref = m.query_ref
 WHERE m.sessao_id = %s AND m.indice_id IS NULL
"""

SQL_RESULTADO = """
SELECT indice_id, query_ref, usou_indice, antes_ms, depois_ms,
       antes_censurado, depois_censurado
  FROM monitoring.matriz_resultado
 WHERE sessao_id = %s
"""

SQL_ESCRITAS = """
SELECT query_text, execucoes
  FROM monitoring.query_resumo
 WHERE database_name = ANY (%s)
   AND upper(query_text) ~ '^\\s*(INSERT|UPDATE|DELETE)\\M'
"""

# A tabela que o comando de escrita altera. relacoes, no resumo, traz também
# as tabelas lidas pelas subqueries, e índice nelas não paga manutenção.
_RE_ALVO = re.compile(
    r"""^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?
        (?:(?:"[^"]+"|\w+)\.)?("[^"]+"|\w+)""",
    re.IGNORECASE | re.VERBOSE)

_RE_CABECA_DDL = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON\s+",
    re.IGNORECASE)


def curto(ddl: str) -> str:
    """'lineitem (l_partkey)': o DDL sem o CREATE INDEX <nome> ON."""
    return " ".join(_RE_CABECA_DDL.sub("", ddl).split())


def relacao_do_ddl(ddl: str, relacao: Optional[str] = None) -> Optional[str]:
    """A relação gravada, ou a que vem depois do ON quando ela falta."""
    if relacao:
        return relacao
    m = re.match(r'(?:ONLY\s+)?((?:"[^"]+"|[\w.])+)', curto(ddl), re.IGNORECASE)
    return m.group(1).strip('"').lower() if m else None


def _f(valor) -> Optional[float]:
    return None if valor is None else float(valor)


@dataclass
class Indice:
    id: int
    chave: str
    ddl: str
    relacao: Optional[str]
    tamanho_bytes: int
    build_ms: Optional[float]
    origem: Optional[str]
    # Escritas na relação, ponderadas como as queries.
    escritas: float = 0.0

    @property
    def mb(self) -> float:
        return self.tamanho_bytes / MB

    @property
    def curto(self) -> str:
        return curto(self.ddl)


@dataclass
class Instancia:
    sessao: int
    peso_tipo: str
    limiar: float
    indices: Dict[int, Indice]
    # query_ref -> w_q
    peso: Dict[int, float]
    # (query_ref, indice_id) -> Δ em ms por execução, já com o corte de ruído.
    # Só os pares em que o plano usa o índice: quem não usa não tem Δ.
    delta: Dict[Tuple[int, int], float]
    # Pares em que Δ é piso: um dos lados estourou o timeout.
    pisos: Set[Tuple[int, int]] = field(default_factory=set)
    # Pares em que a matriz não sabe Δ (timeout dos dois lados, erro): contam 0.
    desconhecidos: Set[Tuple[int, int]] = field(default_factory=set)
    textos: Dict[int, str] = field(default_factory=dict)


def delta_medido(antes: Optional[float], depois: Optional[float],
                 antes_cens: bool, depois_cens: bool,
                 limiar: float) -> Tuple[Optional[float], bool]:
    """(Δ em ms por execução, é piso?). Δ None quando a medição não diz.

    Os pisos da matriz atravessam: com o "antes" censurado o ganho real é
    maior; com o "depois" censurado a perda real é maior. Os dois censurados
    não dizem nada.
    """
    if antes is None or depois is None or (antes_cens and depois_cens):
        return None, False
    d = antes - depois
    if antes > 0 and abs(100.0 * d / antes) < limiar:
        return 0.0, False
    return d, bool(antes_cens or depois_cens)


def carregar(conn, sessao: int, peso: str = "execucoes",
             limiar: float = LIMIAR_PADRAO,
             limiar_por_query: Optional[Dict[int, float]] = None) -> Instancia:
    """A instância da sessão.

    ``limiar_por_query`` troca o corte de ruído fixo por um por query, em %:
    a seleção adaptativa o deriva da variabilidade medida entre sessões
    (``adaptativa.limiares_de_ruido``). Query fora do dicionário fica com
    ``limiar``.
    """
    with conn.cursor() as cur:
        cur.execute(SQL_INDICES, (sessao,))
        indices = {r[0]: Indice(id=r[0], chave=r[1], ddl=r[2],
                                relacao=relacao_do_ddl(r[2], r[3]),
                                tamanho_bytes=int(r[4]), build_ms=_f(r[5]),
                                origem=r[6])
                   for r in cur.fetchall()}
        cur.execute(SQL_QUERIES, (sessao,))
        queries = cur.fetchall()
        cur.execute(SQL_RESULTADO, (sessao,))
        resultado = cur.fetchall()
        cur.execute(SQL_ESCRITAS, (sorted({r[2] for r in queries}),))
        escritas = cur.fetchall()
    if not indices:
        raise SystemExit(f"sessão {sessao}: nenhum índice medido sem erro. "
                         f"Rode antes: matriz --globais N (ou --rodadas)")

    def w(execucoes) -> float:
        return float(execucoes) if peso == "execucoes" else 1.0

    inst = Instancia(sessao=sessao, peso_tipo=peso, limiar=limiar,
                     indices=indices,
                     peso={ref: w(execucoes) for ref, execucoes, _, _ in queries},
                     delta={},
                     textos={ref: texto for ref, _, _, texto in queries})

    for indice_id, ref, usou, antes, depois, a_cens, d_cens in resultado:
        if not usou or indice_id not in indices or ref not in inst.peso:
            continue
        lim = (limiar_por_query or {}).get(ref, limiar)
        d, piso = delta_medido(_f(antes), _f(depois), a_cens, d_cens, lim)
        if d is None:
            inst.desconhecidos.add((ref, indice_id))
            d = 0.0
        if piso:
            inst.pisos.add((ref, indice_id))
        inst.delta[(ref, indice_id)] = d

    por_tabela: Dict[str, float] = {}
    for texto, execucoes in escritas:
        m = _RE_ALVO.match(texto or "")
        if m:
            tabela = m.group(1).strip('"').lower()
            por_tabela[tabela] = por_tabela.get(tabela, 0.0) + w(execucoes)
    for ind in indices.values():
        ind.escritas = por_tabela.get((ind.relacao or "").lower(), 0.0)
    return inst


# --------------------------------------------------------------------------
# O modelo: avaliar um conjunto, e achar o melhor
# --------------------------------------------------------------------------

@dataclass
class Parametros:
    modelo: str = "conservador"
    peso: str = "execucoes"
    limiar: float = LIMIAR_PADRAO
    orcamento_mb: Optional[float] = None
    lambda_mb: float = 0.0          # ms por MB
    custo_escrita: float = 0.0      # ms por escrita na tabela, por índice
    # Preenchido quando o conjunto não saiu do solver, e sim de uma rodada de
    # clusters avaliada na mesma régua.
    rodada: Optional[int] = None

    def como_dict(self) -> dict:
        return asdict(self)


@dataclass
class Avaliacao:
    indices: List[int]
    # query_ref -> {peso, delta_ms, atende, pioram, piso}
    por_query: Dict[int, dict]
    beneficio_ms: float             # Σ w_q · Δ(q, S)
    custo: float                    # λ · MB(S) + μ · Escritas(S)
    mb: float

    @property
    def objetivo(self) -> float:
        return self.beneficio_ms - self.custo


def custo_fixo(inst: Instancia, par: Parametros, i: int) -> float:
    """O que o índice custa só por existir: espaço e manutenção."""
    ind = inst.indices[i]
    return par.lambda_mb * ind.mb + par.custo_escrita * ind.escritas


def delta_conjunto(inst: Instancia, par: Parametros, q: int,
                   S: Sequence[int]) -> Tuple[float, Optional[int], List[int]]:
    """Δ(q, S) pelo modelo de interação: (ms por execução, quem atende, quem piora)."""
    usados = [(inst.delta[(q, i)], i) for i in S if (q, i) in inst.delta]
    if par.modelo == "conservador":
        ganhos = [t for t in usados if t[0] > 0]
        melhor, atende = max(ganhos) if ganhos else (0.0, None)
        pioram = sorted(i for d, i in usados if d < 0)
        return melhor + sum(d for d, _ in usados if d < 0), atende, pioram
    if not usados:
        return 0.0, None, []
    melhor, atende = max(usados)
    return melhor, atende, ([atende] if melhor < 0 else [])


def avaliar(inst: Instancia, par: Parametros, S: Sequence[int]) -> Avaliacao:
    """O valor de um conjunto qualquer, no mesmo modelo que o solver usa.

    É o que põe o conjunto de uma rodada de clusters e o da otimização na
    mesma régua.
    """
    S = sorted(i for i in set(S) if i in inst.indices)
    por_query: Dict[int, dict] = {}
    beneficio = 0.0
    for q, w in inst.peso.items():
        d, atende, pioram = delta_conjunto(inst, par, q, S)
        envolvidos = ([atende] if atende is not None else []) + pioram
        por_query[q] = {"peso": w, "delta_ms": d, "atende": atende,
                        "pioram": pioram,
                        "piso": any((q, i) in inst.pisos for i in envolvidos)}
        beneficio += w * d
    return Avaliacao(indices=S, por_query=por_query, beneficio_ms=beneficio,
                     custo=sum(custo_fixo(inst, par, i) for i in S),
                     mb=sum(inst.indices[i].mb for i in S))


def resolver(inst: Instancia, par: Parametros) -> Tuple[Avaliacao, str]:
    """O conjunto ótimo do modelo, pelo programa inteiro. Devolve (avaliação, status).

    Variáveis: x_i (o índice entra) e y_qi (o índice i atende a query q).

        y_qi ≤ x_i                   só atende quem entrou
        Σ_i y_qi ≤ 1                 cada query fica com um índice: o melhor
        Σ_i y_qi ≥ x_j               (otimista) se q usa j e j entrou, q é
                                     atendida por algum dos que entraram,
                                     mesmo que todos piorem
        Σ_i MB_i · x_i ≤ orçamento

    No conservador só os ganhos viram y; cada regressão vira custo fixo do
    índice que a causa, pago sempre que ele entra.
    """
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_array

    ids = sorted(inst.indices)
    nx = len(ids)
    col = {i: k for k, i in enumerate(ids)}
    pares = sorted(p for p in inst.delta if p[1] in col and p[0] in inst.peso)
    if par.modelo == "conservador":
        atribuiveis = [p for p in pares if inst.delta[p] > 0]
    else:
        atribuiveis = pares
    n = nx + len(atribuiveis)

    # milp minimiza: custo entra positivo, ganho negativo.
    c = np.zeros(n)
    for i in ids:
        c[col[i]] = custo_fixo(inst, par, i) + DESEMPATE_POR_MB * inst.indices[i].mb
    if par.modelo == "conservador":
        for q, i in pares:
            if inst.delta[(q, i)] < 0:
                c[col[i]] -= inst.peso[q] * inst.delta[(q, i)]
    for k, (q, i) in enumerate(atribuiveis):
        c[nx + k] = -inst.peso[q] * inst.delta[(q, i)]

    linhas: List[int] = []
    colunas: List[int] = []
    valores: List[float] = []
    lb: List[float] = []
    ub: List[float] = []

    def restricao(coefs, minimo, maximo):
        r = len(lb)
        for j, v in coefs:
            linhas.append(r)
            colunas.append(j)
            valores.append(v)
        lb.append(minimo)
        ub.append(maximo)

    por_query: Dict[int, List[Tuple[int, int]]] = {}
    for k, (q, i) in enumerate(atribuiveis):
        por_query.setdefault(q, []).append((nx + k, i))
        restricao([(nx + k, 1.0), (col[i], -1.0)], -np.inf, 0.0)
    for q, ys in por_query.items():
        soma = [(j, 1.0) for j, _ in ys]
        restricao(soma, -np.inf, 1.0)
        if par.modelo == "otimista":
            for _, i in ys:
                restricao(soma + [(col[i], -1.0)], 0.0, np.inf)
    if par.orcamento_mb is not None:
        restricao([(col[i], inst.indices[i].mb) for i in ids],
                  -np.inf, par.orcamento_mb)

    restricoes = None
    if lb:
        A = coo_array((valores, (linhas, colunas)), shape=(len(lb), n))
        restricoes = [LinearConstraint(A, lb, ub)]
    res = milp(c, integrality=np.ones(n), bounds=Bounds(0, 1),
               constraints=restricoes)
    if res.x is None:
        raise RuntimeError(f"o solver não achou solução: {res.message}")
    escolhidos = [i for i in ids if res.x[col[i]] > 0.5]
    return avaliar(inst, par, escolhidos), res.message


def conjunto_da_rodada(conn, inst: Instancia,
                       rodada: int) -> Tuple[List[int], List[str]]:
    """Os índices que a rodada propôs, achados na sessão; e os que ela não mediu."""
    por_chave = {ind.chave: ind.id for ind in inst.indices.values()}
    ids: List[int] = []
    fora: List[str] = []
    for chave, ddl, _relacao in indices_das_rodadas(conn, [rodada]):
        if chave in por_chave:
            ids.append(por_chave[chave])
        else:
            fora.append(ddl)
    return ids, fora


# --------------------------------------------------------------------------
# Gravar e mostrar
# --------------------------------------------------------------------------

SQL_GRAVAR = """
INSERT INTO monitoring.otimizacao
       (sessao_id, parametros, status, indices, armazenamento_bytes,
        beneficio_previsto_ms, objetivo, previsao)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

SQL_LISTAR = """
SELECT o.id, o.criado_em, o.sessao_id, o.parametros,
       cardinality(o.indices) AS indices,
       o.armazenamento_bytes, o.beneficio_previsto_ms, o.objetivo, o.validado_em,
       (SELECT count(*) FROM monitoring.otimizacao_validacao v
         WHERE v.otimizacao_id = o.id AND v.concluida_em IS NOT NULL) AS validacoes
  FROM monitoring.otimizacao o
 ORDER BY o.id DESC
 LIMIT 20
"""

# O mesmo conjunto de índices já gravado na sessão. indices é gravado em
# ordem (avaliar() ordena), então igualdade de array é igualdade de conjunto.
SQL_MESMO_CONJUNTO = """
SELECT id FROM monitoring.otimizacao
 WHERE sessao_id = %s AND indices = %s::bigint[]
 ORDER BY id LIMIT 1
"""


def achar_ou_gravar(conn, inst: Instancia, par: Parametros, av: Avaliacao,
                    status: str) -> Tuple[int, bool]:
    """(id, nova?) da otimização com este conjunto na sessão.

    A seleção adaptativa propõe conjuntos que podem já ter sido escolhidos e
    validados por `otimizar` ou por uma rodada de clusters. Reaproveitar a
    otimização é reaproveitar as validações dela: a evidência é do conjunto,
    não de quem o propôs.
    """
    with conn.cursor() as cur:
        cur.execute(SQL_MESMO_CONJUNTO, (inst.sessao, sorted(av.indices)))
        linha = cur.fetchone()
    if linha:
        return linha[0], False
    return gravar(conn, inst, par, av, status), True


def gravar(conn, inst: Instancia, par: Parametros, av: Avaliacao,
           status: str) -> int:
    with conn.cursor() as cur:
        cur.execute(SQL_GRAVAR, (
            inst.sessao, Jsonb(par.como_dict()), status, av.indices,
            sum(inst.indices[i].tamanho_bytes for i in av.indices),
            av.beneficio_ms, av.objetivo,
            Jsonb({str(q): v for q, v in av.por_query.items()})))
        oid = cur.fetchone()[0]
    conn.commit()
    return oid


def como_dict(inst: Instancia, par: Parametros, av: Avaliacao,
              status: Optional[str] = None, oid: Optional[int] = None) -> dict:
    return {
        "otimizacao": oid,
        "sessao": inst.sessao,
        "status": status,
        "parametros": par.como_dict(),
        "indices": [{"id": i, "ddl": inst.indices[i].ddl,
                     "mb": round(inst.indices[i].mb, 1),
                     "origem": inst.indices[i].origem,
                     "escritas": inst.indices[i].escritas}
                    for i in av.indices],
        "armazenamento_mb": round(av.mb, 1),
        "beneficio_previsto_ms": round(av.beneficio_ms, 1),
        "custo": round(av.custo, 1),
        "objetivo": round(av.objetivo, 1),
        "por_query": {str(q): v for q, v in av.por_query.items()},
    }


def listar(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(SQL_LISTAR)
        linhas = cur.fetchall()
    if not linhas:
        print("nenhuma otimização ainda. Rode: otimizar --sessao N")
        return 0
    print(f"{'id':>4}  {'quando':<16} {'sessão':>6} {'modelo':<11} {'peso':<9} "
          f"{'orçam.':>7} {'λ':>5} {'idx':>4} {'MB':>8} {'benefício ms':>14} "
          f"{'validada':<10} {'val.':>4}")
    for (oid, quando, sessao, par, n, bytes_, beneficio, _objetivo,
         validado, validacoes) in linhas:
        orc = par.get("orcamento_mb")
        print(f"{oid:>4}  {str(quando)[:16]:<16} {sessao:>6} "
              f"{par.get('modelo', ''):<11} {par.get('peso', ''):<9} "
              f"{'-' if orc is None else f'{orc:,.0f}':>7} "
              f"{par.get('lambda_mb', 0):>5g} {n:>4} "
              f"{(bytes_ or 0) / MB:>8,.1f} {beneficio or 0:>14,.0f} "
              f"{str(validado)[:10] if validado else '-':<10} {validacoes:>4}")
    return 0


def mostrar_instancia(inst: Instancia) -> None:
    origens: Dict[str, int] = {}
    for ind in inst.indices.values():
        origens[ind.origem or "?"] = origens.get(ind.origem or "?", 0) + 1
    usam = sum(1 for d in inst.delta.values() if d != 0)
    print(f"sessão {inst.sessao}: {len(inst.indices)} índices × "
          f"{len(inst.peso)} queries ("
          + ", ".join(f"{n} de {o}" for o, n in sorted(origens.items())) + ")")
    print(f"  {len(inst.delta)} pares (query, índice) em que o plano usa o "
          f"índice; {usam} fora da faixa de ruído de ±{inst.limiar:g}%")
    print(f"  peso {inst.peso_tipo}")
    if inst.desconhecidos:
        print(f"  AVISO: {len(inst.desconhecidos)} pares sem Δ conhecido "
              f"(timeout antes e depois, ou erro); contam 0")


def mostrar_solucao(inst: Instancia, par: Parametros, av: Avaliacao,
                    status: str, oid: Optional[int],
                    rotulo: Dict[int, str]) -> None:
    def nome(q: int) -> str:
        return rotulo.get(q, f"#{q}")

    orc = "sem limite" if par.orcamento_mb is None else f"{par.orcamento_mb:,.0f} MB"
    print(f"\n=== modelo {par.modelo}, orçamento {orc}, λ {par.lambda_mb:g} ms/MB, "
          f"escrita {par.custo_escrita:g} ms  ({status})")
    if not av.indices:
        print("  conjunto vazio: nenhum índice paga o que custa")
    else:
        print(f"  {len(av.indices)} índice(s), {av.mb:,.1f} MB:")
    for i in av.indices:
        ind = inst.indices[i]
        atende = sorted(nome(q) for q, v in av.por_query.items() if v["atende"] == i)
        pioram = sorted(nome(q) for q, v in av.por_query.items() if i in v["pioram"])
        escritas = f", {ind.escritas:,.0f} escritas" if ind.escritas else ""
        print(f"    i{i:<4} {ind.mb:>8,.1f} MB  {ind.curto}  "
              f"[{ind.origem or '?'}{escritas}]")
        print(f"           atende: {' '.join(atende) or '-'}"
              + (f"   piora: {' '.join(pioram)}" if pioram else ""))
    mudam = sorted(((v["peso"] * v["delta_ms"], q)
                    for q, v in av.por_query.items() if v["delta_ms"]),
                   reverse=True)
    if mudam:
        print("  previsão por query (Δ por execução × peso):")
    for total, q in mudam:
        v = av.por_query[q]
        piso = "  (piso)" if v["piso"] else ""
        print(f"      {nome(q):<6} {v['delta_ms']:>+12,.1f} ms × {v['peso']:<6g} "
              f"= {total:>+14,.0f} ms{piso}")
    print(f"  benefício previsto {av.beneficio_ms:,.0f} ms, custo "
          f"{av.custo:,.0f}, objetivo {av.objetivo:,.0f}")
    if oid is not None:
        print(f"  otimização {oid} gravada. Para medir: validar --otimizacao {oid}")


# --------------------------------------------------------------------------
# A validação: o conjunto inteiro, criado de uma vez
# --------------------------------------------------------------------------

SQL_OTIMIZACAO = """
SELECT sessao_id, indices, previsao, validado_em, parametros
  FROM monitoring.otimizacao WHERE id = %s
"""

SQL_DDLS = """
SELECT id, ddl, relacao FROM monitoring.matriz_indice
 WHERE id = ANY (%s) ORDER BY id
"""

SQL_MEDIDA = """
INSERT INTO monitoring.otimizacao_medida
       (otimizacao_id, validacao, query_ref, fase, repeticao, tempo_ms,
        indices_usados, censurado, erro)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

SQL_VALIDACAO = """
SELECT query_ref, usou_indice, indices_usados, erro, antes_ms, depois_ms,
       antes_censurado, depois_censurado, ganho_ms, ganho_pct
  FROM monitoring.otimizacao_resultado
 WHERE otimizacao_id = %s AND validacao = %s
"""

SQL_VALIDACOES = """
SELECT validacao, finalidade, origem, ordem, ambiente_hash, concluida_em
  FROM monitoring.otimizacao_validacao
 WHERE otimizacao_id = %s
 ORDER BY validacao
"""

# A próxima validação. As medidas entram na conta também: uma validação
# interrompida antes desta tabela deixou linhas sem registro, e a nova não
# pode colidir com elas.
SQL_PROXIMA_VALIDACAO = """
SELECT greatest(
         (SELECT coalesce(max(validacao), 0) FROM monitoring.otimizacao_validacao
           WHERE otimizacao_id = %s),
         (SELECT coalesce(max(validacao), 0) FROM monitoring.otimizacao_medida
           WHERE otimizacao_id = %s)) + 1
"""

SQL_ABRIR_VALIDACAO = """
INSERT INTO monitoring.otimizacao_validacao
       (otimizacao_id, validacao, finalidade, origem, ordem, ambiente,
        ambiente_hash, adaptacao_id, confirma_adaptacao, movimento)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

SQL_FECHAR_VALIDACAO = """
UPDATE monitoring.otimizacao_validacao
   SET concluida_em = now(),
       ambiente = jsonb_set(coalesce(ambiente, '{}'::jsonb),
                            '{concorrencia,ativos_no_fim}', %s)
 WHERE otimizacao_id = %s AND validacao = %s
"""

FINALIDADES = ("selecao", "confirmacao")
ORDENS = ("base_primeiro", "conjunto_primeiro")


def _ler_otimizacao(conn, otimizacao_id: int):
    with conn.cursor() as cur:
        cur.execute(SQL_OTIMIZACAO, (otimizacao_id,))
        linha = cur.fetchone()
    if not linha:
        raise SystemExit(f"otimização {otimizacao_id} não existe. "
                         f"Veja as que existem: otimizar --listar")
    return linha


def _indices_usados(b: bc.Bancada, q: Query,
                    nomes: Dict[str, int]) -> Tuple[Optional[List[int]], Optional[str]]:
    """Quais índices do conjunto o plano cita. EXPLAIN sem ANALYZE."""
    try:
        comando, _via = b._montar(q, analisar=False, formato_json=True)
        with b.obs.cursor() as cur:
            cur.execute(comando)
            plano = cur.fetchone()[0]
        b.obs.commit()
        raiz = plano[0] if isinstance(plano, list) else plano
        citados = {no.get("Index Name") for no in bc._percorrer(raiz.get("Plan", {}))}
        return sorted(nomes[n] for n in citados if n in nomes), None
    except Exception as erro:                   # noqa: BLE001
        b.obs.rollback()
        return None, str(erro)[:400]
    finally:
        b._soltar_preparada()


def _gravar(conn, otimizacao_id: int, validacao: int,
            medidas: Sequence[bc.Medida],
            usados: Optional[List[int]] = None) -> None:
    with conn.cursor() as cur:
        for m in medidas:
            cur.execute(SQL_MEDIDA, (otimizacao_id, validacao, m.query_ref,
                                     m.fase, m.repeticao, m.tempo_ms, usados,
                                     m.censurado, m.erro))
    conn.commit()


def validar(conn, b: bc.Bancada, otimizacao_id: int, queries: Sequence[Query],
            refazer: bool = False, forcar: bool = False, nova: bool = False,
            finalidade: str = "selecao", ordem: str = "base_primeiro",
            origem: str = "validar", adaptacao_id: Optional[int] = None,
            confirma_adaptacao: Optional[int] = None,
            movimento: Optional[str] = None) -> int:
    """Cria o conjunto escolhido inteiro, mede a carga, derruba tudo.
    Devolve o número da validação.

        base_primeiro:     ANALYZE -> linha de base -> CREATE INDEX × n
                           -> ANALYZE -> EXPLAIN -> replay de quem usa -> DROP × n
        conjunto_primeiro: ANALYZE -> CREATE INDEX × n -> ANALYZE -> EXPLAIN
                           -> replay -> DROP × n -> ANALYZE -> linha de base

    A linha de base é medida de novo, e não reaproveitada da matriz: antes e
    depois na mesma sessão, pelo mesmo motivo que a matriz tem a dela.
    Quem não usa nenhum índice do conjunto não é remedido: ganho 0, como na
    matriz.

    Cada chamada é uma validação nova (``nova``) ou substitui todas as
    anteriores (``refazer``). As duas ordens existem para a deriva temporal
    da sessão (cache, vizinhos na VM) não cair sempre do mesmo lado da
    comparação: a seleção adaptativa alterna a ordem entre as validações do
    mesmo conjunto. Alternar dentro da validação, query a query, pediria
    recriar índices de centenas de MB a cada query.
    """
    if finalidade not in FINALIDADES:
        raise ValueError(f"finalidade desconhecida: {finalidade}")
    if ordem not in ORDENS:
        raise ValueError(f"ordem desconhecida: {ordem}")
    sessao, ids, previsao, validado_em, _par = _ler_otimizacao(conn, otimizacao_id)
    if not ids:
        raise SystemExit(f"otimização {otimizacao_id} escolheu o conjunto vazio: "
                         f"nada para criar")
    if validado_em and not (refazer or nova):
        raise SystemExit(f"otimização {otimizacao_id} já foi validada em "
                         f"{validado_em:%Y-%m-%d %H:%M}. --nova-validacao mede "
                         f"de novo e guarda as duas; --refazer apaga a anterior.")
    with conn.cursor() as cur:
        cur.execute(SQL_DDLS, (list(ids),))
        ddls = cur.fetchall()
        cur.execute("SELECT DISTINCT database_name FROM monitoring.query_resumo "
                    " WHERE query_ref = ANY (%s)", ([int(r) for r in previsao],))
        bancos = [r[0] for r in cur.fetchall()]
    alvo = {int(r) for r in previsao}
    do_corpus = [q for q in queries if q.ref in alvo]
    fora = alvo - {q.ref for q in do_corpus}
    if fora:
        print(f"  AVISO: {len(fora)} queries da sessão {sessao} não estão no "
              f"corpus de agora e ficam sem medida: {sorted(fora)}")

    b.preparar_sessao()
    with b.obs.cursor() as cur:
        cur.execute("SELECT current_database()")
        atual = cur.fetchone()[0]
    b.obs.commit()
    if bancos and atual not in bancos:
        raise SystemExit(f"a sessão {sessao} mediu queries de {bancos}, e o banco "
                         f"observado é {atual}. Aponte BROKK_DSN_OBSERVADO para "
                         f"o banco certo.")
    b.checar_ociosidade(forcar)

    replicaveis = []
    for q in do_corpus:
        pode, _via, erro = b.checar_replay(q)
        if pode:
            replicaveis.append(q)
        else:
            print(f"  ref={q.ref:<4} fora (não replicável): "
                  f"{erro.splitlines()[0][:60]}")
    if not replicaveis:
        raise SystemExit("nenhuma query replicável para medir")

    if refazer:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM monitoring.otimizacao_medida "
                        " WHERE otimizacao_id = %s", (otimizacao_id,))
            cur.execute("DELETE FROM monitoring.otimizacao_validacao "
                        " WHERE otimizacao_id = %s", (otimizacao_id,))
            cur.execute("UPDATE monitoring.otimizacao SET validado_em = NULL "
                        " WHERE id = %s", (otimizacao_id,))
        conn.commit()

    amb = b.ambiente(ordem=ordem, finalidade=finalidade)
    with conn.cursor() as cur:
        cur.execute(SQL_PROXIMA_VALIDACAO, (otimizacao_id, otimizacao_id))
        validacao = cur.fetchone()[0]
        cur.execute(SQL_ABRIR_VALIDACAO, (
            otimizacao_id, validacao, finalidade, origem, ordem, Jsonb(amb),
            amb["hash"], adaptacao_id, confirma_adaptacao, movimento))
    conn.commit()
    print(f"\notimização {otimizacao_id}, validação {validacao} ({finalidade}, "
          f"{ordem.replace('_', ' ')}), ambiente {amb['hash']}")

    relacoes = sorted({r for r in (relacao_do_ddl(ddl, rel) for _, ddl, rel in ddls)
                       if r})
    for rel in relacoes:
        b.analisar_relacao(rel)

    def linha_de_base() -> None:
        print(f"\nlinha de base: {len(replicaveis)} queries × {b.repeticoes} "
              f"repetições")
        for q in replicaveis:
            medidas = b.medir(q, "antes", None)
            _gravar(conn, otimizacao_id, validacao, medidas)
            b._linha(q, medidas)

    def conjunto() -> None:
        criados: Dict[str, int] = {}
        try:
            print(f"\nconjunto da otimização {otimizacao_id}:")
            for k, (indice_id, ddl, _rel) in enumerate(ddls, 1):
                final = candidatos.renomear(
                    ddl, f"{candidatos.PREFIXO}otim{otimizacao_id}_{k}")
                nome, build_ms, tamanho = b.criar_indice(final)
                criados[nome] = indice_id
                print(f"  + i{indice_id} {curto(ddl)}: {build_ms / 1000:.1f}s, "
                      f"{(tamanho or 0) / MB:,.1f} MB")
            for rel in relacoes:
                b.analisar_relacao(rel)

            print("\nreplay de quem usa algum índice do conjunto:")
            for q in replicaveis:
                usados, erro = _indices_usados(b, q, criados)
                _gravar(conn, otimizacao_id, validacao,
                        [bc.Medida(q.ref, "depois", 0, erro=erro)], usados)
                if not usados:
                    continue
                medidas = b.medir(q, "depois", None)
                _gravar(conn, otimizacao_id, validacao, medidas, usados)
                b._linha(q, medidas)
                print(f"             usou {' '.join(f'i{i}' for i in usados)}")
        except BaseException:
            # Um CREATE INDEX que falhou deixa a transação abortada, e aí nem o
            # DROP do finally passaria.
            b.obs.rollback()
            raise
        finally:
            # O conjunto sai inteiro, tenha a medição terminado ou não.
            for nome in criados:
                b.derrubar_indice(nome)
            if criados:
                print(f"  - {len(criados)} índice(s) derrubado(s)")

    if ordem == "base_primeiro":
        linha_de_base()
        conjunto()
    else:
        conjunto()
        # A linha de base parte de estatística sem os índices, como a do
        # outro lado parte de estatística com eles.
        for rel in relacoes:
            b.analisar_relacao(rel)
        linha_de_base()

    from . import ambiente as amb_mod
    with conn.cursor() as cur:
        cur.execute(SQL_FECHAR_VALIDACAO, (Jsonb(amb_mod.ativos(b.obs)),
                                           otimizacao_id, validacao))
        cur.execute("UPDATE monitoring.otimizacao SET validado_em = now() "
                    " WHERE id = %s", (otimizacao_id,))
    conn.commit()
    return validacao


def _classe(pct: Optional[float], limiar: float) -> Optional[int]:
    """+1 melhora, 0 neutro, −1 piora, com a mesma faixa de ruído do modelo."""
    if pct is None:
        return None
    return 0 if abs(pct) < limiar else (1 if pct > 0 else -1)


def relatorio(conn, otimizacao_id: int, validacao: Optional[int] = None) -> dict:
    """O previsto ao lado do medido, query por query. Sem bancada.

    Uma validação por vez (padrão: a última concluída). Juntar validações é
    trabalho de `adaptar`, que sabe separar a variação entre sessões.
    """
    sessao, ids, previsao, validado_em, par = _ler_otimizacao(conn, otimizacao_id)
    refs = [int(r) for r in previsao]
    with conn.cursor() as cur:
        cur.execute(SQL_VALIDACOES, (otimizacao_id,))
        validacoes = [{"validacao": v, "finalidade": f, "origem": o, "ordem": ordem,
                       "ambiente_hash": h, "concluida_em": c}
                      for v, f, o, ordem, h, c in cur.fetchall()]
        if validacao is None:
            concluidas = [v["validacao"] for v in validacoes if v["concluida_em"]]
            validacao = concluidas[-1] if concluidas else (
                validacoes[-1]["validacao"] if validacoes else 1)
        cur.execute(SQL_DDLS, (list(ids),))
        indices = {i: curto(ddl) for i, ddl, _ in cur.fetchall()}
        cur.execute(SQL_VALIDACAO, (otimizacao_id, validacao))
        medidas = {r[0]: r for r in cur.fetchall()}
        cur.execute("SELECT query_ref, query_text FROM monitoring.query_resumo "
                    " WHERE query_ref = ANY (%s)", (refs,))
        textos = dict(cur.fetchall())
        # O Δ previsto de cada query em %, para comparar classe com classe:
        # a linha de base da matriz é a régua da previsão.
        cur.execute("SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY tempo_ms), "
                    "       query_ref "
                    "  FROM monitoring.matriz_medida "
                    " WHERE sessao_id = %s AND indice_id IS NULL "
                    "   AND tempo_ms IS NOT NULL "
                    "   AND (censurado OR (repeticao > 1 AND erro IS NULL)) "
                    " GROUP BY query_ref", (sessao,))
        base_matriz = {ref: ms for ms, ref in cur.fetchall()}
    limiar = float(par.get("limiar", LIMIAR_PADRAO))

    queries = []
    for ref in sorted(refs):
        p = previsao[str(ref)]
        m = medidas.get(ref)
        base = base_matriz.get(ref)
        previsto_pct = (100.0 * p["delta_ms"] / base) if base else None
        q = {"query_ref": ref, "peso": p["peso"],
             "previsto_ms": p["delta_ms"], "previsto_pct": previsto_pct,
             "previsto_e_piso": p.get("piso", False),
             "atende_previsto": p["atende"], "pioram_previsto": p["pioram"],
             "medido": m is not None}
        if m:
            q.update({"usou": m[1], "indices_usados": list(m[2] or []),
                      "erro": m[3], "antes_ms": _f(m[4]), "depois_ms": _f(m[5]),
                      "antes_e_piso": m[6], "depois_e_piso": m[7],
                      "medido_ms": _f(m[8]), "medido_pct": _f(m[9])})
        queries.append(q)

    medidas_ok = [q for q in queries if q.get("medido_ms") is not None]
    comparaveis = [q for q in medidas_ok
                   if _classe(q["previsto_pct"], limiar) is not None
                   and _classe(q["medido_pct"], limiar) is not None]
    com_atende = [q for q in medidas_ok if q["atende_previsto"] is not None]
    return {
        "otimizacao": otimizacao_id,
        "sessao": sessao,
        "parametros": par,
        "validado_em": validado_em,
        "validacao": validacao,
        "validacoes": validacoes,
        "indices": {str(i): ddl for i, ddl in indices.items()},
        "queries": queries,
        "textos": textos,
        "totais": {
            "previsto_ms": sum(q["peso"] * q["previsto_ms"] for q in queries),
            "medido_ms": sum(q["peso"] * q["medido_ms"] for q in medidas_ok),
            # As mesmas queries dos dois lados: sem isto, uma query que ficou
            # sem medida some só do lado medido.
            "previsto_nas_medidas_ms": sum(q["peso"] * q["previsto_ms"]
                                           for q in medidas_ok),
            "queries": len(queries),
            "sem_medida": len(queries) - len(medidas_ok),
            "classe_igual": sum(1 for q in comparaveis
                                if _classe(q["previsto_pct"], limiar)
                                == _classe(q["medido_pct"], limiar)),
            "classe_comparaveis": len(comparaveis),
            "atende_conforme": sum(1 for q in com_atende
                                   if q["atende_previsto"] in q["indices_usados"]),
            "atende_previstos": len(com_atende),
            "limiar_pct": limiar,
        },
    }


def mostrar_relatorio(rel: dict, rotulo: Dict[int, str]) -> None:
    def nome(q: int) -> str:
        return rotulo.get(q, f"#{q}")

    def ms(v: Optional[float], piso: bool = False) -> str:
        if v is None:
            return "-"
        return f"{'≥' if piso else ''}{v:,.1f}"

    par = rel["parametros"]
    quando = rel["validado_em"]
    print(f"\notimização {rel['otimizacao']} (sessão {rel['sessao']}, modelo "
          f"{par.get('modelo')}, peso {par.get('peso')})"
          + (f", validada em {quando:%Y-%m-%d %H:%M}" if quando else
             ", AINDA NÃO VALIDADA"))
    concluidas = [v for v in rel.get("validacoes", []) if v["concluida_em"]]
    if len(concluidas) > 1 or (concluidas and concluidas[0]["finalidade"] != "legado"):
        atual = next((v for v in concluidas if v["validacao"] == rel["validacao"]), None)
        print(f"  validação {rel['validacao']} de {len(concluidas)} concluída(s)"
              + (f" ({atual['finalidade']}, {atual['ordem'].replace('_', ' ')})"
                 if atual else "")
              + ". As outras: validar --relatorio --validacao N; todas juntas: adaptar")
    for i, ddl in rel["indices"].items():
        print(f"  i{i}  {ddl}")
    print(f"\n  {'query':<7} {'previsto ms':>12} {'medido ms':>12}   "
          f"{'antes → depois (ms)':<28} usou (previsto)")
    for q in rel["queries"]:
        previsto = f"{q['previsto_ms']:+,.1f}" + ("*" if q["previsto_e_piso"] else "")
        if not q["medido"]:
            print(f"  {nome(q['query_ref']):<7} {previsto:>12} {'sem medida':>12}")
            continue
        medido = "-" if q["medido_ms"] is None else f"{q['medido_ms']:+,.1f}"
        tempos = (f"{ms(q['antes_ms'], q['antes_e_piso'])} → "
                  f"{ms(q['depois_ms'], q['depois_e_piso'])}"
                  if q["usou"] else "não usa o conjunto")
        usou = " ".join(f"i{i}" for i in q["indices_usados"]) or "-"
        prev = f"i{q['atende_previsto']}" if q["atende_previsto"] else "-"
        print(f"  {nome(q['query_ref']):<7} {previsto:>12} {medido:>12}   "
              f"{tempos:<28} {usou} ({prev})")
    t = rel["totais"]
    print(f"\n  Σ peso × Δ nas {t['queries'] - t['sem_medida']} queries medidas: "
          f"previsto {t['previsto_nas_medidas_ms']:,.0f} ms, "
          f"medido {t['medido_ms']:,.0f} ms")
    if t["sem_medida"]:
        print(f"  ({t['sem_medida']} sem medida; previsto em todas: "
              f"{t['previsto_ms']:,.0f} ms)")
    print(f"  classe igual (melhora/neutra/piora, ±{t['limiar_pct']:g}%): "
          f"{t['classe_igual']} de {t['classe_comparaveis']}")
    print(f"  índice que atende conforme o modelo: {t['atende_conforme']} de "
          f"{t['atende_previstos']}")
    print("  * previsão com piso: a matriz tinha timeout num dos lados")
