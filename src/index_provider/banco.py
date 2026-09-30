#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conexões, DSNs e o esquema do laboratório.

Dois bancos, e a separação é deliberada (ver initdb-monitoring/01-schema.sql):

    brokk:5433   o data warehouse — query_resumo, plan_node, e as tabelas
                 que este pacote acrescenta
    tpch:5432    o banco observado — o catálogo real, e onde a bancada cria
                 e derruba índice de verdade
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit(
        "psycopg 3 não encontrado. No venv do projeto:\n"
        "    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
    )


# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

DSN_BROKK = os.environ.get(
    "BROKK_DSN", "postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk")
DSN_OBSERVADO = os.environ.get(
    "BROKK_DSN_OBSERVADO", "postgresql://sindri:sindri_dev_pwd@localhost:5432/tpch")

CREDENCIAL_GOOGLE = os.environ.get(
    "BROKK_GOOGLE_CRED", "google-credentials.json")
PLANILHA_NOME = "BrokkPG — recomendações de índice"

RAIZ = Path(__file__).resolve().parent.parent.parent
ESQUEMA = Path(__file__).with_name("esquema.sql")


# --------------------------------------------------------------------------
# Conexão
# --------------------------------------------------------------------------

def conectar(dsn: str, papel: str = "monitoramento",
             app: Optional[str] = None):
    """Abre a conexão, ou sai com a dica de como subir o banco.

    `app` vira o application_name já no handshake, e não num SET depois. A
    diferença importa no banco observado: ele loga toda query a partir do
    primeiro statement, então um SET application_name posterior chega tarde
    demais para marcar o próprio statement que desliga o log.
    """
    extra = {"application_name": app} if app else {}
    try:
        return psycopg.connect(dsn, **extra)
    except psycopg.OperationalError as erro:
        print(f"não consegui conectar em {dsn} ({papel})\n  {erro}",
              file=sys.stderr)
        print("os bancos sobem com: docker compose up -d", file=sys.stderr)
        raise SystemExit(2)


def linhas_como_dicts(cur) -> List[dict]:
    colunas = [d.name for d in cur.description]
    return [dict(zip(colunas, linha)) for linha in cur.fetchall()]


def como_json(obj: Any) -> str:
    """Serializa com Decimal, datetime e afins virando texto."""
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


# --------------------------------------------------------------------------
# O esquema do laboratório
# --------------------------------------------------------------------------

def aplicar_esquema(conn, verboso: bool = False) -> None:
    """Aplica esquema.sql. Idempotente, roda em toda execução.

    Não dá para pôr isso só em initdb-monitoring/: aquilo roda uma vez, em
    banco vazio, e recriar o volume custaria os planos já coletados.
    """
    sql = ESQUEMA.read_text(encoding="utf-8")
    # Sem parâmetros, o psycopg usa o protocolo simples e aceita o arquivo
    # inteiro numa execute() só.
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    if verboso:
        print(f"  esquema aplicado ({ESQUEMA.name})")


# --------------------------------------------------------------------------
# Chave da Anthropic
# --------------------------------------------------------------------------
# Constante do topo de modelo.py, depois variável de ambiente, depois .env.
# As duas últimas ficam fora do git; a primeira está lá porque foi pedida.

def do_env(raiz: Path = RAIZ) -> str:
    """Lê ANTHROPIC_API_KEY de um .env na raiz do projeto, se existir."""
    arquivo = raiz / ".env"
    if not arquivo.is_file():
        return ""
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha.startswith("ANTHROPIC_API_KEY") and "=" in linha:
            return linha.split("=", 1)[1].strip().strip("'\"")
    return ""


def chave(constante: str = "") -> str:
    for valor in (constante,
                  os.environ.get("ANTHROPIC_API_KEY", ""),
                  do_env()):
        if valor and valor.strip():
            return valor.strip()
    sys.exit(
        "Sem chave da Anthropic. Escolha um destes:\n"
        "  1. preencha CHAVE_ANTHROPIC no topo de src/index_provider/modelo.py\n"
        "  2. export ANTHROPIC_API_KEY=sk-ant-...\n"
        "  3. crie um .env na raiz com ANTHROPIC_API_KEY=sk-ant-...\n"
        "As opções 2 e 3 não correm o risco de a chave ir para o git."
    )
