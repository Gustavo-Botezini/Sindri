#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""As figuras dos clusters.

Duas saídas, as duas em PNG para caber num artigo:

    grafico     um painel por cluster (small multiples)
    projecoes   UMAP x PCA x t-SNE lado a lado

**Por que painéis e não um gráfico só com as cores todas.** Num diagrama de
dispersão quaisquer dois pontos podem encostar, então todos os pares de cores
precisam ser distinguíveis — não só os vizinhos numa legenda. Rodando o
validador de paleta em modo `--pairs all`, nenhum conjunto de cinco cores da
paleta de referência passa: o pior par cai em ΔE 12,9 de visão normal, abaixo
do piso de 15. Com quatro ou mais séries nessa forma, a saída é facetar.

Cada painel então usa **uma cor só** (azul para o cluster em foco, cinza para
o resto), o que passa em todas as seis checagens, e a identidade de cada ponto
vem do rótulo, não da cor. De quebra a figura fica mais legível que a do
artigo de referência, cujas Figuras 3 a 5 não rotulam ponto nenhum — dá para
ver que há grupos, não quais queries estão em cada um.

Os eixos não têm rótulo nem escala de propósito: coordenada de UMAP não tem
unidade nem significado absoluto, só vizinhança. Marcar os números sugeriria
uma precisão que a projeção não tem.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .banco import RAIZ, linhas_como_dicts

# Tokens da paleta de referência, modo claro. O azul foi validado:
# ALL CHECKS PASS em --pairs all, contraste >= 3:1 sobre a superfície.
SUPERFICIE = "#fcfcfb"
TINTA = "#0b0b0b"
TINTA_2 = "#52514e"
MUDO = "#898781"
EIXO = "#c3c2b7"
SERIE = "#2a78d6"
# Os pontos fora do cluster em foco. Não é uma segunda série: é contexto, e
# por isso fica em cinza de eixo em vez de ganhar cor própria.
FUNDO = "#d8d7d0"


SQL_PONTOS = """
SELECT c.cluster_id, c.query_ref, c.x, c.y, r.query_text
  FROM monitoring.query_cluster c
  JOIN monitoring.query_resumo r ON r.query_ref = c.query_ref
 WHERE c.rodada_id = %s
 ORDER BY c.cluster_id, c.query_ref
"""


# --------------------------------------------------------------------------
# Rótulos
# --------------------------------------------------------------------------

def _tokens(texto: str) -> set:
    return set(re.findall(r"[a-z_][a-z0-9_]{2,}", texto.lower()))


def rotulos_tpch(pontos: Sequence[dict],
                 pasta: Path = None) -> Dict[int, str]:
    """Casa cada query do corpus com a do TPC-H, quando dá.

    Por similaridade de Jaccard sobre os identificadores, e não por igualdade
    de texto: a mesma query aparece no corpus com os literais trocados por
    ``$n`` e, no caso da q01, com ``interval '119' day`` virando
    ``concat($1,' day')::interval``. Igualdade exata não casaria; o conjunto
    de identificadores, sim.

    Quem não casar fica com ``#<query_ref>``, que é o que o banco conhece.
    """
    pasta = pasta or (RAIZ / "queries")
    rotulo: Dict[int, str] = {}
    arquivos = sorted(pasta.glob("q*.sql")) if pasta.is_dir() else []
    referencia = {a.stem: _tokens(a.read_text(encoding="utf-8"))
                  for a in arquivos}

    usados = set()
    for p in pontos:
        alvo = _tokens(p["query_text"])
        melhor, score = None, 0.0
        for nome, toks in referencia.items():
            if nome in usados or not (alvo | toks):
                continue
            j = len(alvo & toks) / len(alvo | toks)
            if j > score:
                melhor, score = nome, j
        if melhor and score >= 0.6:
            usados.add(melhor)
            rotulo[p["query_ref"]] = melhor
        else:
            rotulo[p["query_ref"]] = f"#{p['query_ref']}"
    return rotulo


def carregar_pontos(conn, rodada_id: int) -> List[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL_PONTOS, (rodada_id,))
        return linhas_como_dicts(cur)


# --------------------------------------------------------------------------
# Desenho
# --------------------------------------------------------------------------

def _limpar(ax) -> None:
    """Eixo recessivo: sem ticks, sem números, moldura discreta."""
    ax.set_xticks([])
    ax.set_yticks([])
    for lado in ax.spines.values():
        lado.set_color(EIXO)
        lado.set_linewidth(0.8)
    ax.set_facecolor(SUPERFICIE)


def _casco(ax, xs, ys, cor) -> None:
    """Sombra do casco convexo, como as Figuras 3 a 5 do artigo."""
    if len(xs) < 3:
        return
    try:
        import numpy as np
        from scipy.spatial import ConvexHull
    except ImportError:                          # pragma: no cover
        return
    pontos = np.column_stack([xs, ys])
    try:
        casco = ConvexHull(pontos)
    except Exception:                            # noqa: BLE001
        return                                   # colineares: sem área
    ax.fill(pontos[casco.vertices, 0], pontos[casco.vertices, 1],
            color=cor, alpha=0.10, zorder=1, linewidth=0)


def _pontos(ax, itens, cor, rotulo, tamanho=70, com_texto=True) -> List:
    xs = [i["x"] for i in itens]
    ys = [i["y"] for i in itens]
    # Anel branco: separa marcas que se sobrepõem sem inventar outra cor.
    ax.scatter(xs, ys, s=tamanho, c=cor, edgecolors=SUPERFICIE,
               linewidths=1.6, zorder=3)
    if not com_texto:
        return []
    return [ax.annotate(rotulo[i["query_ref"]], (i["x"], i["y"]),
                        xytext=(0, 9), textcoords="offset points",
                        ha="center", fontsize=7, color=TINTA_2, zorder=4)
            for i in itens]


def _separar_rotulos(fig, textos: Sequence, passos: int = 120) -> None:
    """Afasta rótulos que se sobrepõem, empurrando na vertical.

    Sem isto os pontos próximos viram uma mancha ilegível — no corpus atual,
    as três sondagens de FK ficam a poucos pixels uma da outra e seus rótulos
    se escrevem por cima. Repulsão simples: mede as caixas de texto no
    renderer e afasta cada par que encosta, algumas vezes, até parar.
    """
    if len(textos) < 2:
        return
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    for _ in range(passos):
        caixas = [t.get_window_extent(renderer) for t in textos]
        encostou = False
        for i in range(len(textos)):
            for j in range(i + 1, len(textos)):
                if not caixas[i].overlaps(caixas[j]):
                    continue
                encostou = True
                # Quem já está mais alto sobe; o outro desce. Assim o rótulo
                # não se afasta do seu ponto mais do que o necessário.
                acima, abaixo = (i, j) if caixas[i].y0 >= caixas[j].y0 else (j, i)
                for idx, passo in ((acima, 2.2), (abaixo, -2.2)):
                    dx, dy = textos[idx].get_position()
                    textos[idx].set_position((dx, dy + passo))
        if not encostou:
            return
        fig.canvas.draw()


def desenhar_clusters(pontos: Sequence[dict], rotulo: Dict[int, str],
                      caminho: Path, titulo: str) -> Path:
    """Um painel por cluster. Devolve o caminho gravado."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    grupos: Dict[int, List[dict]] = {}
    for p in pontos:
        grupos.setdefault(p["cluster_id"], []).append(p)
    ids = sorted(grupos)

    # Um painel de visão geral mais um por cluster. O geral responde "onde
    # este grupo fica"; os outros, "quais queries estão nele" — e para a
    # segunda pergunta é preciso dar zoom, porque na escala do corpus inteiro
    # um cluster é um borrão de poucos pixels onde nenhum rótulo cabe.
    total = len(ids) + 1
    colunas = min(3, total)
    linhas = (total + colunas - 1) // colunas
    fig, axes = plt.subplots(linhas, colunas,
                             figsize=(4.4 * colunas, 4.0 * linhas),
                             squeeze=False)
    fig.patch.set_facecolor(SUPERFICIE)

    def _janela(itens, folga=0.28, minimo=0.18):
        """Limites do painel: a extensão do cluster com folga para o rótulo."""
        span_geral = max(
            max(p["x"] for p in pontos) - min(p["x"] for p in pontos),
            max(p["y"] for p in pontos) - min(p["y"] for p in pontos))
        cx = (max(i["x"] for i in itens) + min(i["x"] for i in itens)) / 2
        cy = (max(i["y"] for i in itens) + min(i["y"] for i in itens)) / 2
        span = max(max(i["x"] for i in itens) - min(i["x"] for i in itens),
                   max(i["y"] for i in itens) - min(i["y"] for i in itens))
        # Cluster de dois pontos coincidentes teria span zero; o piso evita
        # um zoom absurdo que não mostraria contexto nenhum.
        meio = max(span * (1 + folga), span_geral * minimo) / 2
        return (cx - meio, cx + meio), (cy - meio, cy + meio)

    # -- visão geral -----------------------------------------------------
    ax = axes[0][0]
    for cid in ids:
        foco = grupos[cid]
        _casco(ax, [p["x"] for p in foco], [p["y"] for p in foco], SERIE)
    _pontos(ax, pontos, SERIE, rotulo, tamanho=46, com_texto=False)
    for cid in ids:
        foco = grupos[cid]
        # Acima do ponto mais alto do cluster, não no centroide: o crachá é
        # opaco, e num cluster compacto ele cobriria exatamente os pontos que
        # deveria identificar.
        ax.annotate(
            "ruído" if cid < 0 else str(cid),
            (sum(p["x"] for p in foco) / len(foco),
             max(p["y"] for p in foco)),
            xytext=(0, 15), textcoords="offset points",
            ha="center", va="center", fontsize=10, fontweight="bold",
            color=TINTA, zorder=5,
            bbox=dict(boxstyle="circle,pad=0.26", facecolor=SUPERFICIE,
                      edgecolor=EIXO, linewidth=0.8))
    ax.set_title(f"o corpus inteiro  ·  {len(pontos)} queries",
                 fontsize=10, color=TINTA, pad=8, loc="left")
    # Folga no topo para os crachás, que ficam acima do cluster: sem ela o do
    # grupo mais alto é desenhado fora da moldura.
    span_y = max(p["y"] for p in pontos) - min(p["y"] for p in pontos)
    span_x = max(p["x"] for p in pontos) - min(p["x"] for p in pontos)
    ax.set_xlim(min(p["x"] for p in pontos) - span_x * 0.10,
                max(p["x"] for p in pontos) + span_x * 0.10)
    ax.set_ylim(min(p["y"] for p in pontos) - span_y * 0.06,
                max(p["y"] for p in pontos) + span_y * 0.14)
    # "box" e não "datalim": os limites acima é que mandam, e datalim faria o
    # matplotlib reescrevê-los para acomodar a proporção.
    ax.set_aspect("equal", adjustable="box")
    _limpar(ax)

    # -- um por cluster --------------------------------------------------
    todos_textos = []
    cinzas = 0
    for pos, cid in enumerate(ids, start=1):
        ax = axes[pos // colunas][pos % colunas]
        foco = grupos[cid]
        lim_x, lim_y = _janela(foco)
        dentro = [p for p in pontos
                  if p["cluster_id"] != cid
                  and lim_x[0] <= p["x"] <= lim_x[1]
                  and lim_y[0] <= p["y"] <= lim_y[1]]

        cinzas += len(dentro)
        _pontos(ax, dentro, FUNDO, rotulo, tamanho=46, com_texto=False)
        _casco(ax, [p["x"] for p in foco], [p["y"] for p in foco], SERIE)
        todos_textos.append(_pontos(ax, foco, SERIE, rotulo, tamanho=90))

        nome = "ruído" if cid < 0 else f"cluster {cid}"
        ax.set_title(f"{nome}  ·  {len(foco)} queries",
                     fontsize=10, color=TINTA, pad=8, loc="left")
        ax.set_xlim(*lim_x)
        ax.set_ylim(*lim_y)
        # Mesma escala nos dois eixos: esticar um deles distorceria as
        # distâncias, que é a única coisa que a projeção tem a dizer.
        ax.set_aspect("equal", adjustable="box")
        _limpar(ax)

    for vazio in range(total, linhas * colunas):
        axes[vazio // colunas][vazio % colunas].axis("off")

    # Legenda só quando há mesmo duas séries na figura. Com o zoom apertado,
    # muitas vezes nenhum ponto de outro cluster cai dentro da janela — e
    # anunciar na legenda uma cor que não aparece em painel nenhum é pior que
    # não ter legenda. Com uma série só, o título de cada painel já nomeia.
    if cinzas:
        fig.legend(handles=[
            Line2D([], [], marker="o", linestyle="", markersize=8,
                   markerfacecolor=SERIE, markeredgecolor=SUPERFICIE,
                   label="queries deste cluster"),
            Line2D([], [], marker="o", linestyle="", markersize=6,
                   markerfacecolor=FUNDO, markeredgecolor=SUPERFICIE,
                   label="queries de outro cluster na mesma janela"),
        ], loc="lower center", ncol=2, frameon=False, fontsize=9,
            labelcolor=TINTA_2, bbox_to_anchor=(0.5, -0.005))

    fig.suptitle(titulo, fontsize=12, color=TINTA, x=0.011, ha="left")
    fig.text(0.011, 0.945,
             "projeção UMAP em 2D · eixos sem unidade, só vizinhança · "
             "cada painel de cluster dá zoom; escala igual nos dois eixos",
             fontsize=8.5, color=MUDO, ha="left")
    fig.tight_layout(rect=(0, 0.045 if cinzas else 0.015, 1, 0.925))
    for textos in todos_textos:
        _separar_rotulos(fig, textos)
    fig.savefig(caminho, dpi=200, facecolor=SUPERFICIE)
    plt.close(fig)
    return caminho


def desenhar_projecoes(conn, rodada_id: int, pontos: Sequence[dict],
                       rotulo: Dict[int, str], caminho: Path,
                       modelo: str, normalizacao: str) -> Path:
    """UMAP x PCA x t-SNE, como as Figuras 3 a 5 do artigo.

    Uma cor só nos três painéis: aqui a pergunta é se a projeção separa os
    grupos, e quem responde é a posição com o rótulo ao lado — não a cor.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.manifold import TSNE

    from . import vetores

    refs = [p["query_ref"] for p in pontos]
    ordem, matriz = vetores.carregar_vetores(conn, refs, modelo, normalizacao)
    por_ref = {p["query_ref"]: p for p in pontos}

    umap_xy = np.array([[por_ref[r]["x"], por_ref[r]["y"]] for r in ordem])
    pca_xy = PCA(n_components=2, random_state=42).fit_transform(matriz)
    # perplexity tem que ser menor que o número de amostras; com corpus
    # pequeno o padrão 30 estoura.
    perp = max(2, min(30, len(ordem) - 1) // 2)
    tsne_xy = TSNE(n_components=2, random_state=42, perplexity=perp,
                   init="pca").fit_transform(matriz)

    # Alto de propósito: sob escala igual, a projeção UMAP fica num retângulo
    # estreito, e é nele que os 24 rótulos precisam caber sem se pisarem.
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 7.0))
    fig.patch.set_facecolor(SUPERFICIE)
    textos: List = []
    for ax, (nome, xy) in zip(axes, (("UMAP", umap_xy),
                                     ("PCA", pca_xy),
                                     ("t-SNE", tsne_xy))):
        itens = [{"x": float(x), "y": float(y), "query_ref": r}
                 for (x, y), r in zip(xy, ordem)]
        textos.append(_pontos(ax, itens, SERIE, rotulo, tamanho=62))
        ax.set_title(nome, fontsize=11, color=TINTA, pad=8, loc="left")
        ax.set_aspect("equal", adjustable="box")
        _limpar(ax)

    fig.suptitle("A mesma carga sob três projeções",
                 fontsize=12, color=TINTA, x=0.008, ha="left")
    fig.text(0.008, 0.938,
             "os mesmos vetores de 1024 dimensões reduzidos a 2 · "
             "o agrupamento roda sobre a projeção UMAP",
             fontsize=8.5, color=MUDO, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.925))
    for grupo in textos:
        _separar_rotulos(fig, grupo)
    fig.savefig(caminho, dpi=200, facecolor=SUPERFICIE)
    plt.close(fig)
    return caminho
