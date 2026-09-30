#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UMAP + HDBSCAN sobre os vetores das queries.

O clustering aqui **não decide índice**. Ele particiona o corpus em famílias
de forma parecida, para que a bancada possa fazer a única pergunta que
interessa: um índice tirado de UMA query da família serve às outras?

Três escolhas que o tamanho do corpus impõe:

**``min_dist=0.0``.** O padrão do UMAP (0.1) existe para desenho, para os
pontos não se empilharem. Aqui a projeção alimenta um algoritmo de densidade,
e espalhar os pontos só para ficarem bonitos destrói a densidade que o HDBSCAN
vai procurar.

**``n_neighbors=5``.** Medido: o corpus de carga real do banco ``tpch`` tem 27
queries depois dos cortes de leitura, execuções e catálogo. O padrão 15 faria
cada vizinhança cobrir mais da metade do corpus, e a estrutura local
desapareceria.

**``sklearn.cluster.HDBSCAN``**, não o pacote ``hdbscan`` avulso: está no
scikit-learn desde a 1.3 e não exige compilar Cython no venv.

E a advertência que vale registrar: 27 pontos é pouco, o resultado é sensível
aos parâmetros, e é por isso que ``monitoring.rodada_cluster.parametros``
guarda todos eles junto do resultado. Sem isso um cluster é um número que
apareceu; com isso é um experimento que outra pessoa refaz.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Sequence, Tuple

from .corpus import Query


@dataclass
class Parametros:
    """Tudo que muda o resultado, num objeto só — é o que vai para o banco."""
    n_neighbors: int = 5
    min_dist: float = 0.0
    n_components: int = 5
    metric: str = "cosine"
    min_cluster_size: int = 3
    min_samples: int = 2
    cluster_selection_method: str = "eom"
    semente: int = 42

    def como_dict(self) -> dict:
        return asdict(self)


@dataclass
class Resultado:
    refs: List[int]
    rotulos: List[int]
    probabilidades: List[float]
    outliers: List[float]
    xy: List[Tuple[float, float]]

    @property
    def clusters(self) -> int:
        return len({r for r in self.rotulos if r >= 0})

    @property
    def ruido(self) -> int:
        return sum(1 for r in self.rotulos if r < 0)

    def por_cluster(self) -> Dict[int, List[int]]:
        grupos: Dict[int, List[int]] = {}
        for ref, rot in zip(self.refs, self.rotulos):
            grupos.setdefault(rot, []).append(ref)
        return dict(sorted(grupos.items()))


SQL_RODADA = """
INSERT INTO monitoring.rodada_cluster
       (database_name, modelo, normalizacao, queries, clusters, ruido, parametros)
VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

SQL_PONTO = """
INSERT INTO monitoring.query_cluster
       (rodada_id, query_ref, cluster_id, probabilidade, outlier, x, y)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


# --------------------------------------------------------------------------
# O agrupamento
# --------------------------------------------------------------------------

def agrupar(refs: Sequence[int], matriz, par: Parametros,
            verboso: bool = True) -> Resultado:
    """Projeta e agrupa. A matriz é (n_queries, n_dimensoes), já normalizada."""
    import warnings

    import numpy as np
    from sklearn.cluster import HDBSCAN
    import umap

    n = len(refs)
    if n < par.min_cluster_size:
        raise SystemExit(
            f"só {n} queries no corpus, e min_cluster_size é "
            f"{par.min_cluster_size}. Baixe --minimo-execucoes ou "
            f"--tamanho-minimo."
        )
    # UMAP exige n_neighbors < n_amostras; com corpus pequeno o padrão estoura.
    vizinhos = min(par.n_neighbors, n - 1)
    dimensoes = min(par.n_components, n - 2)
    if verboso and (vizinhos != par.n_neighbors or dimensoes != par.n_components):
        print(f"  ajustado ao tamanho do corpus: n_neighbors={vizinhos}, "
              f"n_components={dimensoes}")

    def projetar(saida: int):
        with warnings.catch_warnings():
            # "n_jobs value 1 overridden to 1 by setting random_state": é o
            # aviso de que fixar a semente serializa o UMAP. Sabido e
            # desejado — sem semente fixa a rodada não é reproduzível, e
            # reprodutibilidade aqui vale mais que os segundos de paralelismo.
            warnings.filterwarnings("ignore", message=".*n_jobs value.*")
            return umap.UMAP(
                n_neighbors=vizinhos,
                min_dist=par.min_dist,
                n_components=saida,
                metric=par.metric,
                random_state=par.semente,
            ).fit_transform(matriz)

    reduzido = projetar(dimensoes)
    # Projeção 2D separada, mesma semente, só para o gráfico. Clusterizar em 2D
    # para poder desenhar seria jogar fora estrutura de graça.
    plano = reduzido if dimensoes == 2 else projetar(2)

    hdb = HDBSCAN(
        min_cluster_size=par.min_cluster_size,
        min_samples=par.min_samples,
        cluster_selection_method=par.cluster_selection_method,
        # Explícito porque o default do scikit-learn vai mudar na 1.10, e um
        # aviso de depreciação no meio do relatório confunde mais do que ajuda.
        copy=True,
    ).fit(np.asarray(reduzido, dtype="float64"))

    return Resultado(
        refs=list(refs),
        rotulos=[int(r) for r in hdb.labels_],
        probabilidades=[float(p) for p in hdb.probabilities_],
        outliers=[float(o) for o in getattr(hdb, "outlier_scores_",
                                            [0.0] * len(refs))],
        xy=[(float(a), float(b)) for a, b in plano],
    )


# --------------------------------------------------------------------------
# A conferência
# --------------------------------------------------------------------------

def pares_separados(refs: Sequence[int], matriz, rotulos: Sequence[int],
                    limiar: float = 0.98) -> List[Tuple[int, int, float]]:
    """Pares quase idênticos que caíram em clusters diferentes.

    É a conferência automática do agrupamento, e ela não é circular: o
    HDBSCAN decide no espaço projetado pelo UMAP, não no original. Dois
    vetores com cosseno acima do limiar que terminam separados significam que
    a projeção rasgou o que estava junto — parâmetro errado, não estrutura.

    Os vetores já vêm normalizados em L2 de vetores.gerar(), então o produto
    interno é o cosseno.
    """
    import numpy as np

    sim = np.asarray(matriz) @ np.asarray(matriz).T
    achados = []
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            if sim[i, j] >= limiar and rotulos[i] != rotulos[j]:
                achados.append((refs[i], refs[j], float(sim[i, j])))
    return sorted(achados, key=lambda t: -t[2])


# --------------------------------------------------------------------------
# Persistência
# --------------------------------------------------------------------------

def gravar(conn, banco: str, modelo: str, normalizacao: str,
           res: Resultado, par: Parametros) -> int:
    """Grava a rodada e os pontos. Devolve o id da rodada."""
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(SQL_RODADA, (banco, modelo, normalizacao, len(res.refs),
                                 res.clusters, res.ruido,
                                 Jsonb(par.como_dict())))
        rodada_id = cur.fetchone()[0]
        for ref, rot, prob, out, (x, y) in zip(
                res.refs, res.rotulos, res.probabilidades,
                res.outliers, res.xy):
            cur.execute(SQL_PONTO, (rodada_id, ref, rot, prob, out, x, y))
    conn.commit()
    return rodada_id


def carregar_rodada(conn, rodada_id: int) -> Tuple[dict, Dict[int, List[int]]]:
    """Devolve (metadados da rodada, {cluster_id: [query_ref]})."""
    from .banco import linhas_como_dicts

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM monitoring.rodada_cluster WHERE id = %s",
                    (rodada_id,))
        linhas = linhas_como_dicts(cur)
        if not linhas:
            raise SystemExit(f"rodada {rodada_id} não existe. "
                             f"Liste com: agrupar --listar")
        meta = linhas[0]

        cur.execute(
            "SELECT cluster_id, query_ref FROM monitoring.query_cluster "
            " WHERE rodada_id = %s ORDER BY cluster_id, query_ref",
            (rodada_id,))
        grupos: Dict[int, List[int]] = {}
        for cid, ref in cur.fetchall():
            grupos.setdefault(cid, []).append(ref)
    return meta, grupos


# --------------------------------------------------------------------------
# Relatório
# --------------------------------------------------------------------------

def mostrar(res: Resultado, queries: Sequence[Query]) -> None:
    """Uma linha por cluster, no estilo de saída do resto do projeto."""
    por_ref = {q.ref: q for q in queries}
    for cid, refs in res.por_cluster().items():
        qs = [por_ref[r] for r in refs if r in por_ref]
        ms = sum(q.duracao_total_ms for q in qs)
        relacoes = sorted({r for q in qs for r in q.relacoes})
        nome = "ruído" if cid < 0 else f"cluster {cid}"
        print(f"  {nome:>10}  {len(refs):>2} queries  {ms:>12,.0f} ms  "
              f"{', '.join(relacoes)[:52]}")
        for q in sorted(qs, key=lambda q: -q.duracao_total_ms):
            print(f"              ref={q.ref:<4} {q.duracao_total_ms:>11,.0f} ms  "
                  f"{q.texto.strip()[:46]!r}")
