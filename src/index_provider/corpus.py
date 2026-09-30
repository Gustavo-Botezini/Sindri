#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""O corpus de queries: seleção, normalização e os parâmetros do replay.

Duas responsabilidades que a bancada e o clusterizador precisam ter em comum,
e que por isso não moram em nenhum dos dois:

1. **quais queries entram** — só leitura, com execuções suficientes para não
   ser acidente;
2. **como o texto é apresentado ao modelo de embedding** — e o porquê está
   logo abaixo.

Sobre a normalização. O trabalho que inspirou este modo passou o texto
integralmente, sem normalização sintática. Aqui isso não serve, e a razão é o
corpus: a mesma query TPC-H aparece em duas grafias, 11 em minúsculo vindas do
benchmark ``tpch`` e as maiúsculas do ``templated``. Sem colapsar espaço e
caixa, o HDBSCAN tem material para agrupar por estilo de formatação em vez de
por forma da query.

E essa duplicata é o **teste de sanidade do clustering**: se ele presta, as
duas grafias da mesma query caem no mesmo cluster. ``agrupar`` avisa quando
não caem. ``--sem-normalizar`` existe para poder rodar o experimento nas duas
condições e comparar.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .banco import linhas_como_dicts

NORMALIZACOES = ("bruto", "espaco", "espaco+caixa")
NORMALIZACAO_PADRAO = "espaco+caixa"

# Abaixo disto a query é acidente de execução, não carga. O corpus tem dezenas
# de queries com 1 ou 2 execuções (DDL do benchmark, sondagens do pgAdmin) e
# elas só somariam ruído ao espaço vetorial.
MINIMO_EXECUCOES = 5


# --------------------------------------------------------------------------
# Seleção
# --------------------------------------------------------------------------
# Dois cortes, por dois motivos diferentes.
#
# **Só leitura.** A bancada reexecuta cada query contra o banco observado, e
# não se replica INSERT/UPDATE/DELETE numa base que é o objeto do
# experimento. O teste é começar com SELECT/WITH e não conter verbo de
# escrita em lugar nenhum. A segunda metade é conservadora de propósito — um
# falso positivo custa uma query a menos no corpus; um falso negativo custa um
# DELETE rodando três vezes contra o tpch. Medido: derruba 1 das 97 queries de
# leitura, uma introspecção que cita CREATE dentro de um literal.
#
# **Sem catálogo.** Medido no corpus: das 69 queries que passam pelo corte de
# leitura e de execuções, 30 são introspecção do pgAdmin e do driver JDBC
# sobre pg_class, pg_attribute e afins — três famílias de nove, cada uma
# variando só nos literais. Elas não são carga: ninguém indexa pg_catalog (o
# próprio PROMPT_SISTEMA já proíbe recomendar isso), e no espaço vetorial elas
# formariam três clusters densos e inúteis que abafariam a carga analítica de
# verdade. Fora também as que não tocam relação nenhuma (SELECT version(),
# current_schema()).
SQL_CORPUS = """
SELECT query_ref, query_text, query_id, execucoes,
       round(duracao_total_ms::numeric)     AS duracao_total_ms,
       round(duracao_media_ms::numeric, 2)  AS duracao_media_ms,
       relacoes, seq_scans,
       round(seq_scan_descartadas::numeric) AS seq_scan_descartadas,
       sorts_em_disco,
       exemplo_event_id, exemplo_event_time
  FROM monitoring.query_resumo
 WHERE database_name = %s
   AND execucoes >= %s
   AND (%s OR (
            upper(btrim(query_text)) ~ '^(SELECT|WITH)\\M'
        AND query_text !~* '\\m(insert|update|delete|truncate|drop|alter|create)\\M'
       ))
   AND (%s OR (
            cardinality(relacoes) > 0
        AND NOT EXISTS (SELECT 1 FROM unnest(relacoes) r
                         WHERE left(r, 3) = 'pg_')
       ))
   -- Fora o que a bancada já tentou reexecutar e não conseguiu. LEFT JOIN e
   -- não NOT IN: quem nunca passou por `conferir` continua entrando, então
   -- este filtro nunca tira nada que não tenha sido reprovado explicitamente.
   AND (%s OR NOT EXISTS (SELECT 1 FROM monitoring.query_replay p
                           WHERE p.query_ref = query_resumo.query_ref
                             AND p.replicavel IS FALSE))
 ORDER BY duracao_total_ms DESC
"""

# Os valores dos $n. Vêm do plano mais lento da query, que o resumo já aponta
# em exemplo_event_id/exemplo_event_time — o par é obrigatório porque
# plan_execution é particionada por dia.
SQL_PARAMETROS = """
SELECT r.query_ref, x.parametros
  FROM monitoring.query_resumo r
  JOIN monitoring.plan_execution x
    ON x.event_id   = r.exemplo_event_id
   AND x.event_time = r.exemplo_event_time
 WHERE r.query_ref = ANY (%s)
   AND x.parametros IS NOT NULL AND x.parametros <> ''
"""


@dataclass
class Query:
    """Uma query do corpus, já normalizada e pronta para embedar."""
    ref: int
    texto: str
    texto_norm: str
    texto_hash: str
    execucoes: int
    duracao_total_ms: float
    duracao_media_ms: float
    relacoes: List[str] = field(default_factory=list)
    seq_scans: int = 0
    seq_scan_descartadas: float = 0.0
    sorts_em_disco: int = 0
    # Preenchido sob demanda por anexar_parametros(); [] quando a query não
    # tem $n, None quando tem mas o corpus não guardou os valores.
    parametros: Optional[List[str]] = None

    @property
    def parametrizada(self) -> bool:
        return bool(re.search(r"\$\d+", self.texto))


# --------------------------------------------------------------------------
# Normalização
# --------------------------------------------------------------------------

def normalizar(texto: str, modo: str = NORMALIZACAO_PADRAO) -> str:
    """Aplica o modo de normalização escolhido.

    'espaco+caixa' baixa a caixa do texto inteiro, literais inclusive
    ('PROMO%' vira 'promo%'). É intencional: o que se quer medir é a forma da
    query, e o valor do literal é justamente o que muda entre duas execuções
    da mesma consulta.
    """
    if modo not in NORMALIZACOES:
        raise ValueError(f"normalização desconhecida: {modo}")
    if modo == "bruto":
        return texto
    limpo = re.sub(r"\s+", " ", texto).strip()
    return limpo.lower() if modo == "espaco+caixa" else limpo


def hash_texto(texto: str) -> str:
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------

def carregar(conn, banco: str,
             normalizacao: str = NORMALIZACAO_PADRAO,
             minimo_execucoes: int = MINIMO_EXECUCOES,
             incluir_escrita: bool = False,
             incluir_catalogo: bool = False,
             incluir_irreplicaveis: bool = False) -> List[Query]:
    """As queries do corpus, já normalizadas."""
    with conn.cursor() as cur:
        cur.execute(SQL_CORPUS, (banco, minimo_execucoes, incluir_escrita,
                                 incluir_catalogo, incluir_irreplicaveis))
        linhas = linhas_como_dicts(cur)

    queries = []
    for l in linhas:
        norm = normalizar(l["query_text"], normalizacao)
        queries.append(Query(
            ref=l["query_ref"],
            texto=l["query_text"],
            texto_norm=norm,
            texto_hash=hash_texto(norm),
            execucoes=l["execucoes"],
            duracao_total_ms=float(l["duracao_total_ms"] or 0),
            duracao_media_ms=float(l["duracao_media_ms"] or 0),
            relacoes=list(l["relacoes"] or []),
            seq_scans=int(l["seq_scans"] or 0),
            seq_scan_descartadas=float(l["seq_scan_descartadas"] or 0),
            sorts_em_disco=int(l["sorts_em_disco"] or 0),
        ))
    return queries


# --------------------------------------------------------------------------
# Os parâmetros do replay
# --------------------------------------------------------------------------

def parse_parametros(texto: str) -> List[str]:
    """Desmonta o "Query Parameters" do auto_explain em lista ordenada.

    O formato é ``$1 = 'valor', $2 = 'outro'``, com aspas simples duplicadas
    dentro do literal. Não dá para quebrar por vírgula: o valor pode conter
    uma — e no corpus atual há literais de data e de lista que contêm.
    """
    valores: Dict[int, str] = {}
    i, n = 0, len(texto)
    while i < n:
        m = re.compile(r"\$(\d+)\s*=\s*").search(texto, i)
        if not m:
            break
        pos = int(m.group(1))
        i = m.end()
        if i < n and texto[i] == "'":
            i += 1
            pedacos = []
            while i < n:
                if texto[i] == "'":
                    # '' é uma aspa escapada, não o fim do literal.
                    if i + 1 < n and texto[i + 1] == "'":
                        pedacos.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                pedacos.append(texto[i])
                i += 1
            valores[pos] = "".join(pedacos)
        else:
            j = texto.find(",", i)
            bruto = (texto[i:] if j < 0 else texto[i:j]).strip()
            # NULL sem aspas é NULL de verdade; qualquer outra coisa é o texto.
            valores[pos] = None if bruto.upper() == "NULL" else bruto
            i = n if j < 0 else j + 1
    return [valores[k] for k in sorted(valores)]


_RE_PARAM = re.compile(r"\$(\d+)")
_RE_DOLAR = re.compile(r"\$([A-Za-z_]\w*)?\$")


def substituir_parametros(texto: str, literais: Sequence[str]) -> str:
    """Troca ``$n`` pelos literais já citados, respeitando o que não é código.

    É o caminho alternativo de quando o ``PREPARE`` não consegue inferir o
    tipo de um parâmetro. Trocar com ``str.replace`` quebraria em três
    lugares que o corpus tem de verdade: ``$`` dentro de literal de texto,
    ``--`` e ``/* */`` com ``$n`` no meio, e corpo de função entre ``$$``.
    Daí o varredor: ele copia o que não é código e só substitui fora disso.

    `literais` vem pronto de psycopg.sql.Literal — a citação e o escape são
    problema dele, não deste varredor.
    """
    saida: List[str] = []
    i, n = 0, len(texto)
    while i < n:
        c = texto[i]

        if c == "'":                                   # literal de texto
            j = i + 1
            while j < n:
                if texto[j] == "'":
                    if j + 1 < n and texto[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            saida.append(texto[i:j])
            i = j
            continue

        if c == '"':                                   # identificador citado
            j = texto.find('"', i + 1)
            j = n if j < 0 else j + 1
            saida.append(texto[i:j])
            i = j
            continue

        if texto.startswith("--", i):                  # comentário de linha
            j = texto.find("\n", i)
            j = n if j < 0 else j
            saida.append(texto[i:j])
            i = j
            continue

        if texto.startswith("/*", i):                  # comentário de bloco
            j = texto.find("*/", i + 2)
            j = n if j < 0 else j + 2
            saida.append(texto[i:j])
            i = j
            continue

        if c == "$":
            m = _RE_PARAM.match(texto, i)
            if m:
                pos = int(m.group(1))
                if 1 <= pos <= len(literais):
                    saida.append(literais[pos - 1])
                    i = m.end()
                    continue
            d = _RE_DOLAR.match(texto, i)
            if d:                                      # literal $tag$...$tag$
                fim = texto.find(d.group(0), d.end())
                j = n if fim < 0 else fim + len(d.group(0))
                saida.append(texto[i:j])
                i = j
                continue

        saida.append(c)
        i += 1
    return "".join(saida)


def anexar_parametros(conn, queries: Sequence[Query]) -> int:
    """Preenche Query.parametros. Devolve quantas ficaram sem.

    Uma query parametrizada sem parâmetros não pode ser reexecutada, e a
    bancada tem que dizer isso em vez de tentar e falhar no meio da medição.
    """
    refs = [q.ref for q in queries if q.parametrizada]
    if not refs:
        for q in queries:
            q.parametros = []
        return 0

    with conn.cursor() as cur:
        cur.execute(SQL_PARAMETROS, (refs,))
        por_ref = {r: p for r, p in cur.fetchall()}

    faltando = 0
    for q in queries:
        if not q.parametrizada:
            q.parametros = []
        elif q.ref in por_ref:
            q.parametros = parse_parametros(por_ref[q.ref])
        else:
            q.parametros = None
            faltando += 1
    return faltando
