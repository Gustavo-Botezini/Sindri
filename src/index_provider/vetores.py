#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Embeddings do texto das queries, com o Qwen3-Embedding-0.6B.

O modelo roda local, na CPU. Não há endpoint de embedding na API da
Anthropic — Claude não devolve vetor — então este é o único jeito de manter o
Qwen3 do experimento de referência sem trocar de provedor.

Custo medido nesta bancada: 27 queries de ~600 caracteres viram 27 vetores de
1024 dimensões em 170s numa CPU sem GPU — uns 6s por query, e a maior parte
disso é o modelo de 0,6 B rodando sem aceleração. A primeira execução ainda
baixa ~1,2 GB de pesos do HuggingFace; depois eles ficam em
~/.cache/huggingface e o cache em monitoring.query_embedding evita reembedar.

**Sem prefixo de instrução.** O Qwen3-Embedding usa prompt só do lado da
*query* em tarefa de recuperação, para distingui-la do documento. Aqui não há
essa assimetria: os SQLs do corpus são todos documentos, comparados entre
si. É
também o que corresponde ao "texto passado integralmente" do trabalho de
referência.

O cache é por ``texto_hash``, não por ``query_ref``: mudar a normalização muda
o texto embedado, e reaproveitar o vetor antigo faria a comparação entre duas
normalizações comparar uma coisa com ela mesma.
"""

from __future__ import annotations

import sys
import time
from typing import Dict, Sequence, Tuple

from .corpus import Query

MODELO_EMBEDDING = "Qwen/Qwen3-Embedding-0.6B"
LOTE = 8

# Não é modelo: é o TF-IDF sobre a assinatura do modo de normalização
# "estrutura" (método B). Entra pelo mesmo --modelo-embedding para que
# agrupar, exportar e grafico funcionem sem saber a diferença.
MODELO_TFIDF = "tfidf"


SQL_CACHE = """
SELECT query_ref, texto_hash, dimensoes
  FROM monitoring.query_embedding
 WHERE modelo = %s AND normalizacao = %s AND query_ref = ANY (%s)
"""

SQL_GRAVAR = """
INSERT INTO monitoring.query_embedding
       (query_ref, modelo, normalizacao, dimensoes, texto_hash, vetor)
VALUES (%s, %s, %s, %s, %s, %s::real[])
ON CONFLICT (query_ref, modelo, normalizacao) DO UPDATE
   SET dimensoes  = EXCLUDED.dimensoes,
       texto_hash = EXCLUDED.texto_hash,
       vetor      = EXCLUDED.vetor,
       gerado_em  = now()
"""

SQL_LER = """
SELECT query_ref, vetor
  FROM monitoring.query_embedding
 WHERE modelo = %s AND normalizacao = %s AND query_ref = ANY (%s)
 ORDER BY query_ref
"""


# --------------------------------------------------------------------------
# O modelo
# --------------------------------------------------------------------------

def carregar_modelo(nome: str = MODELO_EMBEDDING):
    """Import preguiçoso: quem só quer `dossie` não precisa de torch."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        sys.exit(
            "sentence-transformers não encontrado. No venv do projeto:\n"
            "    ./.venv/bin/pip install --index-url "
            "https://download.pytorch.org/whl/cpu torch\n"
            "    ./.venv/bin/pip install -r requirements.txt\n"
            "O torch tem que vir ANTES, do índice CPU: senão o pip baixa a "
            "build com CUDA (~2,5 GB) numa máquina sem GPU."
        )
    return SentenceTransformer(nome, device="cpu")


# --------------------------------------------------------------------------
# Gerar
# --------------------------------------------------------------------------

def gerar(conn, queries: Sequence[Query],
          modelo: str = MODELO_EMBEDDING,
          normalizacao: str = "espaco+caixa",
          lote: int = LOTE,
          refazer: bool = False,
          verboso: bool = True) -> Tuple[int, int]:
    """Embeda o que falta e grava. Devolve (geradas, reaproveitadas)."""
    if modelo == MODELO_TFIDF:
        return _gerar_tfidf(conn, queries, normalizacao, verboso)
    refs = [q.ref for q in queries]
    cache: Dict[int, str] = {}
    if not refazer:
        with conn.cursor() as cur:
            cur.execute(SQL_CACHE, (modelo, normalizacao, refs))
            cache = {r: h for r, h, _ in cur.fetchall()}

    pendentes = [q for q in queries if cache.get(q.ref) != q.texto_hash]
    reaproveitadas = len(queries) - len(pendentes)

    if not pendentes:
        if verboso:
            print(f"  = nada novo ({reaproveitadas} vetores no cache)")
        return 0, reaproveitadas

    if verboso:
        print(f"  carregando {modelo} (a primeira vez baixa ~1,2 GB)...")
    st = carregar_modelo(modelo)

    inicio = time.perf_counter()
    vetores = st.encode(
        [q.texto_norm for q in pendentes],
        batch_size=lote,
        # Normalizado em L2 aqui, e não depois: com vetor unitário a distância
        # cosseno do UMAP fica equivalente à euclidiana, e o resultado não
        # depende de a query ser longa ou curta.
        normalize_embeddings=True,
        show_progress_bar=verboso,
    )
    gasto = time.perf_counter() - inicio

    with conn.cursor() as cur:
        for q, v in zip(pendentes, vetores):
            cur.execute(SQL_GRAVAR, (q.ref, modelo, normalizacao,
                                     int(len(v)), q.texto_hash,
                                     [float(x) for x in v]))
    conn.commit()

    if verboso:
        dim = int(vetores.shape[1])
        print(f"  + {len(pendentes)} vetores de {dim} dimensões, {gasto:.1f}s")
        if reaproveitadas:
            print(f"  = {reaproveitadas} reaproveitados do cache")
    return len(pendentes), reaproveitadas


def _gerar_tfidf(conn, queries: Sequence[Query], normalizacao: str,
                 verboso: bool) -> Tuple[int, int]:
    """TF-IDF binário sobre os tokens de texto_norm (um atributo por token).

    Sem cache: o IDF depende do corpus inteiro, então uma query nova muda o
    vetor de todas. Refazer custa milissegundos.

    Binário porque a assinatura é um conjunto, sem repetição. O IDF é o que
    impede junção onipresente de puxar tudo para o mesmo lugar: FROM:lineitem
    e JOIN:lineitem.l_orderkey estão em boa parte do TPC-H e pesam pouco;
    JOIN:lineitem.l_partkey está em seis queries e pesa mais.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer

    if normalizacao != "estrutura":
        print(f"  AVISO: tfidf com normalização {normalizacao!r}; o esperado "
              f"é 'estrutura'. Cada palavra do texto vira um atributo.",
              file=sys.stderr)
    vz = TfidfVectorizer(tokenizer=str.split, token_pattern=None,
                         lowercase=False, binary=True, norm="l2")
    matriz = vz.fit_transform([q.texto_norm for q in queries]).toarray()

    with conn.cursor() as cur:
        for q, v in zip(queries, matriz):
            cur.execute(SQL_GRAVAR, (q.ref, MODELO_TFIDF, normalizacao,
                                     int(len(v)), q.texto_hash,
                                     [float(x) for x in v]))
    conn.commit()
    if verboso:
        print(f"  + {len(queries)} vetores TF-IDF de {matriz.shape[1]} "
              f"atributos (sempre todos: o IDF depende do corpus inteiro)")
    return len(queries), 0


# --------------------------------------------------------------------------
# Ler
# --------------------------------------------------------------------------

def carregar_vetores(conn, refs: Sequence[int],
                     modelo: str = MODELO_EMBEDDING,
                     normalizacao: str = "espaco+caixa"):
    """Devolve (refs na ordem, matriz numpy float32)."""
    import numpy as np

    with conn.cursor() as cur:
        cur.execute(SQL_LER, (modelo, normalizacao, list(refs)))
        linhas = cur.fetchall()

    if not linhas:
        sys.exit(
            f"nenhum vetor para modelo={modelo} normalizacao={normalizacao}.\n"
            "Rode antes:  python -m src.index_provider vetorizar"
        )

    ordem = [r for r, _ in linhas]
    matriz = np.asarray([v for _, v in linhas], dtype="float32")
    return ordem, matriz
