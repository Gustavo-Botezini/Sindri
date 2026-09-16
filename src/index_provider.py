#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recomendador de índices do BrokkPG.

Etapa 3 do pipeline. O coletor grava o log bruto, o worker transforma os planos
em tabelas, e este aqui pega o que o data warehouse já sabe — quais queries
custam caro, o que elas varrem, quanto descartam, quais índices usam — junta
com o catálogo real do banco observado e pede a Claude que proponha mudanças
no conjunto de índices.

O que faz este recomendador diferente de um que só olha o SQL: ele leva
**números medidos**, não o texto da query. A diferença aparece na estimativa de
espaço. Um modelo que só vê "CREATE INDEX ON lineitem (l_shipdate)" chuta o
tamanho; um que recebe "lineitem tem 6.001.215 linhas, o índice
idx_lineitem_orderkey ocupa 128 MB para uma chave de 8 bytes, e l_shipdate tem
avg_width 4" calcula. Por isso o dossiê inclui pg_stats e o tamanho de cada
índice existente — e por isso o esquema de saída obriga a resposta a declarar
em `base_da_estimativa` de onde tirou cada número.

Duas fontes, dois bancos:

    monitoring.query_resumo  (brokk:5433)  o que a carga revelou
    pg_stat_user_indexes ... (tpch:5432)   o que existe hoje, com tamanho

Uso:

    python3 src/index_provider.py --dossie          # só mostra o que seria enviado
    python3 src/index_provider.py --csv saida.csv   # analisa e grava CSV
    python3 src/index_provider.py --planilha        # analisa e grava no Sheets
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# ===========================================================================
# CONFIGURAÇÃO
# ===========================================================================

# A chave da Anthropic. Deixe vazia e exporte ANTHROPIC_API_KEY, ou cole aqui.
#
# Se colar aqui: este arquivo está versionado. Uma chave commitada é uma chave
# vazada — o GitHub varre pushes públicos e a Anthropic revoga automaticamente
# o que aparece. Para uso local sem risco, deixe vazio e use a variável de
# ambiente, ou ponha a chave em .env (que o .gitignore já cobre).
CHAVE_ANTHROPIC = ""

MODELO = "claude-opus-5"

# Quantas queries do topo do ranking vão no dossiê. Elas entram inteiras, sem
# truncar: uma query cortada no meio faz o modelo recomendar índice para uma
# cláusula que ele não viu.
TOP_QUERIES = 25

# Expressões (filtros, junções, ordenações) por query. A cauda não muda
# decisão de índice e só engorda o dossiê.
EXPRESSOES_POR_QUERY = 12

# ---------------------------------------------------------------------------
# O prompt
# ---------------------------------------------------------------------------

PROMPT_SISTEMA = """\
Você é um especialista em ajuste de índices no PostgreSQL 18. Recebe o retrato
medido de uma carga real — não o palpite de alguém sobre ela — e propõe
mudanças no conjunto de índices.

O dossiê tem quatro partes:

  workload   as queries mais caras, com quantas vezes rodaram, quanto levaram,
             o que cada Seq Scan filtrou e quantas linhas descartou, as
             condições de junção, as chaves de ordenação e quais índices já são
             usados. Tudo isso vem dos planos do auto_explain, ou seja, é
             execução real e não estimativa do planner.
  indices    o que existe hoje no banco: definição, tamanho em disco e quantas
             varreduras cada um recebeu desde o último reset das estatísticas.
  tabelas    linhas vivas, tamanho, e a atividade de escrita (inserts, updates,
             deletes) de cada tabela.
  colunas    pg_stats: largura média, n_distinct e correlação física de cada
             coluna. É o que permite estimar tamanho e seletividade.

Procure, nesta ordem de valor:

1. Índice faltando. Seq Scan repetido que descarta muita linha por um filtro
   estável é o caso clássico. Prefira um índice que sirva a várias queries do
   dossiê a um que sirva a uma só.
2. Consolidação. Dois ou mais índices com prefixo comum costumam virar um só,
   às vezes com INCLUDE para cobrir as colunas projetadas e permitir Index Only
   Scan. Diga quais índices o novo torna redundantes.
3. Índice que não paga o que custa. idx_scan igual ou próximo de zero num
   índice grande, numa tabela que recebe escrita, é candidato a remoção —
   exceto se for UNIQUE ou der suporte a chave primária ou estrangeira, caso em
   que ele existe por integridade e não por desempenho, e deve ficar.

Regras que valem para toda recomendação:

- Estime o tamanho a partir dos números do dossiê, não de intuição. Ancore no
  bytes-por-linha dos índices que já existem naquela tabela e no avg_width das
  colunas. Escreva em `base_da_estimativa` a conta que você fez, com os números
  que usou. Se não der para estimar, diga isso ali em vez de inventar.
- Todo índice novo custa escrita. Se a tabela tem update ou insert relevante no
  dossiê, diga isso em `risco` — com o número.
- Cite as queries pelo `query_ref` do dossiê. Não descreva "a query de vendas".
- Quando o dado não sustentar uma recomendação, use a ação `investigar` e diga
  o que falta medir. Um "não sei" é mais útil que um palpite confiante.
- Não recomende índice para tabela do catálogo (pg_*).

Não há prêmio por encher a lista. Poucas recomendações fortes valem mais que
muitas fracas.
"""

# ---------------------------------------------------------------------------
# O formato da resposta
# ---------------------------------------------------------------------------
# Structured outputs: a resposta volta validada contra este esquema, então não
# há parse de prosa nem recomendação com campo faltando.

ESQUEMA_SAIDA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "recomendacoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "acao": {
                        "type": "string",
                        "enum": ["criar", "substituir", "remover", "investigar"],
                    },
                    "prioridade": {
                        "type": "string",
                        "enum": ["alta", "media", "baixa"],
                    },
                    "relacao": {"type": "string"},
                    "ddl": {
                        "type": "string",
                        "description": "SQL executável, ou vazio quando a ação é investigar",
                    },
                    "indices_afetados": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Índices que esta mudança substitui ou remove",
                    },
                    "espaco_novo_mb": {"type": "number"},
                    "espaco_liberado_mb": {"type": "number"},
                    "base_da_estimativa": {
                        "type": "string",
                        "description": "A conta feita, com os números do dossiê",
                    },
                    "queries_beneficiadas": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "query_ref do dossiê",
                    },
                    "ganho_estimado_ms": {"type": "number"},
                    "justificativa": {"type": "string"},
                    "risco": {"type": "string"},
                    "confianca": {"type": "number"},
                },
                "required": [
                    "acao", "prioridade", "relacao", "ddl", "indices_afetados",
                    "espaco_novo_mb", "espaco_liberado_mb", "base_da_estimativa",
                    "queries_beneficiadas", "ganho_estimado_ms",
                    "justificativa", "risco", "confianca",
                ],
                "additionalProperties": False,
            },
        },
        "observacoes": {
            "type": "string",
            "description": "O que o dossiê não permitiu concluir, e o que faltou medir",
        },
    },
    "required": ["recomendacoes", "observacoes"],
    "additionalProperties": False,
}

# Colunas da planilha, na ordem.
COLUNAS_PLANILHA = [
    ("prioridade",          "Prioridade"),
    ("acao",                "Ação"),
    ("relacao",             "Tabela"),
    ("ddl",                 "DDL"),
    ("indices_afetados",    "Índices substituídos"),
    ("espaco_novo_mb",      "Espaço novo (MB, est.)"),
    ("espaco_liberado_mb",  "Espaço liberado (MB, est.)"),
    ("saldo_mb",            "Saldo (MB, est.)"),
    ("ganho_estimado_ms",   "Ganho (ms, est.)"),
    ("queries_beneficiadas", "Queries (query_ref)"),
    ("confianca",           "Confiança"),
    ("justificativa",       "Justificativa"),
    ("base_da_estimativa",  "Base da estimativa"),
    ("risco",               "Risco"),
]

DSN_BROKK = os.environ.get(
    "BROKK_DSN", "postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk")
DSN_OBSERVADO = os.environ.get(
    "BROKK_DSN_OBSERVADO", "postgresql://sindri:sindri_dev_pwd@localhost:5432/tpch")

CREDENCIAL_GOOGLE = os.environ.get(
    "BROKK_GOOGLE_CRED", "google-credentials.json")
PLANILHA_NOME = "BrokkPG — recomendações de índice"


# ===========================================================================
# Coleta: o que a carga revelou (banco de monitoramento)
# ===========================================================================

SQL_WORKLOAD = """
SELECT query_ref, query_text, execucoes,
       round(duracao_media_ms::numeric, 2)  AS duracao_media_ms,
       round(duracao_total_ms::numeric)     AS duracao_total_ms,
       round(duracao_max_ms::numeric, 1)    AS duracao_max_ms,
       relacoes, indices, seq_scans,
       round(seq_scan_descartadas::numeric) AS seq_scan_descartadas,
       sorts_em_disco,
       round(pior_fator_no::numeric, 1)     AS pior_fator_no,
       shared_read_total,
       round(io_read_ms_total::numeric)     AS io_read_ms_total
  FROM monitoring.query_resumo
 WHERE database_name = %s
 ORDER BY duracao_total_ms DESC
 LIMIT %s
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


# ===========================================================================
# Coleta: o que existe hoje (banco observado)
# ===========================================================================

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


# ===========================================================================
# Montagem do dossiê
# ===========================================================================

def linhas_como_dicts(cur) -> List[dict]:
    colunas = [d.name for d in cur.description]
    return [dict(zip(colunas, linha)) for linha in cur.fetchall()]


def montar_dossie(banco: str, top: int,
                  expressoes_por_query: int = EXPRESSOES_POR_QUERY) -> dict:
    """Junta as duas fontes num único documento para o modelo."""
    import psycopg

    with psycopg.connect(DSN_BROKK) as conn, conn.cursor() as cur:
        cur.execute(SQL_WORKLOAD, (banco, top))
        workload = linhas_como_dicts(cur)
        refs = [q["query_ref"] for q in workload]

        expressoes: List[dict] = []
        if refs:
            cur.execute(SQL_EXPRESSOES, (refs, expressoes_por_query))
            expressoes = linhas_como_dicts(cur)

    # As expressões vão dentro de cada query, não numa lista solta: o modelo
    # precisa saber a qual query cada Seq Scan pertence.
    por_ref: Dict[int, List[dict]] = {}
    for e in expressoes:
        por_ref.setdefault(e.pop("query_ref"), []).append(e)
    for q in workload:
        q["expressoes"] = por_ref.get(q["query_ref"], [])

    with psycopg.connect(DSN_OBSERVADO) as conn, conn.cursor() as cur:
        cur.execute(SQL_INDICES)
        indices = linhas_como_dicts(cur)
        cur.execute(SQL_TABELAS)
        tabelas = linhas_como_dicts(cur)
        cur.execute(SQL_COLUNAS)
        colunas = linhas_como_dicts(cur)

    return {
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "banco": banco,
        "workload": workload,
        "indices": indices,
        "tabelas": tabelas,
        "colunas": colunas,
    }


def como_json(obj: Any) -> str:
    """Serializa com Decimal, datetime e afins virando texto."""
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


# ===========================================================================
# A chamada
# ===========================================================================

def do_env(raiz: Path) -> str:
    """Lê ANTHROPIC_API_KEY de um .env na raiz do projeto, se existir."""
    arquivo = raiz / ".env"
    if not arquivo.is_file():
        return ""
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha.startswith("ANTHROPIC_API_KEY") and "=" in linha:
            return linha.split("=", 1)[1].strip().strip("'\"")
    return ""


def chave() -> str:
    """Constante do topo, depois variável de ambiente, depois .env."""
    raiz = Path(__file__).resolve().parent.parent
    for valor in (CHAVE_ANTHROPIC,
                  os.environ.get("ANTHROPIC_API_KEY", ""),
                  do_env(raiz)):
        if valor and valor.strip():
            return valor.strip()
    sys.exit(
        "Sem chave da Anthropic. Escolha um destes:\n"
        "  1. preencha CHAVE_ANTHROPIC no topo de src/index_provider.py\n"
        "  2. export ANTHROPIC_API_KEY=sk-ant-...\n"
        "  3. crie um .env na raiz com ANTHROPIC_API_KEY=sk-ant-...\n"
        "As opções 2 e 3 não correm o risco de a chave ir para o git."
    )


def analisar(dossie: dict, verboso: bool = True) -> dict:
    """Manda o dossiê e devolve as recomendações já validadas pelo esquema."""
    import anthropic

    cliente = anthropic.Anthropic(api_key=chave())

    pedido = (
        "Segue o dossiê da carga. Analise e devolva as recomendações de índice.\n\n"
        "<dossie>\n" + como_json(dossie) + "\n</dossie>"
    )

    if verboso:
        print(f"  enviando {len(pedido):,} caracteres para {MODELO}...")

    # Streaming porque a entrada é grande e o pensamento adaptativo em effort
    # alto pode levar minutos — sem stream isso bate no timeout de HTTP.
    with cliente.messages.stream(
        model=MODELO,
        max_tokens=32000,
        system=PROMPT_SISTEMA,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": ESQUEMA_SAIDA},
        },
        messages=[{"role": "user", "content": pedido}],
    ) as stream:
        resposta = stream.get_final_message()

    if resposta.stop_reason == "refusal":
        detalhe = getattr(resposta, "stop_details", None)
        sys.exit(f"o modelo recusou a análise: {detalhe}")

    texto = next(b.text for b in resposta.content if b.type == "text")
    saida = json.loads(texto)

    if verboso:
        u = resposta.usage
        print(f"  {u.input_tokens:,} tokens de entrada, "
              f"{u.output_tokens:,} de saída")
    return saida


# ===========================================================================
# Saída
# ===========================================================================

ORDEM_PRIORIDADE = {"alta": 0, "media": 1, "baixa": 2}


def em_linhas(saida: dict) -> List[List[str]]:
    """Recomendações viram linhas de planilha, mais alta prioridade primeiro."""
    recs = sorted(saida.get("recomendacoes", []),
                  key=lambda r: (ORDEM_PRIORIDADE.get(r.get("prioridade"), 9),
                                 -float(r.get("ganho_estimado_ms") or 0)))
    linhas = [[titulo for _, titulo in COLUNAS_PLANILHA]]
    for r in recs:
        novo = float(r.get("espaco_novo_mb") or 0)
        liberado = float(r.get("espaco_liberado_mb") or 0)
        valores = dict(r)
        valores["saldo_mb"] = round(novo - liberado, 1)
        linha = []
        for campo, _ in COLUNAS_PLANILHA:
            v = valores.get(campo, "")
            if isinstance(v, list):
                v = ", ".join(str(i) for i in v)
            linha.append("" if v is None else str(v))
        linhas.append(linha)
    return linhas


def gravar_csv(saida: dict, caminho: Path) -> None:
    with open(caminho, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(em_linhas(saida))
    print(f"  CSV em {caminho}")


def gravar_planilha(saida: dict, dossie: dict, credencial: str,
                    nome: str, compartilhar: Optional[str]) -> str:
    """Cria (ou reaproveita) a planilha no Google Sheets e devolve a URL."""
    import gspread
    from google.oauth2.service_account import Credentials

    caminho = Path(credencial)
    if not caminho.is_file():
        sys.exit(
            f"credencial do Google não encontrada em {caminho}.\n"
            "É um JSON de service account. Para criar:\n"
            "  1. console.cloud.google.com -> novo projeto\n"
            "  2. habilite Google Sheets API e Google Drive API\n"
            "  3. IAM -> Contas de serviço -> criar -> Chaves -> JSON\n"
            f"  4. salve como {caminho}\n"
            "Ou use --csv, que não precisa de nada disso."
        )

    escopos = ["https://www.googleapis.com/auth/spreadsheets",
               "https://www.googleapis.com/auth/drive"]
    cliente = gspread.authorize(
        Credentials.from_service_account_file(str(caminho), scopes=escopos))

    try:
        planilha = cliente.open(nome)
    except gspread.SpreadsheetNotFound:
        planilha = cliente.create(nome)

    # A service account é dona do arquivo e ninguém mais o enxerga por padrão;
    # sem este share a planilha existe mas você não consegue abrir.
    if compartilhar:
        planilha.share(compartilhar, perm_type="user", role="writer")

    linhas = em_linhas(saida)
    aba = planilha.sheet1
    aba.clear()
    aba.update(values=linhas, range_name="A1")
    aba.freeze(rows=1)
    aba.format("A1:N1", {"textFormat": {"bold": True}})

    # Segunda aba com o contexto: sem ela a planilha é um veredito sem processo.
    try:
        contexto = planilha.worksheet("contexto")
    except gspread.WorksheetNotFound:
        contexto = planilha.add_worksheet("contexto", rows=100, cols=4)
    contexto.clear()
    contexto.update(values=[
        ["Gerado em", dossie["gerado_em"]],
        ["Banco analisado", dossie["banco"]],
        ["Modelo", MODELO],
        ["Queries no dossiê", str(len(dossie["workload"]))],
        ["Índices existentes", str(len(dossie["indices"]))],
        ["Tabelas", str(len(dossie["tabelas"]))],
        ["", ""],
        ["Observações do modelo", saida.get("observacoes", "")],
        ["", ""],
        ["Aviso", "Espaço e ganho são ESTIMATIVAS. Confira a coluna "
                  "'Base da estimativa' e valide com EXPLAIN antes de aplicar."],
    ], range_name="A1")

    return planilha.url


# ===========================================================================
# CLI
# ===========================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    raiz = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Propõe mudanças de índice a partir do data warehouse do BrokkPG.")
    ap.add_argument("--banco", default="tpch",
                    help="qual banco do corpus analisar (padrão: tpch)")
    ap.add_argument("--top", type=int, default=TOP_QUERIES,
                    help=f"quantas queries do topo enviar (padrão: {TOP_QUERIES})")
    ap.add_argument("--dossie", action="store_true",
                    help="só monta e imprime o dossiê, sem chamar a API")
    ap.add_argument("--salvar-dossie", type=Path, metavar="ARQ",
                    help="grava o dossiê em JSON")
    ap.add_argument("--csv", type=Path, metavar="ARQ",
                    help="grava as recomendações em CSV")
    ap.add_argument("--planilha", action="store_true",
                    help="grava as recomendações no Google Sheets")
    ap.add_argument("--credencial", default=CREDENCIAL_GOOGLE,
                    help=f"JSON da service account (padrão: {CREDENCIAL_GOOGLE})")
    ap.add_argument("--nome-planilha", default=PLANILHA_NOME)
    ap.add_argument("--compartilhar", metavar="EMAIL",
                    help="e-mail que recebe acesso de escrita à planilha")
    ap.add_argument("--json", type=Path, metavar="ARQ", dest="json_saida",
                    help="grava a resposta crua do modelo")
    args = ap.parse_args(argv)

    print(f"montando o dossiê ({args.banco}, top {args.top})...")
    dossie = montar_dossie(args.banco, args.top)
    tamanho = len(como_json(dossie))
    print(f"  {len(dossie['workload'])} queries, "
          f"{len(dossie['indices'])} índices, "
          f"{len(dossie['tabelas'])} tabelas, "
          f"{len(dossie['colunas'])} colunas — {tamanho:,} caracteres")

    if args.salvar_dossie:
        args.salvar_dossie.write_text(como_json(dossie), encoding="utf-8")
        print(f"  dossiê em {args.salvar_dossie}")

    if args.dossie:
        if not args.salvar_dossie:
            print(como_json(dossie))
        return 0

    print(f"analisando com {MODELO}...")
    saida = analisar(dossie)
    recs = saida.get("recomendacoes", [])
    print(f"  {len(recs)} recomendação(ões)")

    if args.json_saida:
        args.json_saida.write_text(como_json(saida), encoding="utf-8")
        print(f"  JSON em {args.json_saida}")

    if args.csv:
        gravar_csv(saida, args.csv)

    if args.planilha:
        url = gravar_planilha(saida, dossie, args.credencial,
                              args.nome_planilha, args.compartilhar)
        print(f"  planilha em {url}")

    if not (args.csv or args.planilha or args.json_saida):
        # Sem destino escolhido, mostra no terminal em vez de perder o trabalho.
        for linha in em_linhas(saida):
            print("  " + " | ".join(c[:40] for c in linha[:6]))
        print(f"\n{saida.get('observacoes', '')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
