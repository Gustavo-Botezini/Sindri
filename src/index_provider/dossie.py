#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""O dossiê: o que a carga revelou e o que existe hoje no banco.

Quatro partes, montadas de dois bancos:

    workload  brokk:5433  as queries mais caras, com os filtros de cada Seq
                          Scan, quanto cada um descartou, junções, ordenações
                          e os índices já usados
    indices   tpch:5432   definição, tamanho, idx_scan, se sustenta constraint
    tabelas   tpch:5432   linhas, tamanho e atividade de escrita
    colunas   tpch:5432   pg_stats: avg_width, n_distinct, correlação

O que faz este dossiê diferente de mandar o SQL: ele leva **números medidos**.
Um modelo que só vê ``CREATE INDEX ON lineitem (l_shipdate)`` chuta o tamanho;
um que recebe "lineitem tem 6.001.215 linhas, idx_lineitem_orderkey ocupa
128 MB para uma chave de 8 bytes, e l_shipdate tem avg_width 4" calcula. Por
isso ``sustenta_constraint`` existe: é o que evita a recomendação mais
perigosa que um modelo pode dar, remover um índice que existe por integridade
e não por desempenho.

O mesmo montador serve aos dois modos. Sem ``refs``, pega as ``top`` queries
mais caras do banco — é o modo clássico. Com ``refs``, recorta exatamente as
queries de um cluster.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .banco import DSN_BROKK, DSN_OBSERVADO, conectar, linhas_como_dicts

# Quantas queries do topo do ranking vão no dossiê global. Elas entram
# inteiras, sem truncar: uma query cortada no meio faz o modelo recomendar
# índice para uma cláusula que ele não viu.
TOP_QUERIES = 25

# Expressões (filtros, junções, ordenações) por query. A cauda não muda
# decisão de índice e só engorda o dossiê.
EXPRESSOES_POR_QUERY = 12


# --------------------------------------------------------------------------
# O que a carga revelou (banco de monitoramento)
# --------------------------------------------------------------------------

_COLUNAS_WORKLOAD = """
       query_ref, query_text, execucoes,
       round(duracao_media_ms::numeric, 2)  AS duracao_media_ms,
       round(duracao_total_ms::numeric)     AS duracao_total_ms,
       round(duracao_max_ms::numeric, 1)    AS duracao_max_ms,
       relacoes, indices, seq_scans,
       round(seq_scan_descartadas::numeric) AS seq_scan_descartadas,
       sorts_em_disco,
       round(pior_fator_no::numeric, 1)     AS pior_fator_no,
       shared_read_total,
       round(io_read_ms_total::numeric)     AS io_read_ms_total
"""

SQL_WORKLOAD = f"""
SELECT {_COLUNAS_WORKLOAD}
  FROM monitoring.query_resumo
 WHERE database_name = %s
 ORDER BY duracao_total_ms DESC
 LIMIT %s
"""

SQL_WORKLOAD_REFS = f"""
SELECT {_COLUNAS_WORKLOAD}
  FROM monitoring.query_resumo
 WHERE query_ref = ANY (%s)
 ORDER BY duracao_total_ms DESC
"""

# As expressões são o que aponta a coluna a indexar. Um Seq Scan com filtro
# estável e muita linha descartada é candidato; sem o texto do filtro, a
# recomendação seria "indexe alguma coisa nesta tabela".
SQL_EXPRESSOES = """
WITH agrupado AS (
    SELECT x.query_ref,
           n.node_type,
           n.relation_name,
           n.filtro,
           n.index_cond,
           n.hash_cond,
           n.sort_key,
           n.sort_method,
           count(*)                                       AS ocorrencias,
           round(avg(n.linhas_removidas_filtro)::numeric) AS descartadas_media,
           round(avg(n.linhas_reais * n.loops)::numeric)  AS linhas_media,
           round(sum(n.tempo_total_ms)::numeric)          AS ms_total
      FROM monitoring.plan_execution x
      JOIN monitoring.plan_node n
        ON n.event_id = x.event_id AND n.event_time = x.event_time
     WHERE x.query_ref = ANY (%s)
       AND (n.filtro IS NOT NULL OR n.hash_cond IS NOT NULL
            OR n.sort_key IS NOT NULL OR n.index_cond IS NOT NULL)
     GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
    HAVING count(*) > 1
),
ranqueado AS (
    -- Uma query grande gera dezenas de expressões, e a cauda é ruído: nós que
    -- custam milissegundos não mudam decisão de índice. Sem este corte as
    -- expressões respondiam por mais de quatro quintos do dossiê.
    -- (Cuidado ao editar: um sinal de porcentagem aqui dentro vira placeholder
    --  para o psycopg e quebra a execução.)
    SELECT *, row_number() OVER (PARTITION BY query_ref
                                 ORDER BY ms_total DESC NULLS LAST) AS pos
      FROM agrupado
)
SELECT query_ref, node_type, relation_name, filtro, index_cond, hash_cond,
       sort_key, sort_method, ocorrencias, descartadas_media, linhas_media,
       ms_total
  FROM ranqueado
 WHERE pos <= %s
 ORDER BY query_ref, ms_total DESC
"""


# --------------------------------------------------------------------------
# O que existe hoje (banco observado)
# --------------------------------------------------------------------------

# bytes_por_linha é o que ancora a estimativa de tamanho de um índice novo:
# sem ele o modelo só pode chutar.
SQL_INDICES = """
SELECT s.relname                                   AS tabela,
       s.indexrelname                              AS indice,
       pg_get_indexdef(s.indexrelid)               AS ddl,
       pg_relation_size(s.indexrelid)              AS bytes,
       round(pg_relation_size(s.indexrelid) / 1048576.0, 2) AS mb,
       s.idx_scan,
       s.idx_tup_read,
       i.indisunique                               AS unico,
       i.indisprimary                              AS primario,
       EXISTS (SELECT 1 FROM pg_constraint c
                WHERE c.conindid = s.indexrelid)   AS sustenta_constraint,
       c.reltuples::bigint                         AS linhas_tabela,
       CASE WHEN c.reltuples > 0
            THEN round((pg_relation_size(s.indexrelid) / c.reltuples)::numeric, 1)
       END                                         AS bytes_por_linha
  FROM pg_stat_user_indexes s
  JOIN pg_index i ON i.indexrelid = s.indexrelid
  JOIN pg_class c ON c.oid = s.relid
 ORDER BY pg_relation_size(s.indexrelid) DESC
"""

SQL_TABELAS = """
SELECT relname                                        AS tabela,
       n_live_tup                                     AS linhas,
       round(pg_relation_size(relid) / 1048576.0, 1)  AS mb_dados,
       round(pg_indexes_size(relid) / 1048576.0, 1)   AS mb_indices,
       n_tup_ins                                      AS inserts,
       n_tup_upd                                      AS updates,
       n_tup_del                                      AS deletes,
       n_dead_tup                                     AS tuplas_mortas,
       seq_scan, idx_scan
  FROM pg_stat_user_tables
 ORDER BY pg_total_relation_size(relid) DESC
"""

SQL_COLUNAS = """
SELECT tablename AS tabela, attname AS coluna,
       avg_width, n_distinct, round(correlation::numeric, 3) AS correlacao,
       null_frac
  FROM pg_stats
 WHERE schemaname = 'public'
 ORDER BY tablename, attname
"""


# --------------------------------------------------------------------------
# Montagem
# --------------------------------------------------------------------------

def montar(banco: str,
           refs: Optional[Sequence[int]] = None,
           top: int = TOP_QUERIES,
           expressoes_por_query: int = EXPRESSOES_POR_QUERY,
           conn_brokk=None, conn_observado=None) -> dict:
    """Junta as duas fontes num único documento para o modelo.

    Sem `refs`, o dossiê global das `top` queries mais caras. Com `refs`, só
    aquelas queries — é o recorte de um cluster.
    """
    proprias = conn_brokk is None
    conn = conn_brokk or conectar(DSN_BROKK)
    try:
        with conn.cursor() as cur:
            if refs is None:
                cur.execute(SQL_WORKLOAD, (banco, top))
            else:
                cur.execute(SQL_WORKLOAD_REFS, (list(refs),))
            workload = linhas_como_dicts(cur)

            alvos = [q["query_ref"] for q in workload]
            expressoes: List[dict] = []
            if alvos:
                cur.execute(SQL_EXPRESSOES, (alvos, expressoes_por_query))
                expressoes = linhas_como_dicts(cur)
    finally:
        if proprias:
            conn.close()

    # As expressões vão dentro de cada query, não numa lista solta: o modelo
    # precisa saber a qual query cada Seq Scan pertence.
    por_ref: Dict[int, List[dict]] = {}
    for e in expressoes:
        por_ref.setdefault(e.pop("query_ref"), []).append(e)
    for q in workload:
        q["expressoes"] = por_ref.get(q["query_ref"], [])

    proprias_obs = conn_observado is None
    obs = conn_observado or conectar(DSN_OBSERVADO, "banco observado")
    try:
        with obs.cursor() as cur:
            cur.execute(SQL_INDICES)
            indices = linhas_como_dicts(cur)
            cur.execute(SQL_TABELAS)
            tabelas = linhas_como_dicts(cur)
            cur.execute(SQL_COLUNAS)
            colunas = linhas_como_dicts(cur)
    finally:
        if proprias_obs:
            obs.close()

    return {
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "banco": banco,
        "workload": workload,
        "indices": indices,
        "tabelas": tabelas,
        "colunas": colunas,
    }


def resumir(dossie: dict) -> str:
    """A linha de contagens que o CLI imprime."""
    from .banco import como_json
    return (f"{len(dossie['workload'])} queries, "
            f"{len(dossie['indices'])} índices, "
            f"{len(dossie['tabelas'])} tabelas, "
            f"{len(dossie['colunas'])} colunas — "
            f"{len(como_json(dossie)):,} caracteres")
