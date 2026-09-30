#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recomendador de índices do BrokkPG.

Etapa 3 do pipeline. O coletor grava o log bruto, o worker transforma os
planos em tabelas, e este pacote pega o que o data warehouse já sabe — quais
queries custam caro, o que elas varrem, quanto descartam, quais índices usam —
e propõe mudanças no conjunto de índices.

São dois modos, e a diferença entre eles é o que sai no fim.

**Modo clássico** (``dossie``, ``recomendar``). Monta o dossiê da carga
inteira, manda para Claude e grava as recomendações em CSV ou Google Sheets.
Era o ``src/index_provider.py``, e continua idêntico no comportamento. O que
o torna diferente de um recomendador que só olha SQL é que ele leva **números
medidos**: um modelo que só vê ``CREATE INDEX ON lineitem (l_shipdate)`` chuta
o tamanho; um que recebe "lineitem tem 6.001.215 linhas, idx_lineitem_orderkey
ocupa 128 MB para uma chave de 8 bytes, e l_shipdate tem avg_width 4" calcula.

**Modo de clusters** (``vetorizar`` → ``agrupar`` → ``candidatar`` → ``medir``
→ ``relatar``). Fecha o ciclo que o modo clássico deixa aberto: nenhuma
recomendação dele é aplicada, nenhum ganho é medido, e a saída é uma opinião
com números de entrada bons e nenhum número de saída.

    monitoring.query_resumo
            │  texto das queries
            ▼
    1. vetorizar   Qwen3-Embedding-0.6B          -> query_embedding
            ▼
    2. agrupar     UMAP + HDBSCAN                -> rodada_cluster, query_cluster
            ▼
    3. candidatar  heurística propõe, Claude escolhe -> experimento
            ▼
    4. medir       CREATE INDEX, replay, DROP    -> experimento_medida
            ▼
    5. relatar     experimento_veredito          -> CSV / Sheets

**A hipótese em teste, e ela pode ser falsa.** A premissa do modo de clusters
é que um índice derivado de uma query serve às outras queries do mesmo cluster
semântico. O desenho tem que poder mostrar que não serve — por isso a bancada
mede o efeito em *todas* as queries do cluster, inclusive as que pioram, e por
isso ``monitoring.experimento_veredito`` separa ``ganho_fora_da_origem_ms`` do
ganho total. A query de onde o candidato saiu vai melhorar quase por
construção; só o que sobra depois de tirá-la responde à pergunta.

**O embedding é o particionador, não o critério.** Quem decide o índice
continua sendo o número medido em ``plan_node`` — quantas linhas aquele Seq
Scan descartou, quanto tempo custou. O vetor do texto só serve para dizer
quais queries têm forma parecida e portanto merecem ser testadas juntas.

Uso:

    ./.venv/bin/python -m src.index_provider --help
    ./.venv/bin/python -m src.index_provider dossie --banco tpch
    ./.venv/bin/python -m src.index_provider vetorizar --banco tpch
    ./.venv/bin/python -m src.index_provider agrupar --banco tpch
    ./.venv/bin/python -m src.index_provider candidatar --rodada 1
    ./.venv/bin/python -m src.index_provider medir --rodada 1 --cluster 0

Rode da raiz do projeto e de dentro da WSL. As dependências pesadas (torch,
umap, anthropic, gspread) são importadas dentro da função que as usa, não no
topo do módulo: assim ``dossie`` e ``relatar`` rodam sem nada disso instalado.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv=None) -> int:
    """Atalho para o CLI, para quem importar o pacote em vez de rodá-lo."""
    from .__main__ import main as _main
    return _main(argv)
