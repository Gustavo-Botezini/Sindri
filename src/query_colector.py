#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coletor do BrokkPG.

Lê o jsonlog do Postgres observado em ``logs/`` e alimenta o banco de
monitoramento (``sindri_monitoring``, porta 5433). É a Etapa 1 do pipeline: o
coletor grava o evento bruto e termina. Quem interpreta o plano do
``auto_explain`` é o worker, depois, por conta própria.

O contrato com o banco está em ``initdb-monitoring/02-functions.sql``:

    1. INSERT em monitoring.ingestion_file            -> devolve file_id
    2. COPY  monitoring.pg_log_event_staging FROM STDIN
    3. SELECT * FROM monitoring.ingest_staging(file_id)
    4. UPDATE do deslocamento em monitoring.ingestion_file

Os passos 2 a 4 vão numa transação por lote. Se a execução for interrompida no
meio de um arquivo, a próxima retoma do último lote que fechou.

Três coisas que o corpus obrigou a tratar, e que explicam o tamanho deste
arquivo:

  * **Os dois formatos convivem.** Metade dos arquivos em ``logs/`` é jsonlog
    cru (um objeto por linha) e metade está identada em dezenas de linhas.
    ``json.JSONDecoder.raw_decode`` em cima de um buffer deslizante lê os dois
    sem saber a diferença.

  * **O arquivo corrente ainda cresce.** A rotação é por 50 MB, então há sempre
    um arquivo aberto sendo escrito enquanto o coletor lê. Por isso o controle
    é por deslocamento em bytes, e não por um status de "já li este arquivo".

  * **O deslocamento é uma dica, não um dogma.** A garantia de não duplicar vem
    do índice único ``(session_id, line_num, event_time)``, que é a chave
    natural do log do Postgres. Se o deslocamento gravado cair no meio de um
    registro — arquivo truncado, byte inválido, o que for — o leitor ressincroniza
    procurando o próximo objeto e o banco descarta o que já tiver.

Uso:

    python3 src/query_colector.py                  # ingere o que há de novo
    python3 src/query_colector.py --seguir         # fica acompanhando
    python3 src/query_colector.py --status         # só mostra o estado
    python3 src/query_colector.py --reingerir      # zera os deslocamentos
    python3 src/query_colector.py --limite 5000    # para testes

Depende de psycopg 3 (``pip install 'psycopg[binary]'``). O escaping do COPY
fica com o driver de propósito: o payload de um registro de plano é JSON com
barras invertidas e quebras de linha dentro, e montar isso à mão é onde esse
tipo de coletor costuma quebrar.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    sys.exit(
        "psycopg 3 não encontrado. No venv do projeto:\n"
        "    python3 -m venv .venv && ./.venv/bin/pip install 'psycopg[binary]'\n"
        "e rode com ./.venv/bin/python src/query_colector.py"
    )


# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

DSN_PADRAO = os.environ.get(
    "BROKK_DSN", "postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk"
)

# Onde o container do banco de monitoramento enxerga a pasta logs/. O
# file_path gravado usa este prefixo para significar a mesma coisa dos dois
# lados, independentemente de onde o coletor esteja rodando.
PREFIXO_PADRAO = "/var/log/postgresql"

BLOCO = 1 << 20          # 1 MB por leitura
LOTE_PADRAO = 20000      # eventos por transação
CABECA_HASH = 65536      # bytes do início do arquivo que viram a impressão

# Acima disto sem conseguir decodificar, o buffer é lixo e não registro grande.
# O maior message do corpus tem 45 KB; 8 MB é folga de sobra.
LIMITE_REGISTRO = 8 << 20

# Chaves do jsonlog que têm coluna própria em monitoring.pg_log_event. O que
# não estiver aqui vai para raw_extra — que na prática fica quase sempre nulo,
# porque o corpus inteiro tem 19 chaves distintas e todas estão mapeadas.
CHAVES_MAPEADAS = frozenset({
    "timestamp", "user", "dbname", "pid", "leader_pid", "remote_host",
    "remote_port", "session_id", "line_num", "ps", "session_start", "vxid",
    "txid", "error_severity", "state_code", "message", "detail", "hint",
    "context", "backend_type", "query_id", "application_name",
})

# Ordem das colunas no COPY. Tem que bater com a ordem montada em
# linha_da_staging().
COLUNAS_STAGING = (
    "ingestion_file_id", "event_time", "session_id", "line_num", "record_type",
    "duration_ms", "pid", "leader_pid", "remote_port", "transaction_id",
    "query_id", "session_start", "username", "database_name",
    "application_name", "backend_type", "remote_host", "vxid",
    "error_severity", "state_code", "command", "message", "detail", "hint",
    "context", "raw_extra",
)


# --------------------------------------------------------------------------
# Classificação do message
# --------------------------------------------------------------------------
# O prefixo "duration: X ms  " aparece em 99,7% dos registros do corpus, com
# cinco continuações diferentes. Separar isso aqui é o que evita que um
# avg(duration_ms) mais adiante misture o tempo de um bind (0,17 ms de média)
# com o de um plano (132 ms) — 78% dos registros são parse/bind/execute e
# apenas 21,5% são plano.

RE_DURACAO = re.compile(
    r"^duration:\s+([0-9]+(?:\.[0-9]+)?)\s+ms\s{1,2}(.*)$", re.DOTALL
)

# "plan:\n{...}" e "statement: SELECT ..." — a palavra vem com dois pontos.
TIPOS_COM_DOIS_PONTOS = ("plan", "statement")
# "execute S_1: SELECT ..." — a palavra vem seguida do nome do portal, que
# fica no message (não há coluna para ele; o worker separa no primeiro ": ").
TIPOS_COM_PORTAL = ("execute", "bind", "parse")


def classificar(message: str) -> Tuple[str, Optional[float], str]:
    """Separa (record_type, duration_ms, payload) do campo message.

    A regra é sempre a mesma: tira o prefixo de duração, tira a palavra que
    nomeia o tipo, e o que sobra é o payload — o JSON do plano, o SQL, ou
    ``S_1: COMMIT``. Nada é jogado fora.
    """
    if not message:
        return "other", None, ""

    achou = RE_DURACAO.match(message)
    if not achou:
        return "other", None, message

    duracao = float(achou.group(1))
    resto = achou.group(2).lstrip()

    for tipo in TIPOS_COM_DOIS_PONTOS:
        if resto.startswith(tipo + ":"):
            # No caso do plano o payload começa na linha seguinte.
            return tipo, duracao, resto[len(tipo) + 1:].lstrip("\r\n").lstrip()

    for tipo in TIPOS_COM_PORTAL:
        if resto.startswith(tipo + " "):
            return tipo, duracao, resto[len(tipo):].lstrip()

    # Forma desconhecida: guarda a duração, que é real, e não força um tipo.
    return "other", duracao, resto


# --------------------------------------------------------------------------
# Conversão de evento para linha da staging
# --------------------------------------------------------------------------

def momento(bruto: Optional[str], estat: Counter) -> Optional[datetime]:
    """Converte "2026-09-06 21:13:40.482 UTC" em datetime com fuso.

    O jsonlog escreve o fuso por extenso no fim, conforme o log_timezone do
    servidor. O container observado roda em UTC; qualquer outro sufixo é
    contado e tratado como UTC, porque errar o fuso em silêncio deslocaria as
    partições diárias.
    """
    if not bruto:
        return None
    partes = bruto.rsplit(" ", 1)
    if len(partes) == 2 and partes[1].isalpha():
        instante, zona = partes
        if zona not in ("UTC", "GMT"):
            estat["fuso_inesperado"] += 1
    else:
        instante = bruto
    try:
        return datetime.fromisoformat(instante).replace(tzinfo=timezone.utc)
    except ValueError:
        estat["timestamp_ilegivel"] += 1
        return None


def linha_da_staging(evento: dict, file_id: int,
                     estat: Counter) -> Optional[tuple]:
    """Monta a tupla do COPY na ordem de COLUNAS_STAGING."""
    instante = momento(evento.get("timestamp"), estat)
    sessao = evento.get("session_id")
    linha = evento.get("line_num")

    # event_time, session_id e line_num são NOT NULL no destino, e juntos são
    # a chave natural que garante a idempotência. Registro sem os três não
    # tem como ser deduplicado, então é melhor contá-lo do que inseri-lo.
    if instante is None or sessao is None or linha is None:
        estat["sem_chave_natural"] += 1
        return None

    tipo, duracao, payload = classificar(evento.get("message") or "")
    extra = {k: v for k, v in evento.items() if k not in CHAVES_MAPEADAS}
    if extra:
        estat["com_raw_extra"] += 1
    estat[f"tipo_{tipo}"] += 1

    return (
        file_id,
        instante,
        sessao,
        linha,
        tipo,
        duracao,
        evento.get("pid"),
        evento.get("leader_pid"),
        evento.get("remote_port"),
        evento.get("txid"),
        evento.get("query_id"),
        momento(evento.get("session_start"), estat),
        evento.get("user"),
        evento.get("dbname"),
        evento.get("application_name"),
        evento.get("backend_type"),
        evento.get("remote_host"),
        evento.get("vxid"),
        evento.get("error_severity"),
        evento.get("state_code"),
        evento.get("ps"),
        payload or None,
        evento.get("detail"),
        evento.get("hint"),
        evento.get("context"),
        Jsonb(extra) if extra else None,
    )


# --------------------------------------------------------------------------
# Leitura do jsonlog com deslocamento em bytes
# --------------------------------------------------------------------------

@dataclass
class Lote:
    eventos: List[dict]
    deslocamento: int      # byte no arquivo logo após o último evento do lote
    fim_do_arquivo: bool


class LeitorJsonlog:
    """Percorre um arquivo de log devolvendo lotes de eventos e o byte em que
    cada lote termina.

    Aceita o jsonlog cru e o já formatado. O arquivo é aberto em binário e
    decodificado incrementalmente para que um caractere multibyte partido no
    limite do bloco não estrague a contagem — o deslocamento tem que ser em
    bytes, porque é ele que vai para o ``seek`` da próxima execução.
    """

    def __init__(self, caminho: Path, inicio: int = 0) -> None:
        self.caminho = caminho
        self.inicio = max(0, inicio)
        self.estat: Counter = Counter()

    def _contar(self, chave: str, n: int = 1) -> None:
        self.estat[chave] += n

    def lotes(self, tamanho: int = LOTE_PADRAO,
              limite: int = 0) -> Iterator[Lote]:
        decodificador = json.JSONDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")("replace")

        buffer = ""
        pos = 0                    # posição de leitura dentro de buffer
        deslocamento = self.inicio  # bytes confirmados no arquivo
        eventos: List[dict] = []
        lidos = 0

        with open(self.caminho, "rb") as fh:
            fh.seek(self.inicio)
            while True:
                pedaco = fh.read(BLOCO)
                fim_arquivo = not pedaco
                buffer += utf8.decode(pedaco, final=fim_arquivo)
                tamanho_buf = len(buffer)

                while True:
                    inicio_fatia = pos
                    while pos < tamanho_buf and buffer[pos] in " \t\r\n":
                        pos += 1
                    if pos >= tamanho_buf:
                        pos = inicio_fatia
                        break

                    try:
                        obj, fim = decodificador.raw_decode(buffer, pos)
                    except ValueError:
                        if not fim_arquivo and tamanho_buf - pos <= LIMITE_REGISTRO:
                            pos = inicio_fatia
                            break          # registro partido: precisa de mais bytes
                        # Lixo ou registro truncado. Procura o próximo objeto —
                        # é o que faz um deslocamento gravado errado se
                        # recuperar em vez de travar o arquivo para sempre.
                        proximo = buffer.find("\n{", pos + 1)
                        self._contar("registros_ilegiveis")
                        if proximo < 0:
                            deslocamento += len(
                                buffer[inicio_fatia:tamanho_buf].encode("utf-8"))
                            pos = tamanho_buf
                            break
                        deslocamento += len(
                            buffer[inicio_fatia:proximo + 1].encode("utf-8"))
                        pos = proximo + 1
                        continue

                    # Contabiliza whitespace + objeto de uma vez só: fatia curta,
                    # então o custo total da conversão fica linear no arquivo.
                    deslocamento += len(buffer[inicio_fatia:fim].encode("utf-8"))
                    pos = fim

                    if not isinstance(obj, dict):
                        # Não é jsonlog. O .log em texto puro cai aqui:
                        # "2026-09-06 ..." decodifica como o número 2026.
                        self._contar("registros_ilegiveis")
                        continue

                    eventos.append(obj)
                    lidos += 1
                    if len(eventos) >= tamanho:
                        yield Lote(eventos, deslocamento, False)
                        eventos = []
                    if limite and lidos >= limite:
                        if eventos:
                            yield Lote(eventos, deslocamento, False)
                        return

                buffer = buffer[pos:]
                pos = 0

                if fim_arquivo:
                    # O que sobrou é espaço em branco depois do último objeto
                    # (o \n final). Sem somar isto, bytes_ingested nunca chega
                    # a file_size_bytes e o arquivo parece eternamente parcial.
                    if buffer.strip():
                        self._contar("resto_ilegivel")
                    deslocamento += len(buffer.encode("utf-8"))
                    yield Lote(eventos, deslocamento, True)
                    return


def impressao_da_cabeca(caminho: Path) -> str:
    """sha256 dos primeiros 64 KB.

    Hash do arquivo inteiro não serve: ele cresce, e o hash mudaria a cada
    passada. O começo nunca muda, então isto detecta o caso de um nome de
    arquivo reaproveitado apontando para conteúdo diferente.
    """
    with open(caminho, "rb") as fh:
        return hashlib.sha256(fh.read(CABECA_HASH)).hexdigest()


# --------------------------------------------------------------------------
# Estado no banco
# --------------------------------------------------------------------------

@dataclass
class Arquivo:
    """O que o banco sabe sobre um arquivo de log."""
    id: int
    caminho: Path
    file_path: str
    tamanho: int
    deslocamento: int
    rotacionado: bool
    reiniciado: str = ""       # motivo, quando o deslocamento foi zerado


SQL_UPSERT = """
INSERT INTO monitoring.ingestion_file
       (file_name, file_path, file_size_bytes, head_fingerprint,
        status, started_at)
VALUES (%s, %s, %s, %s, 'processing', now())
ON CONFLICT (file_path) DO UPDATE
   SET file_size_bytes = EXCLUDED.file_size_bytes,
       status          = 'processing',
       started_at      = now()
RETURNING id, bytes_ingested, head_fingerprint
"""

SQL_RESETAR = """
UPDATE monitoring.ingestion_file
   SET bytes_ingested = 0, head_fingerprint = %s,
       rows_loaded = 0, rows_skipped = 0, rows_failed = 0,
       first_event_time = NULL, last_event_time = NULL
 WHERE id = %s
"""

SQL_FECHAR = """
UPDATE monitoring.ingestion_file
   SET bytes_ingested = %s, file_size_bytes = %s,
       status = %s, rotated = %s, finished_at = now(), error_message = NULL
 WHERE id = %s
"""

SQL_FALHAR = """
UPDATE monitoring.ingestion_file
   SET status = 'failed', error_message = %s, finished_at = now()
 WHERE id = %s
"""


def registrar_arquivo(conn, caminho: Path, file_path: str,
                      rotacionado: bool) -> Arquivo:
    """Garante a linha em ingestion_file e devolve de onde retomar a leitura."""
    tamanho = caminho.stat().st_size
    impressao = impressao_da_cabeca(caminho)

    with conn.cursor() as cur:
        cur.execute(SQL_UPSERT,
                    (caminho.name, file_path, tamanho, impressao))
        fid, deslocamento, impressao_antiga = cur.fetchone()

        motivo = ""
        if impressao_antiga and impressao_antiga != impressao:
            # Mesmo nome, conteúdo diferente: o arquivo foi recriado.
            motivo = "conteúdo do início mudou"
        elif deslocamento > tamanho:
            # Encolheu — truncado por fora, ou log_truncate_on_rotation.
            motivo = f"arquivo encolheu ({tamanho} < {deslocamento})"

        if motivo:
            cur.execute(SQL_RESETAR, (impressao, fid))
            deslocamento = 0

    conn.commit()
    return Arquivo(id=fid, caminho=caminho, file_path=file_path,
                   tamanho=tamanho, deslocamento=deslocamento,
                   rotacionado=rotacionado, reiniciado=motivo)


# --------------------------------------------------------------------------
# Coletor
# --------------------------------------------------------------------------

@dataclass
class Resultado:
    arquivo: str
    lidos: int = 0
    carregados: int = 0
    pulados: int = 0
    descartados: int = 0
    deslocamento: int = 0
    tamanho: int = 0
    status: str = "pending"
    segundos: float = 0.0
    erro: str = ""


class Coletor:
    def __init__(self, conn, lote: int = LOTE_PADRAO,
                 prefixo: str = PREFIXO_PADRAO, limite: int = 0,
                 verboso: bool = True) -> None:
        self.conn = conn
        self.lote = lote
        self.prefixo = prefixo.rstrip("/")
        self.limite = limite
        self.verboso = verboso
        self.estat: Counter = Counter()

    def _contar(self, chave: str, n: int = 1) -> None:
        self.estat[chave] += n

    # -- um arquivo ------------------------------------------------------

    def ingerir(self, caminho: Path, rotacionado: bool) -> Optional[Resultado]:
        alvo = f"{self.prefixo}/{caminho.name}"
        arq = registrar_arquivo(self.conn, caminho, alvo, rotacionado)
        res = Resultado(arquivo=caminho.name, tamanho=arq.tamanho,
                        deslocamento=arq.deslocamento)

        if arq.reiniciado:
            self._contar("arquivos_reiniciados")
            if self.verboso:
                print(f"  ! {caminho.name}: {arq.reiniciado} — relendo do zero")

        # Nada novo desde a última passada.
        if arq.deslocamento >= arq.tamanho and not arq.reiniciado:
            self._fechar(arq, arq.deslocamento, rotacionado)
            return None

        inicio = time.perf_counter()
        leitor = LeitorJsonlog(caminho, arq.deslocamento)
        try:
            for lote in leitor.lotes(self.lote, self.limite):
                if lote.eventos:
                    carregados, pulados, descartados = self._gravar(
                        arq.id, lote.eventos, lote.deslocamento, arq.tamanho,
                        rotacionado and lote.fim_do_arquivo)
                    res.lidos += len(lote.eventos)
                    res.carregados += carregados
                    res.pulados += pulados
                    res.descartados += descartados
                elif lote.fim_do_arquivo:
                    # Arquivo só cresceu em espaço em branco: ainda assim vale
                    # gravar o deslocamento, senão ele é relido toda passada.
                    self._fechar(arq, lote.deslocamento, rotacionado)
                res.deslocamento = lote.deslocamento
        except Exception as erro:                       # noqa: BLE001
            self.conn.rollback()
            with self.conn.cursor() as cur:
                cur.execute(SQL_FALHAR, (str(erro)[:2000], arq.id))
            self.conn.commit()
            res.erro = str(erro)
            res.status = "failed"
            self._contar("arquivos_com_erro")
            return res

        for chave, n in leitor.estat.items():
            self._contar(chave, n)

        res.segundos = time.perf_counter() - inicio
        res.status = self._status(arq, res.deslocamento, rotacionado)
        self._contar("arquivos_lidos")
        return res

    # -- um lote, numa transação ----------------------------------------

    def _gravar(self, file_id: int, eventos: List[dict], deslocamento: int,
                tamanho: int, encerrar: bool) -> Tuple[int, int, int]:
        estat: Counter = Counter()
        linhas = [linha_da_staging(e, file_id, estat) for e in eventos]
        linhas = [l for l in linhas if l is not None]
        descartados = len(eventos) - len(linhas)

        colunas = ", ".join(COLUNAS_STAGING)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                with cur.copy(
                    f"COPY monitoring.pg_log_event_staging ({colunas}) "
                    "FROM STDIN"
                ) as copia:
                    for linha in linhas:
                        copia.write_row(linha)

                cur.execute(
                    "SELECT loaded, skipped FROM monitoring.ingest_staging(%s)",
                    (file_id,))
                carregados, pulados = cur.fetchone()

                status = "completed" if encerrar else "partial"
                cur.execute(SQL_FECHAR,
                            (deslocamento, tamanho, status, encerrar, file_id))
                if descartados:
                    cur.execute(
                        "UPDATE monitoring.ingestion_file "
                        "SET rows_failed = rows_failed + %s WHERE id = %s",
                        (descartados, file_id))

        for chave, n in estat.items():
            self._contar(chave, n)
        return carregados, pulados, descartados

    def _status(self, arq: Arquivo, deslocamento: int, rotacionado: bool) -> str:
        if rotacionado and deslocamento >= arq.tamanho:
            return "completed"
        return "partial"

    def _fechar(self, arq: Arquivo, deslocamento: int, rotacionado: bool) -> None:
        status = self._status(arq, deslocamento, rotacionado)
        with self.conn.cursor() as cur:
            cur.execute(SQL_FECHAR, (deslocamento, arq.tamanho, status,
                                     rotacionado, arq.id))
        self.conn.commit()

    # -- uma passada sobre a pasta ---------------------------------------

    def rodada(self, arquivos: Sequence[Path]) -> List[Resultado]:
        # O Postgres nomeia os arquivos por timestamp, então o último em ordem
        # alfabética é o que está aberto agora. Todos os outros já rotacionaram
        # e não crescem mais.
        atual = arquivos[-1] if arquivos else None
        saida: List[Resultado] = []
        for caminho in arquivos:
            res = self.ingerir(caminho, rotacionado=(caminho != atual))
            if res is not None:
                saida.append(res)
                if self.verboso:
                    self._imprimir(res)
        return saida

    def _imprimir(self, res: Resultado) -> None:
        if res.erro:
            print(f"  x {res.arquivo}: {res.erro}")
            return
        taxa = res.lidos / res.segundos if res.segundos > 0.001 else 0
        extra = f" {res.pulados:>7,} já estavam" if res.pulados else ""
        falhas = f" {res.descartados} descartados" if res.descartados else ""
        print(f"  + {res.arquivo}  {res.carregados:>7,} novos{extra}{falhas}"
              f"  [{res.status}, {taxa:,.0f} ev/s]")


# --------------------------------------------------------------------------
# Relatórios
# --------------------------------------------------------------------------

SQL_ESTADO = """
SELECT f.file_name,
       f.status,
       f.rotated,
       f.rows_loaded,
       f.rows_skipped,
       f.bytes_ingested,
       f.file_size_bytes,
       f.first_event_time,
       f.last_event_time
  FROM monitoring.ingestion_file f
 ORDER BY f.file_name
"""

SQL_RESUMO = """
SELECT (SELECT count(*) FROM monitoring.pg_log_event),
       (SELECT count(*) FROM monitoring.ingestion_file),
       (SELECT count(*) FROM pg_partition_tree('monitoring.pg_log_event')
         WHERE isleaf),
       (SELECT pg_size_pretty(coalesce(sum(pg_total_relation_size(relid)), 0))
          FROM pg_partition_tree('monitoring.pg_log_event') WHERE isleaf)
"""


def mostrar_estado(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SQL_ESTADO)
        linhas = cur.fetchall()

    if not linhas:
        print("Nenhum arquivo registrado.")
    else:
        print(f"{'arquivo':<38} {'status':<10} {'novos':>9} {'repetidos':>10} "
              f"{'lido':>6}")
        print("-" * 78)
        for (nome, status, rot, carregados, pulados,
             lidos, total, _pri, _ult) in linhas:
            pct = (100.0 * lidos / total) if total else 100.0
            print(f"{nome:<38} {status:<10} {carregados:>9,} {pulados:>10,} "
                  f"{pct:>5.1f}%")

    with conn.cursor() as cur:
        cur.execute(SQL_RESUMO)
        eventos, arquivos, particoes, tamanho = cur.fetchone()
    print()
    print(f"{eventos:,} eventos em {particoes} partição(ões), "
          f"{arquivos} arquivo(s), {tamanho} em disco")


def mostrar_estatisticas(estat: Counter) -> None:
    tipos = {k[5:]: v for k, v in estat.items() if k.startswith("tipo_")}
    outros = {k: v for k, v in estat.items() if not k.startswith("tipo_")}
    if tipos:
        total = sum(tipos.values())
        print("\nPor record_type:")
        for nome, n in sorted(tipos.items(), key=lambda x: -x[1]):
            print(f"  {nome:<12} {n:>9,}  ({100.0 * n / total:>4.1f}%)")
    ruido = {k: v for k, v in outros.items() if not k.startswith("arquivos_")}
    if ruido:
        print("\nObservações:")
        for nome, n in sorted(ruido.items()):
            print(f"  {nome:<22} {n:>9,}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def descobrir_arquivos(pasta: Path, padrao: str) -> List[Path]:
    return sorted(p for p in pasta.glob(padrao) if p.is_file())


def main(argv: Optional[Sequence[str]] = None) -> int:
    raiz = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Alimenta o banco de monitoramento com o jsonlog do Postgres.")
    ap.add_argument("--logs", type=Path, default=raiz / "logs",
                    help="pasta com os arquivos de log (padrão: ./logs)")
    ap.add_argument("--padrao", default="*.json",
                    help="glob dos arquivos a ler (padrão: *.json)")
    ap.add_argument("--dsn", default=DSN_PADRAO,
                    help="conexão com o banco de monitoramento "
                         "(ou a variável BROKK_DSN)")
    ap.add_argument("--prefixo", default=PREFIXO_PADRAO,
                    help="caminho da pasta de logs como o container do banco a "
                         f"enxerga (padrão: {PREFIXO_PADRAO})")
    ap.add_argument("--lote", type=int, default=LOTE_PADRAO,
                    help=f"eventos por transação (padrão: {LOTE_PADRAO})")
    ap.add_argument("--limite", type=int, default=0,
                    help="lê no máximo N eventos por arquivo (para testes)")
    ap.add_argument("--seguir", action="store_true",
                    help="não termina: relê a pasta a cada intervalo")
    ap.add_argument("--intervalo", type=float, default=10.0,
                    help="segundos entre passadas com --seguir (padrão: 10)")
    ap.add_argument("--reingerir", action="store_true",
                    help="zera os deslocamentos e relê tudo (o índice único "
                         "descarta o que já estiver no banco)")
    ap.add_argument("--status", action="store_true",
                    help="só mostra o estado da ingestão e sai")
    ap.add_argument("--silencioso", action="store_true",
                    help="não imprime linha por arquivo")
    args = ap.parse_args(argv)

    try:
        conn = psycopg.connect(args.dsn)
    except psycopg.OperationalError as erro:
        print(f"não consegui conectar em {args.dsn}\n  {erro}", file=sys.stderr)
        print("o banco sobe com: docker compose up -d monitoring",
              file=sys.stderr)
        return 2

    with conn:
        if args.status:
            mostrar_estado(conn)
            return 0

        if not args.logs.is_dir():
            print(f"pasta não encontrada: {args.logs}", file=sys.stderr)
            return 2

        if args.reingerir:
            with conn.cursor() as cur:
                cur.execute("UPDATE monitoring.ingestion_file "
                            "SET bytes_ingested = 0, status = 'pending'")
                afetados = cur.rowcount
            conn.commit()
            print(f"deslocamento zerado em {afetados} arquivo(s)")

        coletor = Coletor(conn, lote=args.lote, prefixo=args.prefixo,
                          limite=args.limite, verboso=not args.silencioso)

        passada = 0
        try:
            while True:
                passada += 1
                arquivos = descobrir_arquivos(args.logs, args.padrao)
                if not arquivos:
                    print(f"nenhum arquivo em {args.logs}/{args.padrao}")
                elif not args.silencioso and args.seguir:
                    print(f"[passada {passada}] {len(arquivos)} arquivo(s)")

                inicio = time.perf_counter()
                resultados = coletor.rodada(arquivos)
                gasto = time.perf_counter() - inicio

                novos = sum(r.carregados for r in resultados)
                repetidos = sum(r.pulados for r in resultados)
                if resultados and not args.silencioso:
                    print(f"  = {novos:,} novos, {repetidos:,} já estavam, "
                          f"{gasto:.1f}s")
                elif args.seguir and not args.silencioso:
                    print("  = nada novo")

                if not args.seguir:
                    break
                time.sleep(args.intervalo)
        except KeyboardInterrupt:
            print("\ninterrompido — o deslocamento do último lote está gravado")

        if not args.silencioso:
            mostrar_estatisticas(coletor.estat)
            print()
            mostrar_estado(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
