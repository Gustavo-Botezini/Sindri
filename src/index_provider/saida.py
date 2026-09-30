#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Saída: terminal, CSV e Google Sheets.

Dois formatos, um por modo. O clássico exporta as recomendações do modelo; o
de clusters exporta o que a bancada mediu — e esse é o único dos dois em que
a coluna mais importante não veio de ninguém opinando.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .banco import linhas_como_dicts

ORDEM_PRIORIDADE = {"alta": 0, "media": 1, "baixa": 2}

# Colunas da planilha do modo clássico, na ordem.
COLUNAS_PLANILHA = [
    ("prioridade",           "Prioridade"),
    ("acao",                 "Ação"),
    ("relacao",              "Tabela"),
    ("ddl",                  "DDL"),
    ("indices_afetados",     "Índices substituídos"),
    ("espaco_novo_mb",       "Espaço novo (MB, est.)"),
    ("espaco_liberado_mb",   "Espaço liberado (MB, est.)"),
    ("saldo_mb",             "Saldo (MB, est.)"),
    ("ganho_estimado_ms",    "Ganho (ms, est.)"),
    ("queries_beneficiadas", "Queries (query_ref)"),
    ("confianca",            "Confiança"),
    ("justificativa",        "Justificativa"),
    ("base_da_estimativa",   "Base da estimativa"),
    ("risco",                "Risco"),
]

# Colunas do relatório de clusters. Aqui não há "estimado": tudo que não é o
# DDL saiu de um relógio.
COLUNAS_CLUSTER = [
    ("cluster_id",     "Cluster"),
    ("query_ref",      "Query"),
    ("e_a_origem",     "É a origem"),
    ("relacao",        "Tabela"),
    ("ddl",            "DDL testado"),
    ("antes_ms",       "Antes (ms)"),
    ("depois_ms",      "Depois (ms)"),
    ("ganho_ms",       "Ganho (ms)"),
    ("ganho_pct",      "Ganho (%)"),
    ("usou_indice",    "Usou o índice"),
    ("amostras_antes", "Amostras antes"),
    ("amostras_depois", "Amostras depois"),
]


# O agrupamento em si, para quem quer conferir o que o algoritmo fez em vez de
# confiar no resumo do terminal. Traz o texto da query junto: sem ele, um
# cluster é uma lista de números.
SQL_PONTOS = """
SELECT c.cluster_id, c.query_ref,
       round(c.probabilidade::numeric, 4) AS probabilidade,
       round(c.outlier::numeric, 4)       AS outlier,
       round(c.x::numeric, 4) AS x,
       round(c.y::numeric, 4) AS y,
       r.execucoes,
       round(r.duracao_total_ms::numeric) AS duracao_total_ms,
       round(r.duracao_media_ms::numeric, 1) AS duracao_media_ms,
       r.relacoes, r.seq_scans,
       round(r.seq_scan_descartadas::numeric) AS seq_scan_descartadas,
       r.query_text
  FROM monitoring.query_cluster c
  JOIN monitoring.query_resumo r ON r.query_ref = c.query_ref
 WHERE c.rodada_id = %s
 ORDER BY c.cluster_id, r.duracao_total_ms DESC
"""

SQL_RESULTADO = """
SELECT cluster_id, query_ref, e_a_origem, relacao, ddl,
       antes_ms, depois_ms, ganho_ms, ganho_pct, usou_indice,
       amostras_antes, amostras_depois
  FROM monitoring.experimento_resultado
 WHERE rodada_id = %s
 ORDER BY cluster_id, ganho_ms DESC NULLS LAST
"""

SQL_VEREDITO = """
SELECT cluster_id, experimento_id, relacao, ddl, queries, usaram_indice,
       melhoraram, pioraram, sem_efeito, ganho_atribuivel_ms,
       ganho_fora_da_origem_ms, ganho_bruto_ms, melhor_ms, pior_ms
  FROM monitoring.experimento_veredito
 WHERE rodada_id = %s
 ORDER BY cluster_id
"""

SQL_SEM_MEDIDA = """
SELECT e.cluster_id, e.estado, e.erro, e.ddl, e.justificativa
  FROM monitoring.experimento e
 WHERE e.rodada_id = %s
   AND NOT EXISTS (SELECT 1 FROM monitoring.experimento_resultado r
                    WHERE r.experimento_id = e.id)
 ORDER BY e.cluster_id
"""


# --------------------------------------------------------------------------
# Modo clássico
# --------------------------------------------------------------------------

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
        linhas.append(_linha(valores, COLUNAS_PLANILHA))
    return linhas


# --------------------------------------------------------------------------
# Modo de clusters
# --------------------------------------------------------------------------

def pontos(conn, rodada_id: int) -> List[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL_PONTOS, (rodada_id,))
        return linhas_como_dicts(cur)


# O agrupamento em CSV. `texto_embedado` é a coluna que responde "por que esta
# query caiu neste cluster": é literalmente a string que virou vetor, depois
# da normalização — com o espaço colapsado, ela cabe numa célula de planilha,
# o que o SQL original não faz.
COLUNAS_PONTOS = [
    ("cluster_id",           "Cluster"),
    ("query_ref",            "Query"),
    ("probabilidade",        "Probabilidade"),
    ("outlier",              "Outlier"),
    ("x",                    "x"),
    ("y",                    "y"),
    ("execucoes",            "Execuções"),
    ("duracao_total_ms",     "Duração total (ms)"),
    ("duracao_media_ms",     "Duração média (ms)"),
    ("relacoes",             "Relações"),
    ("seq_scans",            "Seq Scans"),
    ("seq_scan_descartadas", "Linhas descartadas"),
    ("texto_embedado",       "Texto embedado"),
]


def pontos_em_linhas(registros: Sequence[dict]) -> List[List[str]]:
    linhas = [[titulo for _, titulo in COLUNAS_PONTOS]]
    for r in registros:
        linhas.append(_linha(r, COLUNAS_PONTOS))
    return linhas


def resultado_em_linhas(conn, rodada_id: int) -> List[List[str]]:
    with conn.cursor() as cur:
        cur.execute(SQL_RESULTADO, (rodada_id,))
        registros = linhas_como_dicts(cur)
    linhas = [[titulo for _, titulo in COLUNAS_CLUSTER]]
    for r in registros:
        linhas.append(_linha(r, COLUNAS_CLUSTER))
    return linhas


def veredito(conn, rodada_id: int) -> List[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL_VEREDITO, (rodada_id,))
        return linhas_como_dicts(cur)


def sem_medida(conn, rodada_id: int) -> List[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL_SEM_MEDIDA, (rodada_id,))
        return linhas_como_dicts(cur)


def mostrar_veredito(conn, rodada_id: int) -> None:
    """A resposta à pergunta do experimento, em uma tela."""
    linhas = veredito(conn, rodada_id)
    if not linhas:
        print("  nenhum experimento medido nesta rodada")
    for v in linhas:
        print(f"  cluster {v['cluster_id']}  {v['relacao']}")
        print(f"      {v['ddl']}")
        print(f"      {v['queries']} queries medidas: {v['usaram_indice']} "
              f"usaram o índice ({v['melhoraram']} melhoraram, "
              f"{v['pioraram']} pioraram), {v['sem_efeito']} não o tocaram")
        print(f"      ganho atribuível  {v['ganho_atribuivel_ms']:>10,} ms")
        print(f"      fora da origem    {v['ganho_fora_da_origem_ms']:>10,} ms"
              f"   <- responde à hipótese")
        if v["melhor_ms"] is not None:
            print(f"      melhor {v['melhor_ms']:,} ms, pior {v['pior_ms']:,} ms"
                  f"  (bruto, com ruído: {v['ganho_bruto_ms']:,} ms)")

    faltando = sem_medida(conn, rodada_id)
    for f in faltando:
        motivo = f["erro"] or f["estado"]
        print(f"  cluster {f['cluster_id']}  sem medida ({motivo})")


# --------------------------------------------------------------------------
# Destinos
# --------------------------------------------------------------------------

def _linha(valores: dict, colunas) -> List[str]:
    linha = []
    for campo, _ in colunas:
        v = valores.get(campo, "")
        if isinstance(v, list):
            v = ", ".join(str(i) for i in v)
        linha.append("" if v is None else str(v))
    return linha


def gravar_csv(linhas: Sequence[Sequence[str]], caminho: Path) -> None:
    with open(caminho, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(linhas)
    print(f"  CSV em {caminho}")


def gravar_planilha(linhas: Sequence[Sequence[str]], contexto: Sequence[Sequence[str]],
                    credencial: str, nome: str,
                    compartilhar: Optional[str]) -> str:
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

    aba = planilha.sheet1
    aba.clear()
    aba.update(values=list(linhas), range_name="A1")
    aba.freeze(rows=1)
    aba.format("A1:N1", {"textFormat": {"bold": True}})

    # Segunda aba com o contexto: sem ela a planilha é um veredito sem processo.
    try:
        ctx = planilha.worksheet("contexto")
    except gspread.WorksheetNotFound:
        ctx = planilha.add_worksheet("contexto", rows=100, cols=4)
    ctx.clear()
    ctx.update(values=[list(l) for l in contexto], range_name="A1")

    return planilha.url
