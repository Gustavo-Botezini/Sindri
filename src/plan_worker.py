#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Worker de planos do BrokkPG.

Etapa 2 do pipeline. O coletor (``query_colector.py``) grava o evento bruto e
para; este aqui lê os registros com ``record_type = 'plan'``, parseia o JSON do
``auto_explain`` que está no ``message`` e o transforma em três tabelas:

    monitoring.query           dimensão, uma linha por texto de query distinto
    monitoring.plan_execution  fato, uma linha por registro de plano
    monitoring.plan_node       uma linha por nó da árvore, com o pai

O esquema está em ``initdb-monitoring/04-worker-schema.sql``.

Duas decisões que a medição do corpus impôs:

**A dimensão é chaveada pelo texto, não pelo query_id.** O ``query_id`` do
registro de plano é o certo em 96% dos casos, mas não nos 3,8% do regime de
portal adiado — quando o cliente mantém o portal de um ``SELECT`` aberto até o
commit, o ``auto_explain`` só dispara no ``ExecutorEnd``, que acontece durante
o processamento do ``COMMIT``, e o registro sai com o ``query_id`` do COMMIT.
O ``Query Text``, esse o plano sempre carrega.

**O que não dá para resolver fica marcado, não chutado.** Medido no corpus:
dos 7.381 planos adiados, todos caem em vxid que cobre mais de uma query, então
nem o ``vxid`` nem os ``bind`` daquele vxid identificam de qual query o plano é.
Esses saem com ``query_id_origem = 'indefinido'``, e o índice que serve
agregação por ``query_id`` os exclui.

Uso:

    python3 src/plan_worker.py                 # processa o que chegou
    python3 src/plan_worker.py --reprocessar    # volta ao começo do corpus
    python3 src/plan_worker.py --status
    python3 src/plan_worker.py --achados        # o que o corpus já revela
    python3 src/plan_worker.py --seguir
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    sys.exit(
        "psycopg 3 não encontrado. No venv do projeto:\n"
        "    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
    )


# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

DSN_PADRAO = os.environ.get(
    "BROKK_DSN", "postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk"
)

NOME_WORKER = "plan_worker"
LOTE_PADRAO = 5000

RE_ESPACO = re.compile(r"\s+")

# Comandos cujo execute não identifica a query que gerou o plano: o plano saiu
# durante o processamento deles, não durante a query.
COMANDOS_OPACOS = frozenset({"COMMIT", "ROLLBACK", "BEGIN"})

COLUNAS_EXECUCAO = (
    "event_id", "event_time", "query_ref", "query_id_resolvido",
    "query_id_origem", "session_id", "line_num", "database_name", "username",
    "application_name", "command", "duration_ms", "nos", "profundidade",
    "node_type_topo", "custo_estimado", "linhas_estimadas", "linhas_reais",
    "tempo_total_ms", "shared_hit", "shared_read", "temp_read", "temp_written",
    "io_read_ms", "wal_records", "wal_bytes", "jit", "settings", "parametros",
)

COLUNAS_NO = (
    "event_id", "event_time", "node_idx", "parent_idx", "profundidade",
    "node_type", "parent_rel", "relation_name", "alias", "index_name",
    "scan_direction", "join_type", "strategy", "operacao", "custo_inicial",
    "custo_total", "linhas_est", "linhas_reais", "loops", "largura",
    "tempo_inicial_ms", "tempo_total_ms", "shared_hit", "shared_read",
    "temp_read", "temp_written", "io_read_ms", "wal_bytes", "paralelo",
    "workers_lancados", "index_cond", "filtro", "linhas_removidas_filtro",
    "hash_cond", "sort_key", "sort_method", "sort_espaco_kb",
)


# --------------------------------------------------------------------------
# Parse da árvore do plano
# --------------------------------------------------------------------------

def texto(valor: Any) -> Optional[str]:
    """O EXPLAIN escreve Sort Key como lista e Filter como string."""
    if valor is None:
        return None
    if isinstance(valor, list):
        return ", ".join(str(v) for v in valor)
    return str(valor)


def achatar(no: dict, saida: List[dict], pai: Optional[int] = None,
            profundidade: int = 0) -> int:
    """Percorre a árvore em pré-ordem e devolve quantos nós ela tem.

    O índice do nó é a sua posição nessa ordem, então ``parent_idx`` sempre
    aponta para uma linha que já existe — dá para reconstruir a árvore com um
    ``WITH RECURSIVE`` sem depender de nada externo.
    """
    idx = len(saida)
    saida.append({
        "node_idx": idx,
        "parent_idx": pai,
        "profundidade": profundidade,
        "node_type": no.get("Node Type") or "?",
        "parent_rel": no.get("Parent Relationship"),
        "relation_name": no.get("Relation Name"),
        "alias": no.get("Alias"),
        "index_name": no.get("Index Name"),
        "scan_direction": no.get("Scan Direction"),
        "join_type": no.get("Join Type"),
        "strategy": no.get("Strategy"),
        "operacao": no.get("Operation"),
        "custo_inicial": no.get("Startup Cost"),
        "custo_total": no.get("Total Cost"),
        "linhas_est": no.get("Plan Rows"),
        "linhas_reais": no.get("Actual Rows"),
        "loops": no.get("Actual Loops"),
        "largura": no.get("Plan Width"),
        "tempo_inicial_ms": no.get("Actual Startup Time"),
        "tempo_total_ms": no.get("Actual Total Time"),
        "shared_hit": no.get("Shared Hit Blocks"),
        "shared_read": no.get("Shared Read Blocks"),
        "temp_read": no.get("Temp Read Blocks"),
        "temp_written": no.get("Temp Written Blocks"),
        "io_read_ms": no.get("Shared I/O Read Time"),
        "wal_bytes": no.get("WAL Bytes"),
        "paralelo": no.get("Parallel Aware"),
        "workers_lancados": no.get("Workers Launched"),
        "index_cond": texto(no.get("Index Cond")),
        "filtro": texto(no.get("Filter")),
        "linhas_removidas_filtro": no.get("Rows Removed by Filter"),
        "hash_cond": texto(no.get("Hash Cond")),
        "sort_key": texto(no.get("Sort Key")),
        "sort_method": no.get("Sort Method"),
        "sort_espaco_kb": no.get("Sort Space Used"),
    })

    total = 1
    for filho in no.get("Plans") or []:
        total += achatar(filho, saida, idx, profundidade + 1)
    return total


def hash_do_texto(sql: str) -> str:
    """sha1 do SQL com espaços colapsados.

    Não é a normalização do pg_stat_statements — literais continuam onde
    estão. É só uma identidade estável para agrupar execuções do mesmo texto.
    """
    return hashlib.sha1(
        RE_ESPACO.sub(" ", sql).strip().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Resolução do query_id
# --------------------------------------------------------------------------

def resolver_query_id(qid_plano: Optional[int], qid_seguinte: Optional[int],
                      cmd_seguinte: Optional[str]) -> Tuple[Optional[int], str]:
    """Decide o query_id de um plano e de onde ele veio.

    A ordem no log é bind -> plano(s) -> execute, e o execute seguinte da mesma
    sessão carrega o id certo — verificado em 53.905 pares no corpus, zero
    divergência. A exceção é o execute seguinte ser um COMMIT: aí o plano saiu
    durante o commit e o id que ele traz é o do COMMIT.
    """
    if cmd_seguinte in COMANDOS_OPACOS:
        return None, "indefinido"
    if qid_seguinte:
        origem = "proprio" if qid_seguinte == qid_plano else "seguinte"
        return qid_seguinte, origem
    if qid_plano:
        return qid_plano, "proprio"
    return None, "indefinido"


# --------------------------------------------------------------------------
# Leitura do lote
# --------------------------------------------------------------------------
# O LATERAL procura o próximo execute/statement da mesma sessão. Usa o índice
# único (session_id, line_num, event_time), e para em cima do primeiro — não
# varre a sessão inteira.

SQL_LOTE = """
WITH lote AS (
    SELECT e.id, e.event_time, e.session_id, e.line_num, e.query_id,
           e.database_name, e.username, e.application_name, e.command,
           e.duration_ms, e.message
      FROM monitoring.pg_log_event e
     WHERE e.id > %s
       AND e.record_type = 'plan'
     ORDER BY e.id
     LIMIT %s
)
SELECT l.id, l.event_time, l.session_id, l.line_num, l.query_id,
       l.database_name, l.username, l.application_name, l.command,
       l.duration_ms, l.message,
       v.query_id AS qid_seguinte,
       v.command  AS cmd_seguinte
  FROM lote l
  LEFT JOIN LATERAL (
      SELECT s.query_id, s.command
        FROM monitoring.pg_log_event s
       WHERE s.session_id = l.session_id
         AND s.line_num  > l.line_num
         AND s.record_type IN ('execute', 'statement')
       ORDER BY s.line_num
       LIMIT 1
  ) v ON true
 ORDER BY l.id
"""

# A dimensão é identidade e nada mais. Contagem, datas e agregados vivem em
# monitoring.query_resumo, mantida por trigger sobre plan_execution — que é a
# única forma de o número não poder divergir do fato.
SQL_UPSERT_QUERY = """
INSERT INTO monitoring.query (database_name, query_hash, query_text, query_id)
VALUES (%s, %s, %s, %s)
ON CONFLICT (database_name, query_hash) DO UPDATE
   -- Só preenche o query_id; nunca sobrescreve um id já conhecido por um nulo
   -- vindo de um plano do regime adiado.
   SET query_id = coalesce(monitoring.query.query_id, EXCLUDED.query_id)
RETURNING id
"""


@dataclass
class Estatisticas:
    planos: int = 0
    nos: int = 0
    queries_novas: int = 0
    ilegiveis: int = 0
    origem: Counter = None

    def __post_init__(self) -> None:
        if self.origem is None:
            self.origem = Counter()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

class Worker:
    def __init__(self, conn, lote: int = LOTE_PADRAO,
                 verboso: bool = True) -> None:
        self.conn = conn
        self.lote = lote
        self.verboso = verboso
        self.estat = Estatisticas()
        # Cache de (database_name, hash) -> query.id, para não fazer um upsert
        # por plano: no corpus são 193 mil planos para poucas centenas de
        # textos distintos.
        self._cache: Dict[Tuple[str, str], int] = {}
        # As queries tocadas nesta execução. As colunas incrementais de
        # query_resumo são mantidas por trigger, mas as de análise (que vêm de
        # plan_node) não são — precisam desta lista.
        self._tocadas: set = set()

    # -- marca d'água ----------------------------------------------------

    def marca(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT last_event_id FROM monitoring.worker_progress "
                "WHERE worker_name = %s", (NOME_WORKER,))
            linha = cur.fetchone()
        return linha[0] if linha else 0

    def gravar_marca(self, ultimo: int, processados: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO monitoring.worker_progress
                       (worker_name, last_event_id, events_processed,
                        last_run_at, updated_at)
                VALUES (%s, %s, %s, now(), now())
                ON CONFLICT (worker_name) DO UPDATE
                   SET last_event_id    = EXCLUDED.last_event_id,
                       events_processed = monitoring.worker_progress
                                            .events_processed + %s,
                       last_run_at      = now(),
                       updated_at       = now()
            """, (NOME_WORKER, ultimo, processados, processados))

    def zerar(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE monitoring.worker_progress "
                        "SET last_event_id = 0, events_processed = 0 "
                        "WHERE worker_name = %s", (NOME_WORKER,))
        self.conn.commit()

    # -- dimensão --------------------------------------------------------

    def ref_da_query(self, cur, banco: str, sql: str,
                     qid: Optional[int]) -> int:
        chave = (banco, hash_do_texto(sql))
        ref = self._cache.get(chave)
        if ref is not None:
            return ref
        cur.execute(SQL_UPSERT_QUERY, (banco, chave[1], sql, qid))
        ref = cur.fetchone()[0]
        self._cache[chave] = ref
        self.estat.queries_novas += 1
        return ref

    # -- um lote ---------------------------------------------------------

    def processar_lote(self, desde: int) -> Tuple[int, int]:
        """Devolve (último id processado, quantos planos)."""
        with self.conn.cursor() as cur:
            cur.execute(SQL_LOTE, (desde, self.lote))
            linhas = cur.fetchall()

        if not linhas:
            return desde, 0

        execucoes: List[tuple] = []
        nos: List[tuple] = []
        dias = set()
        ultimo = desde

        with self.conn.cursor() as cur:
            for (eid, quando, sessao, linha, qid_plano, banco, usuario,
                 aplicacao, comando, duracao, msg,
                 qid_seguinte, cmd_seguinte) in linhas:
                ultimo = eid
                try:
                    plano = json.loads(msg) if msg else None
                except (ValueError, TypeError):
                    self.estat.ilegiveis += 1
                    continue
                if isinstance(plano, list):
                    plano = plano[0] if plano else None
                if not isinstance(plano, dict) or "Plan" not in plano:
                    self.estat.ilegiveis += 1
                    continue

                topo = plano["Plan"]
                arvore: List[dict] = []
                achatar(topo, arvore)
                profundidade = max(n["profundidade"] for n in arvore)

                sql = plano.get("Query Text") or ""
                qid, origem = resolver_query_id(qid_plano, qid_seguinte,
                                                cmd_seguinte)
                self.estat.origem[origem] += 1

                ref = self.ref_da_query(cur, banco or "?", sql, qid)
                self._tocadas.add(ref)
                dias.add(quando.date())

                execucoes.append((
                    eid, quando, ref, qid, origem, sessao, linha, banco,
                    usuario, aplicacao, comando, duracao,
                    len(arvore), profundidade, topo.get("Node Type"),
                    topo.get("Total Cost"), topo.get("Plan Rows"),
                    topo.get("Actual Rows"), topo.get("Actual Total Time"),
                    topo.get("Shared Hit Blocks"), topo.get("Shared Read Blocks"),
                    topo.get("Temp Read Blocks"), topo.get("Temp Written Blocks"),
                    topo.get("Shared I/O Read Time"), topo.get("WAL Records"),
                    topo.get("WAL Bytes"), "JIT" in plano,
                    Jsonb(plano.get("Settings")) if plano.get("Settings") is not None
                    else None,
                    plano.get("Query Parameters"),
                ))
                for n in arvore:
                    nos.append((eid, quando) + tuple(
                        n[c] for c in COLUNAS_NO[2:]))

                self.estat.planos += 1
                self.estat.nos += len(arvore)

            # As partições derivadas do dia têm que existir antes do COPY.
            for dia in sorted(dias):
                cur.execute(
                    "SELECT monitoring.ensure_partition_de('plan_execution', %s),"
                    "       monitoring.ensure_partition_de('plan_node', %s)",
                    (dia, dia))

            self._copiar(cur, "plan_execution", COLUNAS_EXECUCAO, execucoes)
            self._copiar(cur, "plan_node", COLUNAS_NO, nos)

            self.gravar_marca(ultimo, len(execucoes))

        self.conn.commit()
        return ultimo, len(execucoes)

    def _copiar(self, cur, tabela: str, colunas: Sequence[str],
                linhas: List[tuple]) -> None:
        """COPY para uma temporária e INSERT ... ON CONFLICT DO NOTHING.

        O DO NOTHING é o que torna --reprocessar seguro: reprocessar o corpus
        inteiro não duplica nada nem exige apagar as tabelas antes.
        """
        if not linhas:
            return
        tmp = f"tmp_{tabela}"
        lista = ", ".join(colunas)
        cur.execute(f"CREATE TEMP TABLE {tmp} "
                    f"(LIKE monitoring.{tabela}) ON COMMIT DROP")
        with cur.copy(f"COPY {tmp} ({lista}) FROM STDIN") as copia:
            for linha in linhas:
                copia.write_row(linha)
        cur.execute(f"INSERT INTO monitoring.{tabela} ({lista}) "
                    f"SELECT {lista} FROM {tmp} "
                    f"ON CONFLICT DO NOTHING")

    # -- análise ---------------------------------------------------------

    def analisar(self) -> int:
        """Preenche as colunas de query_resumo que a trigger não mantém.

        Elas vêm de plan_node — quais nós, quais relações, quanto o Seq Scan
        descartou — e não são agregáveis de forma incremental. Restringir às
        queries tocadas mantém o custo proporcional ao que chegou, e não ao
        tamanho do corpus.
        """
        if not self._tocadas:
            return 0
        with self.conn.cursor() as cur:
            cur.execute("SELECT monitoring.analisar_resumo(%s)",
                        (sorted(self._tocadas),))
            n = cur.fetchone()[0]
        self.conn.commit()
        self._tocadas.clear()
        return n

    # -- laço ------------------------------------------------------------

    def rodar(self) -> int:
        desde = self.marca()
        total = 0
        while True:
            inicio = time.perf_counter()
            desde, n = self.processar_lote(desde)
            if n == 0:
                break
            total += n
            if self.verboso:
                gasto = time.perf_counter() - inicio
                print(f"  + {n:>6,} planos  (até o evento {desde:,}, "
                      f"{n / gasto:,.0f} planos/s)")
        return total


# --------------------------------------------------------------------------
# Relatórios
# --------------------------------------------------------------------------

def mostrar_status(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT (SELECT count(*) FROM monitoring.pg_log_event
                     WHERE record_type = 'plan'),
                   (SELECT count(*) FROM monitoring.plan_execution),
                   (SELECT count(*) FROM monitoring.plan_node),
                   (SELECT count(*) FROM monitoring.query),
                   (SELECT coalesce(last_event_id, 0)
                      FROM monitoring.worker_progress
                     WHERE worker_name = %s)
        """, (NOME_WORKER,))
        planos, execucoes, nos, queries, marca = cur.fetchone()

    print(f"planos no bruto      {planos:>12,}")
    print(f"plan_execution       {execucoes:>12,}")
    print(f"plan_node            {nos:>12,}")
    print(f"queries distintas    {queries:>12,}")
    print(f"marca d'água         {marca:>12,}")

    if execucoes:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT query_id_origem, count(*),
                       round(100.0 * count(*) / sum(count(*)) OVER (), 1)
                  FROM monitoring.plan_execution GROUP BY 1 ORDER BY 2 DESC
            """)
            print("\norigem do query_id:")
            for origem, n, pct in cur.fetchall():
                print(f"  {origem:<12} {n:>10,}  ({pct}%)")


def mostrar_achados(conn) -> None:
    consultas = [
        ("Onde o planner mais erra a estimativa (nós com 1000+ linhas reais)", """
            SELECT n.node_type, n.relation_name,
                   count(*) AS ocorrencias,
                   round(avg(n.linhas_reais * n.loops)::numeric) AS reais,
                   round(avg(n.linhas_est   * n.loops)::numeric) AS estimadas,
                   -- Razão das médias, para ser lida junto com as duas colunas
                   -- acima. O pior caso individual vai na coluna seguinte.
                   round((avg(n.linhas_reais * n.loops)
                        / nullif(avg(n.linhas_est * n.loops), 0))::numeric, 1)
                     AS fator,
                   round(max((n.linhas_reais * n.loops + 1.0)
                           / (n.linhas_est   * n.loops + 1.0))::numeric, 1)
                     AS pior_caso
              FROM monitoring.plan_node n
             WHERE n.linhas_reais * n.loops > 1000
               AND n.relation_name IS NOT NULL
             GROUP BY 1, 2
            HAVING count(*) > 5
             ORDER BY fator DESC LIMIT 10
        """),
        ("Seq Scan que joga linha fora: candidato a índice", """
            SELECT n.relation_name,
                   count(*)                                    AS ocorrencias,
                   round(avg(n.linhas_removidas_filtro)::numeric) AS descartadas,
                   round(avg(n.tempo_total_ms)::numeric, 1)    AS ms,
                   left(min(n.filtro), 58)                     AS exemplo_de_filtro
              FROM monitoring.plan_node n
             WHERE n.node_type = 'Seq Scan'
               AND n.linhas_removidas_filtro > 0
             GROUP BY 1 ORDER BY 3 DESC LIMIT 10
        """),
        # Agrupa pela dimensão de texto, não pelo query_id: é o agrupamento que
        # vale para 100% dos planos, inclusive os 3,8% de origem indefinida.
        ("As queries mais caras no total", """
            SELECT left(trim(regexp_replace(q.query_text, '\\s+', ' ', 'g')), 52) AS query,
                   q.database_name                          AS banco,
                   count(*)                                 AS execucoes,
                   round(sum(x.duration_ms)::numeric, 1)    AS total_ms,
                   round(avg(x.duration_ms)::numeric, 2)    AS media_ms
              FROM monitoring.plan_execution x
              JOIN monitoring.query q ON q.id = x.query_ref
             GROUP BY 1, 2 ORDER BY total_ms DESC LIMIT 10
        """),
        ("Sort que derramou para disco", """
            SELECT n.sort_method, count(*) AS ocorrencias,
                   round(avg(n.sort_espaco_kb)::numeric) AS kb_medio,
                   round(max(n.sort_espaco_kb)::numeric) AS kb_maximo
              FROM monitoring.plan_node n
             WHERE n.sort_method IS NOT NULL
             GROUP BY 1 ORDER BY 2 DESC LIMIT 5
        """),
    ]
    for titulo, sql in consultas:
        print(f"\n=== {titulo} ===")
        with conn.cursor() as cur:
            cur.execute(sql)
            colunas = [d.name for d in cur.description]
            linhas = cur.fetchall()
        if not linhas:
            print("  (nada)")
            continue
        larguras = [max(len(c), *(len(str(l[i])) for l in linhas))
                    for i, c in enumerate(colunas)]
        print("  " + "  ".join(c.ljust(larguras[i])
                               for i, c in enumerate(colunas)))
        print("  " + "  ".join("-" * larguras[i] for i in range(len(colunas))))
        for l in linhas:
            print("  " + "  ".join(str(v).ljust(larguras[i])
                                   for i, v in enumerate(l)))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Transforma os planos do auto_explain em tabelas analisáveis.")
    ap.add_argument("--dsn", default=DSN_PADRAO)
    ap.add_argument("--lote", type=int, default=LOTE_PADRAO,
                    help=f"planos por transação (padrão: {LOTE_PADRAO})")
    ap.add_argument("--reprocessar", action="store_true",
                    help="volta a marca d'água a zero e relê o corpus inteiro "
                         "(seguro: o ON CONFLICT descarta o que já existe)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--achados", action="store_true",
                    help="mostra o que as tabelas derivadas já revelam")
    ap.add_argument("--seguir", action="store_true")
    ap.add_argument("--intervalo", type=float, default=10.0)
    ap.add_argument("--sem-analise", action="store_true",
                    dest="sem_analise",
                    help="não atualiza as colunas de análise de "
                         "monitoring.query_resumo (as que vêm de plan_node)")
    ap.add_argument("--silencioso", action="store_true")
    args = ap.parse_args(argv)

    try:
        conn = psycopg.connect(args.dsn)
    except psycopg.OperationalError as erro:
        print(f"não consegui conectar em {args.dsn}\n  {erro}", file=sys.stderr)
        return 2

    with conn:
        if args.status:
            mostrar_status(conn)
            return 0
        if args.achados:
            mostrar_achados(conn)
            return 0

        worker = Worker(conn, lote=args.lote, verboso=not args.silencioso)
        if args.reprocessar:
            worker.zerar()
            print("marca d'água zerada")

        try:
            while True:
                inicio = time.perf_counter()
                total = worker.rodar()
                analisadas = 0 if args.sem_analise else worker.analisar()
                gasto = time.perf_counter() - inicio
                if total and not args.silencioso:
                    extra = (f", {analisadas} no resumo" if analisadas else "")
                    print(f"  = {total:,} planos, {worker.estat.nos:,} nós"
                          f"{extra}, {gasto:.1f}s")
                elif args.seguir and not args.silencioso:
                    print("  = nada novo")
                if not args.seguir:
                    break
                time.sleep(args.intervalo)
        except KeyboardInterrupt:
            print("\ninterrompido — a marca d'água do último lote está gravada")

        if not args.silencioso:
            if worker.estat.ilegiveis:
                print(f"\n{worker.estat.ilegiveis:,} planos ilegíveis")
            print()
            mostrar_status(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
