#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Estimador de esquema do BrokkPG.

Lê todos os arquivos de log em ``logs/`` (o jsonlog do Postgres, com os planos
do auto_explain) e reconstrói o esquema do banco a partir do que a carga
revela — tabelas, colunas, tipos, índices, chaves primárias, chaves
estrangeiras e cardinalidade — sem abrir conexão com o banco.

Fontes de evidência, da mais forte para a mais fraca:

  1. **DDL capturado no log.** Com ``log_min_duration_statement=0`` os comandos
     do ``initdb/`` também entram no corpus, então um log que cubra a primeira
     subida do container traz o esquema literal (``CREATE TABLE``,
     ``ALTER TABLE ... ADD PRIMARY KEY``, ``CREATE INDEX``, ...).
  2. **Checagens de FK dos triggers RI.** O Postgres dispara
     ``SELECT 1 FROM ONLY "public"."x" WHERE "col" = $1 FOR KEY SHARE OF x``
     a cada escrita em tabela ligada por chave estrangeira. Elas provam que a
     FK existe e dizem de que lado a coluna está: coluna antes do ``$n`` é o
     lado referenciado (checagem no pai), ``$n`` antes da coluna é o lado que
     referencia (checagem nos filhos).
  3. **Os planos do auto_explain.** ``Relation Name``/``Alias`` dão as tabelas,
     ``Index Name`` + ``Index Cond`` dão os índices e suas colunas, e as
     expressões (``Filter``, ``Hash Cond``, ``Sort Key``, ...) dão colunas,
     tipos — pelos casts que o próprio planner imprime — e as junções que
     viram FK candidata. ``Plan Rows``/``Actual Rows`` dão a cardinalidade.
  4. **O texto SQL e os parâmetros vinculados.** A lista de colunas de um
     ``INSERT`` dá a ordem real das colunas; ``Query Parameters`` dá valores de
     verdade, que tipam o que o plano deixou sem cast.

Uso:

    python3 src/schema_estimator.py                    # relatório no stdout
    python3 src/schema_estimator.py --sql esquema.sql  # + DDL estimado
    python3 src/schema_estimator.py --json evidencias.json
    python3 src/schema_estimator.py --ignorar-ddl      # só o que a carga revela

O modo ``--ignorar-ddl`` existe para medir o próprio estimador: joga fora a
evidência de DDL e mostra até onde a carga sozinha chega. Quando o corpus tem
DDL, o relatório imprime uma conferência entre os dois caminhos.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

PREFIXOS_SISTEMA = ("pg_", "information_schema", "sql_")

# Funções do próprio Postgres que aparecem como Function Scan e não fazem
# parte do esquema da aplicação.
ROTINAS_EMBUTIDAS = {
    "generate_series", "generate_subscripts", "unnest", "each", "aclexplode",
    "json_each", "jsonb_each", "json_array_elements", "jsonb_array_elements",
    "string_to_table", "regexp_split_to_table", "regexp_matches",
}

# Tipos como o planner e o DDL os escrevem -> nome canônico.
TIPOS_CANONICOS = {
    "int2": "smallint", "smallint": "smallint",
    "int4": "integer", "int": "integer", "integer": "integer",
    "int8": "bigint", "bigint": "bigint",
    "serial": "integer", "bigserial": "bigint", "smallserial": "smallint",
    "numeric": "numeric", "decimal": "numeric",
    "float4": "real", "real": "real",
    "float8": "double precision", "double precision": "double precision",
    "bpchar": "character", "character": "character", "char": "character",
    "varchar": "character varying", "character varying": "character varying",
    "text": "text", "name": "name",
    "date": "date",
    "time": "time", "timetz": "time with time zone",
    "timestamp": "timestamp", "timestamp without time zone": "timestamp",
    "timestamptz": "timestamp with time zone",
    "timestamp with time zone": "timestamp with time zone",
    "interval": "interval",
    "bool": "boolean", "boolean": "boolean",
    "uuid": "uuid", "json": "json", "jsonb": "jsonb", "bytea": "bytea",
    "inet": "inet", "money": "money",
}

# Família do tipo: usada para generalizar votos conflitantes.
FAMILIA_DO_TIPO = {
    "smallint": "numerico", "integer": "numerico", "bigint": "numerico",
    "numeric": "numerico", "real": "numerico", "double precision": "numerico",
    "money": "numerico",
    "character": "texto", "character varying": "texto", "text": "texto",
    "name": "texto",
    "date": "tempo", "time": "tempo", "time with time zone": "tempo",
    "timestamp": "tempo", "timestamp with time zone": "tempo",
    "interval": "tempo",
    "boolean": "logico",
}

# Dentro de uma família, quanto maior o índice mais geral é o tipo. Um voto em
# "numeric" derruba um voto em "integer": todo integer cabe num numeric, o
# contrário não.
GENERALIDADE = {
    "smallint": 0, "integer": 1, "bigint": 2, "real": 3,
    "double precision": 4, "numeric": 5,
    "character": 0, "name": 1, "character varying": 2, "text": 3,
    "date": 0, "time": 0, "timestamp": 1, "timestamp with time zone": 2,
}

# Peso de cada tipo de evidência (0 a 1); vira a "confiança" do relatório.
PESO = {
    "ddl": 1.00,          # CREATE TABLE literal
    "cast": 0.90,         # (col = 'x'::bpchar) — anotação do próprio planner
    "array": 0.85,        # (col = ANY ('{...}'::name[]))
    "parametro": 0.70,    # (col = $1) com $1 = '1996-10-09' em Query Parameters
    "literal_sql": 0.65,  # INSERT ... VALUES ('O', ...)
    "numero": 0.60,       # (col >= 15) — literal numérico cru no plano
    "juncao": 0.55,       # tipo herdado do outro lado de (a.x = b.y)
    "aritmetica": 0.50,   # ('1'::numeric - col)
    "cast_saida": 0.35,   # (col)::double precision — não é o tipo, mas é a família
    "like": 0.30,         # (col ~~ '%x%')
}

# Palavras que aparecem em expressão de plano e não são nome de coluna.
PALAVRAS_NAO_COLUNA: Set[str] = {
    "and", "or", "not", "null", "true", "false", "case", "when", "then",
    "else", "end", "any", "all", "some", "in", "exists", "is", "distinct",
    "asc", "desc", "nulls", "first", "last", "initplan", "subplan", "hashed",
    "array", "row", "collate", "using", "from", "for", "cast", "as", "on",
    "select", "where", "group", "order", "by", "having", "limit", "offset",
    "only", "key", "share", "operator", "returning", "values", "set", "col",
    # campos de EXTRACT()/date_part()
    "year", "month", "day", "hour", "minute", "second", "epoch", "quarter",
    "week", "doy", "dow", "isoyear", "millennium", "century", "decade",
    # palavras que sobram de nomes de tipo
    "precision", "varying", "zone", "without", "with", "time",
}
PALAVRAS_NAO_COLUNA.update(TIPOS_CANONICOS)

# Campos de expressão dos planos.
CAMPOS_EXPRESSAO = (
    "Filter", "Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond",
    "Join Filter", "TID Cond", "Sort Key", "Group Key", "Hash Key",
    "Presorted Key", "Cache Key", "Order By", "Conflict Filter",
)
# Destes a gente também tira relacionamento (junção entre duas tabelas).
CAMPOS_DE_JUNCAO = {"Hash Cond", "Merge Cond", "Join Filter", "Index Cond",
                    "Recheck Cond", "Filter"}

# --------------------------------------------------------------------------
# Expressões regulares
# --------------------------------------------------------------------------

# Identificador como o planner imprime: minúsculo, ou entre aspas.
_ID = r'(?:"[^"]+"|[a-z_][a-z0-9_$]*)'
_LIT = r"'(?:[^']|'')*'"
# Nome de tipo num cast: minúsculo, até 4 palavras, com [] opcional.
_TIPO = r'(?:"[^"]+"|[a-z][a-z0-9_]*(?:\s+[a-z][a-z0-9_]*){0,3})(?:\[\])?'

# O `(?![\w$])` no fim é o que impede o regex de retroceder dentro do próprio
# identificador: sem ele, `concat(` casaria como a coluna `conca`.
_FIM_ID = r'(?![\w$])(?!\s*\()'

RE_REF = re.compile(rf'(?<![\w.$"])({_ID})(?:\.({_ID}))?{_FIM_ID}')

RE_COMPARACAO = re.compile(
    rf'(?:\(\s*)?(?<![\w.$"])(?P<lref>{_ID}(?:\.{_ID})?){_FIM_ID}\s*'
    rf'(?:\)\s*::\s*(?P<lcast>{_TIPO}))?'
    rf'\s*(?P<op>=|<>|!=|>=|<=|>|<|~~\*?|!~~\*?|\+|\*|/|-)\s*'
    rf'(?:'
    rf"(?P<lit>{_LIT})(?:\s*::\s*(?P<litcast>{_TIPO}))?"
    rf'|\(\s*(?P<plit>{_LIT}|\$\d+)\s*\)\s*::\s*(?P<pcast>{_TIPO})'
    rf'|(?P<param>\$\d+)'
    rf'|ANY\s*\(\s*{_LIT}\s*::\s*(?P<anycast>{_TIPO})\s*\)'
    rf'|(?P<num>[-+]?\d+(?:\.\d+)?)(?![\w.])'
    rf'|(?P<rref>{_ID}(?:\.{_ID})?){_FIM_ID}'
    rf')'
)

RE_MASCARA_LIT = re.compile(_LIT)
RE_MASCARA_CAST = re.compile(rf'::\s*{_TIPO}')

RE_PARAMETROS = re.compile(rf"\$(\d+)\s*=\s*({_LIT}|NULL)")

# Checagem de FK disparada por trigger RI.
RE_CHECAGEM_RI = re.compile(
    r'SELECT\s+1\s+FROM\s+ONLY\s+(?:"?(?P<esquema>[^".\s]+)"?\.)?"?(?P<tabela>[^".\s]+)"?'
    r'\s+\w+\s+WHERE\s+(?P<cond>.+?)\s+FOR\s+KEY\s+SHARE',
    re.I | re.S,
)
RE_RI_PAI = re.compile(r'"?([a-z_][a-z0-9_$]*)"?\s*OPERATOR\([^)]*\)\s*\$\d+', re.I)
RE_RI_FILHO = re.compile(r'\$\d+\s*OPERATOR\([^)]*\)\s*"?([a-z_][a-z0-9_$]*)"?', re.I)

RE_INSERT = re.compile(
    r'INSERT\s+INTO\s+(?:"?(?P<esquema>[^".\s(]+)"?\.)?"?(?P<tabela>[a-z_][a-z0-9_$]*)"?\s*'
    r'\((?P<colunas>[^()]*)\)\s*(?:VALUES\s*\((?P<valores>[^;]*?)\)\s*'
    r'(?:ON\s+CONFLICT|RETURNING|;|$))?',
    re.I | re.S,
)
RE_UPDATE_SET = re.compile(
    r'UPDATE\s+(?:ONLY\s+)?(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[a-z_][a-z0-9_$]*)"?\s+'
    r'SET\s+(?P<atribuicoes>.+?)(?:\s+WHERE\s|\s+RETURNING\s|;|$)',
    re.I | re.S,
)
RE_ATRIBUICAO = re.compile(
    rf'([a-z_][a-z0-9_$]*)\s*=\s*(?:(\$\d+)|({_LIT})|([-+]?\d+(?:\.\d+)?))\s*(?:,|$)',
    re.I,
)
RE_ALVO_ESCRITA = re.compile(
    r'\b(?P<op>INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?'
    r'(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[a-z_][a-z0-9_$]*)"?',
    re.I,
)
# O `(?!\s*[)(])` descarta o FROM que não é cláusula: `EXTRACT(year FROM col)`,
# `TRIM(... FROM x)` e chamadas de função — ao custo de perder o último item de
# uma subconsulta (`... FROM lineitem)`), que costuma aparecer em outro lugar.
RE_FROM_JOIN = re.compile(
    r'\b(?:FROM|JOIN)\s+(?:ONLY\s+)?(?:"?(?P<esquema>[^".\s(]+)"?\.)?'
    r'"?(?P<tabela>[a-z_][a-z0-9_$]*)"?(?![\w$])(?!\s*[()])',
    re.I,
)
# Nomes que aparecem depois de FROM sem serem relação.
NAO_SAO_RELACAO = {"stdin", "stdout", "program", "dual"}
RE_CTE = re.compile(
    r'(?:\bWITH\b|,)\s*"?([a-z_][a-z0-9_$]*)"?\s+AS\s*(?:NOT\s+)?(?:MATERIALIZED\s*)?\(',
    re.I,
)

# DDL
_ID_DDL = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
RE_DDL_CREATE_TABLE = re.compile(
    r'CREATE\s+(?:UNLOGGED\s+|TEMP\w*\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[A-Za-z_][A-Za-z0-9_$]*)"?\s*\(',
    re.I,
)
RE_DDL_CREATE_VIEW = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?(?P<mat>MATERIALIZED\s+)?VIEW\s+'
    r'(?:IF\s+NOT\s+EXISTS\s+)?(?:"?[^".\s(]+"?\.)?"?(?P<nome>[A-Za-z_][A-Za-z0-9_$]*)"?',
    re.I,
)
RE_DDL_CREATE_ROTINA = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?(?P<especie>FUNCTION|PROCEDURE)\s+'
    r'(?:"?[^".\s(]+"?\.)?"?(?P<nome>[A-Za-z_][A-Za-z0-9_$]*)"?',
    re.I,
)
RE_DDL_INDICE = re.compile(
    r'CREATE\s+(?P<unico>UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:"?(?P<indice>[A-Za-z_][A-Za-z0-9_$]*)"?\s+)?ON\s+(?:ONLY\s+)?'
    r'(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[A-Za-z_][A-Za-z0-9_$]*)"?\s*'
    r'(?:USING\s+\w+\s*)?\((?P<colunas>[^;]*?)\)\s*(?:WHERE|WITH|TABLESPACE|;|$)',
    re.I | re.S,
)
RE_DDL_PK = re.compile(
    r'ALTER\s+TABLE\s+(?:ONLY\s+)?(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[A-Za-z_][A-Za-z0-9_$]*)"?\s+'
    r'ADD\s+(?:CONSTRAINT\s+"?[A-Za-z_][A-Za-z0-9_$]*"?\s+)?PRIMARY\s+KEY\s*\((?P<colunas>[^)]*)\)',
    re.I,
)
RE_DDL_UNIQUE = re.compile(
    r'ALTER\s+TABLE\s+(?:ONLY\s+)?(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[A-Za-z_][A-Za-z0-9_$]*)"?\s+'
    r'ADD\s+(?:CONSTRAINT\s+"?[A-Za-z_][A-Za-z0-9_$]*"?\s+)?UNIQUE\s*\((?P<colunas>[^)]*)\)',
    re.I,
)
RE_DDL_FK = re.compile(
    r'ALTER\s+TABLE\s+(?:ONLY\s+)?(?:"?[^".\s(]+"?\.)?"?(?P<tabela>[A-Za-z_][A-Za-z0-9_$]*)"?\s+'
    r'ADD\s+(?:CONSTRAINT\s+"?[A-Za-z_][A-Za-z0-9_$]*"?\s+)?FOREIGN\s+KEY\s*\((?P<colunas>[^)]*)\)\s*'
    r'REFERENCES\s+(?:"?[^".\s(]+"?\.)?"?(?P<alvo>[A-Za-z_][A-Za-z0-9_$]*)"?\s*'
    r'(?:\((?P<alvo_colunas>[^)]*)\))?',
    re.I,
)
RE_COLUNA_DDL = re.compile(rf'^(?P<col>{_ID_DDL})\s+(?P<resto>.+)$', re.S)
RE_MODIFICADOR = re.compile(
    r'\b(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|UNIQUE|CHECK|'
    r'GENERATED|COLLATE|CONSTRAINT|DEFERRABLE)\b',
    re.I,
)
RE_DDL_DEFAULT = re.compile(
    r'\bDEFAULT\s+(?P<valor>.+?)'
    r'(?=\s+(?:NOT\s+NULL|NULL|PRIMARY\s+KEY|REFERENCES|UNIQUE|CHECK|COLLATE|'
    r'GENERATED|CONSTRAINT|DEFERRABLE)\b|\s*$)',
    re.I | re.S,
)
RE_INLINE_REFERENCES = re.compile(
    r'REFERENCES\s+(?:"?[^".\s(]+"?\.)?"?(?P<alvo>[A-Za-z_][A-Za-z0-9_$]*)"?\s*'
    r'(?:\((?P<alvo_colunas>[^)]*)\))?',
    re.I,
)
RE_ITEM_TABELA_IGNORADO = re.compile(
    r'^\s*(?:CONSTRAINT|PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|EXCLUDE|LIKE)\b', re.I)

# Mensagem de statement no jsonlog (com ou sem o prefixo de duração).
RE_STATEMENT = re.compile(
    r'^(?:duration:\s*[\d.]+\s*ms\s+)?'
    r'(?:statement|execute\s+[^:]*|parse\s+[^:]*|bind\s+[^:]*)\s*:\s*(?P<sql>.*)$',
    re.S,
)

# Valores vinculados: só o formato basta para tipar.
RE_VAL_INTEIRO = re.compile(r'^-?\d{1,18}$')
RE_VAL_DECIMAL = re.compile(r'^-?\d+\.\d+$')
RE_VAL_DATA = re.compile(r'^\d{4}-\d{2}-\d{2}$')
RE_VAL_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?')


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def _desaspas(nome: Optional[str]) -> Optional[str]:
    if nome is None:
        return None
    return nome[1:-1] if len(nome) > 1 and nome[0] == '"' else nome


def e_sistema(nome: str) -> bool:
    """Relação do catálogo ou de extensão de monitoramento."""
    n = nome.lower()
    return n.startswith(PREFIXOS_SISTEMA)


def canonizar_tipo(bruto: Optional[str]) -> Optional[str]:
    """Normaliza o nome de um tipo, preservando (precisão, escala) do DDL."""
    if not bruto:
        return None
    t = " ".join(bruto.split()).lower().rstrip(",")
    if t.startswith('"') and t.endswith('"'):
        t = t[1:-1]
    t = t.replace("[]", "")
    args = None
    m = re.match(r'^([a-z][a-z0-9_ ]*?)\s*\(([^)]*)\)$', t)
    if m:
        t, args = m.group(1).strip(), m.group(2).replace(" ", "")
    base = TIPOS_CANONICOS.get(t, t)
    if base in ("integer", "bigint", "smallint") and args:
        args = None  # serial/bigserial não têm argumento
    return f"{base}({args})" if args else base


def tipo_base(tipo: Optional[str]) -> Optional[str]:
    """"numeric(15,2)" -> "numeric"."""
    if not tipo:
        return None
    return tipo.split("(", 1)[0].strip()


def tipo_do_valor(valor: Optional[str]) -> Optional[str]:
    """Tipo provável de um valor vindo de ``Query Parameters``/literal SQL.

    Valor de texto não distingue ``character``, ``character varying`` e
    ``text`` — o driver manda tudo como string. Devolve o do meio, que é o
    palpite menos arriscado; quando o plano imprime um cast (``'R'::bpchar``)
    aquela evidência é mais forte e ganha desta.
    """
    if valor is None:
        return None
    v = valor.strip()
    if not v:
        return "text"
    if RE_VAL_INTEIRO.match(v):
        # '000001' é código, não número: driver nenhum escreve inteiro com
        # zero à esquerda, e o tipo por trás costuma ser char(n).
        if v.lstrip("-").startswith("0") and len(v.lstrip("-")) > 1:
            return "character"
        try:
            return "integer" if abs(int(v)) < 2_147_483_648 else "bigint"
        except ValueError:
            return "bigint"
    if RE_VAL_DECIMAL.match(v):
        return "numeric"
    if RE_VAL_DATA.match(v):
        return "date"
    if RE_VAL_TIMESTAMP.match(v):
        return "timestamp"
    if v.lower() in ("true", "false"):
        return "boolean"
    return "character varying"


def parse_parametros(texto: str) -> Dict[int, Optional[str]]:
    """``$1 = '61266', $2 = NULL`` -> {1: '61266', 2: None}."""
    if not texto or "$" not in texto:
        return {}
    saida: Dict[int, Optional[str]] = {}
    for numero, valor in RE_PARAMETROS.findall(texto):
        if valor.upper() == "NULL":
            saida[int(numero)] = None
        else:
            saida[int(numero)] = valor[1:-1].replace("''", "'")
    return saida


def partes_topo(texto: str, sep: str = ",") -> List[str]:
    """Divide por ``sep`` ignorando o que está dentro de parênteses/aspas."""
    partes, nivel, atual, aspas = [], 0, [], None
    for ch in texto:
        if aspas:
            atual.append(ch)
            if ch == aspas:
                aspas = None
            continue
        if ch in ("'", '"'):
            aspas = ch
            atual.append(ch)
            continue
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel -= 1
        if ch == sep and nivel == 0:
            partes.append("".join(atual))
            atual = []
        else:
            atual.append(ch)
    partes.append("".join(atual))
    return [p.strip() for p in partes if p.strip()]


def corpo_parenteses(texto: str, abre: int) -> Optional[str]:
    """Conteúdo entre o parêntese em ``abre`` e o que o fecha."""
    nivel = 0
    for i in range(abre, len(texto)):
        c = texto[i]
        if c == "(":
            nivel += 1
        elif c == ")":
            nivel -= 1
            if nivel == 0:
                return texto[abre + 1:i]
    return None


RE_ALIAS_DE_SAIDA = re.compile(rf'\bAS\s+({_ID_DDL})\s*$', re.I | re.S)
RE_REF_SIMPLES = re.compile(rf'^({_ID_DDL})(?:\.({_ID_DDL}))?\s*$')


def fatia_projecao(sql: str, inicio: int) -> str:
    """O trecho entre ``SELECT`` e o ``FROM`` do mesmo nível."""
    nivel, aspas, i = 0, None, inicio
    while i < len(sql):
        ch = sql[i]
        if aspas:
            if ch == aspas:
                aspas = None
            i += 1
            continue
        if ch in ("'", '"'):
            aspas = ch
        elif ch == "(":
            nivel += 1
        elif ch == ")":
            if nivel == 0:
                break
            nivel -= 1
        elif nivel == 0 and ch in "fF" and sql[i:i + 4].upper() == "FROM":
            antes = sql[i - 1] if i else " "
            depois = sql[i + 4] if i + 4 < len(sql) else " "
            if not (antes.isalnum() or antes == "_") and \
                    not (depois.isalnum() or depois == "_"):
                return sql[inicio:i]
        i += 1
    return sql[inicio:i]


def colunas_de_projecao(projecao: str) -> List[str]:
    """Nomes de saída de uma lista de SELECT: o alias, ou o nome da coluna."""
    saida = []
    for item in partes_topo(projecao):
        item = re.sub(r'^\s*(?:DISTINCT|ALL)\b', "", item, flags=re.I).strip()
        alias = RE_ALIAS_DE_SAIDA.search(item)
        if alias:
            nome = _desaspas(alias.group(1))
        else:
            simples = RE_REF_SIMPLES.match(item)
            if not simples:
                continue  # expressão sem apelido: o Postgres nomeia sozinho
            nome = _desaspas(simples.group(2) or simples.group(1))
        nome = (nome or "").lower()
        if nome and nome not in PALAVRAS_NAO_COLUNA:
            saida.append(nome)
    return saida


def fmt_num(n: Optional[float]) -> str:
    if n is None:
        return "-"
    return f"{int(n):,}".replace(",", ".")


# --------------------------------------------------------------------------
# Análise de uma expressão de plano (cacheada: o corpus repete muito)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExprAnalisada:
    refs: Tuple[Tuple[Optional[str], str], ...]           # (alias, coluna)
    tipos: Tuple[Tuple[Optional[str], str, str, str], ...]  # (alias, coluna, tipo, fonte)
    parametros: Tuple[Tuple[Optional[str], str, int], ...]  # (alias, coluna, $n)
    juncoes: Tuple[Tuple[Tuple[Optional[str], str], Tuple[Optional[str], str]], ...]
    esquerdas: Tuple[Tuple[Optional[str], str], ...]      # lados esquerdos, em ordem


def _mascarar(expr: str) -> str:
    """Some com literais e nomes de tipo, que não são colunas."""
    return RE_MASCARA_CAST.sub("::T", RE_MASCARA_LIT.sub("''", expr))


def _partir_ref(bruto: str) -> Optional[Tuple[Optional[str], str]]:
    partes = [p for p in re.split(r'\.(?=(?:[^"]*"[^"]*")*[^"]*$)', bruto) if p]
    if len(partes) == 1:
        alias, col = None, _desaspas(partes[0])
    else:
        alias, col = _desaspas(partes[-2]), _desaspas(partes[-1])
    if not col or col.lower() in PALAVRAS_NAO_COLUNA:
        return None
    if alias and alias.lower() in PALAVRAS_NAO_COLUNA:
        return None
    return alias, col


@lru_cache(maxsize=8192)
def analisar_expressao(expr: str) -> ExprAnalisada:
    refs: List[Tuple[Optional[str], str]] = []
    tipos: List[Tuple[Optional[str], str, str, str]] = []
    parametros: List[Tuple[Optional[str], str, int]] = []
    juncoes: List[Tuple[Tuple[Optional[str], str], Tuple[Optional[str], str]]] = []
    esquerdas: List[Tuple[Optional[str], str]] = []

    mascarada = _mascarar(expr)
    for m in RE_REF.finditer(mascarada):
        bruto = m.group(1) + ("." + m.group(2) if m.group(2) else "")
        ref = _partir_ref(bruto)
        if ref:
            refs.append(ref)

    for m in RE_COMPARACAO.finditer(expr):
        ref = _partir_ref(m.group("lref"))
        if ref is None:
            continue
        op = m.group("op")
        comparacao = op in ("=", "<>", "!=", ">=", "<=", ">", "<", "~~", "~~*", "!~~", "!~~*")
        if comparacao:
            esquerdas.append(ref)

        if m.group("lcast"):
            # (col)::tipo — a coluna NÃO é desse tipo, mas é da mesma família.
            alvo = canonizar_tipo(m.group("lcast"))
            familia = FAMILIA_DO_TIPO.get(tipo_base(alvo) or "")
            if familia == "numerico":
                tipos.append((ref[0], ref[1], "numeric", "cast_saida"))
            elif familia == "texto":
                tipos.append((ref[0], ref[1], "character varying", "cast_saida"))
            elif familia == "tempo":
                tipos.append((ref[0], ref[1], alvo or "timestamp", "cast_saida"))
            continue  # o lado direito tipa o cast, não a coluna

        if m.group("anycast"):
            tipo = canonizar_tipo(m.group("anycast"))
            if tipo:
                tipos.append((ref[0], ref[1], tipo, "array"))
        elif m.group("lit") is not None:
            tipo = canonizar_tipo(m.group("litcast"))
            if tipo:
                tipos.append((ref[0], ref[1], tipo,
                              "cast" if comparacao else "aritmetica"))
            elif op in ("~~", "~~*", "!~~", "!~~*"):
                tipos.append((ref[0], ref[1], "character varying", "like"))
        elif m.group("plit") is not None:
            tipo = canonizar_tipo(m.group("pcast"))
            if tipo:
                tipos.append((ref[0], ref[1], tipo,
                              "cast" if comparacao else "aritmetica"))
        elif m.group("param") is not None:
            if comparacao:
                parametros.append((ref[0], ref[1], int(m.group("param")[1:])))
        elif m.group("num") is not None:
            if comparacao:
                num = m.group("num")
                tipos.append((ref[0], ref[1],
                              "numeric" if "." in num else "integer", "numero"))
        elif m.group("rref") is not None:
            outra = _partir_ref(m.group("rref"))
            if outra and op == "=":
                juncoes.append((ref, outra))

    return ExprAnalisada(tuple(refs), tuple(tipos), tuple(parametros),
                         tuple(juncoes), tuple(esquerdas))


# --------------------------------------------------------------------------
# Estruturas do esquema estimado
# --------------------------------------------------------------------------

@dataclass
class Coluna:
    nome: str
    votos: Counter = field(default_factory=Counter)   # (tipo, fonte) -> ocorrências
    usos: int = 0
    tipo_ddl: Optional[str] = None
    tipo_serial: Optional[str] = None   # serial/bigserial declarado no DDL
    default_ddl: Optional[str] = None
    not_null: bool = False
    exemplos: List[str] = field(default_factory=list)
    tam_min: Optional[int] = None
    tam_max: Optional[int] = None

    def votar(self, tipo: Optional[str], fonte: str, n: int = 1) -> None:
        if tipo:
            self.votos[(tipo, fonte)] += n

    def exemplo(self, valor: Optional[str]) -> None:
        if valor is None:
            return
        tam = len(valor)
        self.tam_min = tam if self.tam_min is None else min(self.tam_min, tam)
        self.tam_max = tam if self.tam_max is None else max(self.tam_max, tam)
        if len(self.exemplos) < 6 and valor not in self.exemplos:
            self.exemplos.append(valor)

    def resolver(self, usar_ddl: bool = True) -> Tuple[Optional[str], float, str]:
        """Tipo escolhido, confiança (0..1) e a evidência que decidiu."""
        if usar_ddl and self.tipo_ddl:
            return self.tipo_ddl, 1.0, "ddl"
        melhor: Dict[str, float] = {}
        fonte_de: Dict[str, str] = {}
        ocorrencias: Counter = Counter()
        for (tipo, fonte), n in self.votos.items():
            if fonte == "ddl":
                continue
            peso = PESO.get(fonte, 0.2)
            if peso > melhor.get(tipo, 0.0):
                melhor[tipo] = peso
                fonte_de[tipo] = fonte
            ocorrencias[tipo] += n
        if not melhor:
            return None, 0.0, "-"
        topo = max(melhor.values())
        candidatos = [t for t, p in melhor.items() if p >= topo - 0.2]
        lider = max(candidatos, key=lambda t: (melhor[t], ocorrencias[t]))
        familia = FAMILIA_DO_TIPO.get(tipo_base(lider) or "")
        escolhido = lider
        if familia == "numerico":
            # integer x numeric: o mais geral acomoda os dois, e comparar uma
            # coluna numeric com literal inteiro é comum demais para ignorar.
            mesma = [t for t in candidatos
                     if FAMILIA_DO_TIPO.get(tipo_base(t) or "") == familia]
            escolhido = max(mesma, key=lambda t: (GENERALIDADE.get(tipo_base(t) or "", 0),
                                                  melhor[t], ocorrencias[t]))
        # Nas outras famílias generalizar erra mais do que acerta: uma coluna
        # date comparada com timestamp (o planner promove sozinho) continua date.
        return escolhido, round(melhor[escolhido], 2), fonte_de[escolhido]


@dataclass
class Indice:
    nome: str
    tabela: str
    colunas: List[str] = field(default_factory=list)
    unico: bool = False
    primaria: bool = False
    usos: int = 0
    fontes: Set[str] = field(default_factory=set)

    def registrar_colunas(self, novas: Sequence[str]) -> None:
        for c in novas:
            if c not in self.colunas:
                self.colunas.append(c)


@dataclass
class ChaveEstrangeira:
    tabela: str
    colunas: List[str]
    alvo: str
    alvo_colunas: List[str]
    confianca: float
    evidencias: List[str] = field(default_factory=list)
    observacoes: int = 0


@dataclass
class Tabela:
    nome: str
    especie: str = "relação"          # tabela | view | view materializada | rotina
    origens: Set[str] = field(default_factory=set)
    colunas: Dict[str, Coluna] = field(default_factory=dict)
    ordem_ddl: List[str] = field(default_factory=list)
    ordem_insert: List[str] = field(default_factory=list)
    indices: Dict[str, Indice] = field(default_factory=dict)
    pk: List[str] = field(default_factory=list)
    pk_fonte: str = "-"
    fks: List[ChaveEstrangeira] = field(default_factory=list)
    fks_ddl: List[Tuple[Tuple[str, ...], str, Tuple[str, ...]]] = field(default_factory=list)
    linhas_planner: Optional[int] = None
    linhas_lidas: Optional[int] = None
    operacoes: Counter = field(default_factory=Counter)
    nos: Counter = field(default_factory=Counter)
    ri_referenciada: Counter = field(default_factory=Counter)
    ri_referenciadora: Counter = field(default_factory=Counter)

    def coluna(self, nome: str) -> Coluna:
        col = self.colunas.get(nome)
        if col is None:
            col = self.colunas[nome] = Coluna(nome)
        return col

    def ordem(self) -> List[str]:
        """Ordem das colunas: DDL > lista de INSERT > mais usada primeiro."""
        base = self.ordem_ddl or self.ordem_insert
        vistas = [c for c in base if c in self.colunas]
        resto = sorted((c for c in self.colunas if c not in vistas),
                       key=lambda c: (-self.colunas[c].usos, c))
        return vistas + resto

    def linhas(self) -> Optional[int]:
        valores = [v for v in (self.linhas_planner, self.linhas_lidas) if v]
        return max(valores) if valores else None


# --------------------------------------------------------------------------
# Leitura do jsonlog
# --------------------------------------------------------------------------

def iter_registros(caminho: Path, estat: Counter, limite: int = 0) -> Iterator[dict]:
    """Percorre um arquivo de log campo a campo.

    Aceita tanto o jsonlog cru (um objeto por linha) quanto um arquivo já
    passado por um formatador — os dois convivem em ``logs/``.
    """
    decodificador = json.JSONDecoder()
    buffer = ""
    lidos = 0
    with open(caminho, encoding="utf-8", errors="replace") as fh:
        while True:
            pedaco = fh.read(1 << 20)
            if pedaco:
                buffer += pedaco
            pos = 0
            tamanho = len(buffer)
            while True:
                while pos < tamanho and buffer[pos] in " \t\r\n":
                    pos += 1
                if pos >= tamanho:
                    break
                try:
                    obj, fim = decodificador.raw_decode(buffer, pos)
                except ValueError:
                    if not pedaco or tamanho - pos > (8 << 20):
                        # registro truncado ou lixo: pula para o próximo objeto
                        proximo = buffer.find("\n{", pos + 1)
                        estat["registros_ilegiveis"] += 1
                        if proximo < 0:
                            pos = tamanho
                            break
                        pos = proximo + 1
                        continue
                    break
                pos = fim
                if not isinstance(obj, dict):
                    # arquivo que não é jsonlog (o .log em texto puro, por
                    # exemplo): "2026-09-06 ..." decodifica como o número 2026
                    estat["registros_ilegiveis"] += 1
                    continue
                lidos += 1
                yield obj
                if limite and lidos >= limite:
                    return
            buffer = buffer[pos:]
            if not pedaco:
                break


# --------------------------------------------------------------------------
# Estimador
# --------------------------------------------------------------------------

class Estimador:
    """Acumula evidência de todos os registros e devolve o esquema estimado."""

    def __init__(self, incluir_sistema: bool = False, amostras_por_texto: int = 3,
                 ignorar_ddl: bool = False, banco: Optional[str] = None):
        self.incluir_sistema = incluir_sistema
        self.amostras_por_texto = amostras_por_texto
        self.ignorar_ddl = ignorar_ddl
        self.banco = banco
        self.bancos: Counter = Counter()
        self.tabelas: Dict[str, Tabela] = {}
        self.esquemas: Counter = Counter()
        self.juncoes: Counter = Counter()          # ((t,c),(t,c)) -> n
        self.grupos_juncao: Counter = Counter()    # conjunto de pares vistos juntos
        self.pendentes_uso: Counter = Counter()    # coluna sem dono -> n
        self.pendentes_tipo: Counter = Counter()   # (coluna, tipo, fonte) -> n
        self.ctes: Counter = Counter()
        self.rotinas: Counter = Counter()
        self.so_no_sql: Counter = Counter()        # relação citada em SQL, nunca em plano
        self.textos: Counter = Counter()
        self.estat: Counter = Counter()
        self._alias: Dict[str, Optional[str]] = {}
        self._relacao_unica: Optional[str] = None

    # -------------------------------------------------- entrada de registros

    def consumir_registro(self, registro: dict) -> None:
        self.estat["registros"] += 1
        banco = registro.get("dbname")
        if banco:
            self.bancos[banco] += 1
            # Um corpus pode cobrir mais de um banco (o mesmo servidor serve
            # todos), e misturar dois esquemas com nomes de tabela iguais dá
            # um Frankenstein. Quem escolhe é o --banco.
            if self.banco and banco != self.banco:
                self.estat["registros_de_outro_banco"] += 1
                return
        mensagem = registro.get("message") or ""
        if not mensagem:
            return
        corte = mensagem.find("plan:\n")
        if corte >= 0 and mensagem.startswith("duration:"):
            try:
                plano = json.loads(mensagem[corte + 6:])
            except ValueError:
                self.estat["planos_ilegiveis"] += 1
                return
            self.estat["planos"] += 1
            self.consumir_plano(plano)
            return
        casado = RE_STATEMENT.match(mensagem)
        if casado:
            self.estat["statements"] += 1
            self.consumir_sql(casado.group("sql"),
                              parse_parametros(registro.get("detail") or ""))

    def consumir_plano(self, plano: dict) -> None:
        texto = plano.get("Query Text") or ""
        parametros = parse_parametros(plano.get("Query Parameters") or "")
        if texto:
            self.consumir_sql(texto, parametros)
        raiz = plano.get("Plan")
        if not isinstance(raiz, dict):
            return
        self._alias = {}
        self._mapear_aliases(raiz)
        # Plano de uma relação só: coluna sem qualificação em nó de topo
        # (Sort Key, Group Key) só pode ser dela. Com duas ou mais — inclusive
        # se uma delas for de catálogo — não dá para saber, e a coluna fica
        # pendente até a atribuição por nome único no fechamento.
        relacoes = {r for r in self._alias.values() if r}
        unica = next(iter(relacoes)) if len(relacoes) == 1 else None
        self._relacao_unica = unica if unica and self._relevante(unica) else None
        self._percorrer(raiz, parametros, None)

    # -------------------------------------------------- travessia do plano

    def _mapear_aliases(self, no: dict) -> None:
        relacao = no.get("Relation Name")
        apelido = no.get("Alias")
        if relacao:
            if apelido:
                anterior = self._alias.get(apelido, relacao)
                # apelido reaproveitado para relações diferentes: inútil
                self._alias[apelido] = relacao if anterior == relacao else None
            self._alias.setdefault(relacao, relacao)
        for filho in no.get("Plans") or ():
            self._mapear_aliases(filho)

    def _percorrer(self, no: dict, parametros: Dict[int, Optional[str]],
                   relacao_herdada: Optional[str]) -> None:
        tipo_no = no.get("Node Type") or ""
        relacao = no.get("Relation Name")
        if not relacao and tipo_no.startswith("Bitmap"):
            relacao = relacao_herdada  # Bitmap Index Scan não repete a relação

        tabela: Optional[Tabela] = None
        if relacao and (self.incluir_sistema or not e_sistema(relacao)):
            tabela = self._tabela(relacao)
            tabela.origens.add("plano")
            tabela.nos[tipo_no] += 1
            self._cardinalidade(no, tipo_no, tabela)
            if tipo_no == "ModifyTable":
                op = (no.get("Operation") or "").lower()
                if op:
                    tabela.operacoes[op] += 1
        elif relacao:
            relacao = None  # relação de sistema: ignora as expressões dela

        nome_relacao = tabela.nome if tabela else None
        for campo in CAMPOS_EXPRESSAO:
            valor = no.get(campo)
            if not valor:
                continue
            for expressao in (valor if isinstance(valor, list) else [valor]):
                if isinstance(expressao, str):
                    self._consumir_expressao(expressao, nome_relacao, parametros, campo)

        indice = no.get("Index Name")
        if indice and nome_relacao:
            self._indice_do_plano(indice, nome_relacao, no)

        if tipo_no == "Function Scan" and no.get("Function Name"):
            rotina = no["Function Name"].lower()
            if rotina not in ROTINAS_EMBUTIDAS and self._relevante(rotina):
                self.rotinas[rotina] += 1
        if tipo_no == "CTE Scan" and no.get("CTE Name"):
            self.ctes[no["CTE Name"]] += 1

        herdar = nome_relacao if tipo_no.startswith("Bitmap") else None
        for filho in no.get("Plans") or ():
            self._percorrer(filho, parametros, herdar)

    def _cardinalidade(self, no: dict, tipo_no: str, tabela: Tabela) -> None:
        """Quantas linhas a tabela tem, segundo os scans que a leram inteira."""
        if tipo_no != "Seq Scan" or no.get("Parallel Aware"):
            return
        if (no.get("Actual Loops") or 1) != 1:
            return
        lidas = (no.get("Actual Rows") or 0) + (no.get("Rows Removed by Filter") or 0)
        if lidas:
            tabela.linhas_lidas = max(tabela.linhas_lidas or 0, int(lidas))
        if "Filter" not in no and no.get("Plan Rows"):
            tabela.linhas_planner = max(tabela.linhas_planner or 0, int(no["Plan Rows"]))

    def _indice_do_plano(self, nome: str, tabela_nome: str, no: dict) -> None:
        indice = self._indice(nome, tabela_nome)
        indice.usos += 1
        indice.fontes.add("plano")
        if nome.endswith("_pkey"):
            indice.primaria = True
            indice.unico = True
        elif nome.endswith("_key"):
            # nome que o Postgres dá sozinho ao índice de um UNIQUE; a
            # unicidade em si não aparece em plano nenhum
            indice.unico = True
        condicao = no.get("Index Cond")
        if isinstance(condicao, str):
            analise = analisar_expressao(condicao)
            colunas = []
            for alias, coluna in analise.esquerdas:
                dono = self._resolver(alias, tabela_nome)
                if dono == tabela_nome and coluna not in colunas:
                    colunas.append(coluna)
            indice.registrar_colunas(colunas)

    # -------------------------------------------------- expressões

    def _resolver(self, alias: Optional[str], relacao_local: Optional[str]) -> Optional[str]:
        if alias is None:
            return relacao_local or self._relacao_unica
        destino = self._alias.get(alias)
        if not destino:
            return None
        if not self.incluir_sistema and e_sistema(destino):
            return None
        return destino

    def _consumir_expressao(self, expressao: str, relacao_local: Optional[str],
                            parametros: Dict[int, Optional[str]], campo: str) -> None:
        analise = analisar_expressao(expressao)

        for alias, coluna in analise.refs:
            dono = self._resolver(alias, relacao_local)
            if dono:
                self._tabela(dono).coluna(coluna).usos += 1
            elif alias is None:
                self.pendentes_uso[coluna] += 1

        for alias, coluna, tipo, fonte in analise.tipos:
            dono = self._resolver(alias, relacao_local)
            if dono:
                self._tabela(dono).coluna(coluna).votar(tipo, fonte)
            elif alias is None:
                self.pendentes_tipo[(coluna, tipo, fonte)] += 1

        for alias, coluna, numero in analise.parametros:
            if numero not in parametros:
                continue
            valor = parametros[numero]
            tipo = tipo_do_valor(valor)
            dono = self._resolver(alias, relacao_local)
            if dono:
                alvo = self._tabela(dono).coluna(coluna)
                alvo.votar(tipo, "parametro")
                alvo.exemplo(valor)
            elif alias is None and tipo:
                self.pendentes_tipo[(coluna, tipo, "parametro")] += 1

        if campo in CAMPOS_DE_JUNCAO and analise.juncoes:
            pares = []
            for (a1, c1), (a2, c2) in analise.juncoes:
                t1 = self._resolver(a1, relacao_local)
                t2 = self._resolver(a2, relacao_local)
                if not t1 or not t2 or t1 == t2:
                    continue
                par = tuple(sorted([(t1, c1), (t2, c2)]))
                self.juncoes[par] += 1
                pares.append(par)
            if len(pares) > 1:
                self.grupos_juncao[tuple(sorted(set(pares)))] += 1

    # -------------------------------------------------- SQL

    def consumir_sql(self, sql: str, parametros: Dict[int, Optional[str]]) -> None:
        if not sql or len(sql) < 6:
            return
        chave = sql if len(sql) <= 2000 else sql[:2000]
        if len(self.textos) < 50_000:
            self.textos[chave] += 1
            vezes = self.textos[chave]
        else:
            vezes = 1
        if vezes > self.amostras_por_texto:
            return  # este texto já disse o que tinha para dizer

        cabeca = sql.lstrip()[:16].upper()
        if vezes == 1 and cabeca.startswith(("CREATE", "ALTER")):
            self.estat["ddl_comandos"] += 1
            if not self.ignorar_ddl:
                self._consumir_ddl(sql)

        self._consumir_checagem_ri(sql)
        self._consumir_insert(sql, parametros)
        self._consumir_update(sql, parametros)

        for casado in RE_ALVO_ESCRITA.finditer(sql):
            nome = casado.group("tabela").lower()
            if self._relevante(nome):
                op = casado.group("op").split()[0].lower()
                self._tabela(nome).operacoes[op] += 1

        if vezes == 1:
            ctes = {c.lower() for c in RE_CTE.findall(sql)}
            self.ctes.update(ctes)
            for casado in RE_FROM_JOIN.finditer(sql):
                nome = casado.group("tabela").lower()
                esquema = (casado.group("esquema") or "").strip('"').lower()
                if esquema and e_sistema(esquema):
                    continue  # information_schema.columns não é a tabela "columns"
                if self._relevante(nome) and nome not in ctes \
                        and nome not in NAO_SAO_RELACAO:
                    self.so_no_sql[nome] += 1

    def _consumir_checagem_ri(self, sql: str) -> None:
        if "FOR KEY SHARE" not in sql.upper():
            return
        casado = RE_CHECAGEM_RI.search(sql)
        if not casado:
            return
        nome = casado.group("tabela").lower()
        if not self._relevante(nome):
            return
        esquema = casado.group("esquema")
        if esquema:
            self.esquemas[esquema.strip('"')] += 1
        tabela = self._tabela(nome)
        tabela.origens.add("trigger RI")
        condicao = casado.group("cond")
        # "col = $1" é a checagem no pai; "$1 = col" é a checagem nos filhos.
        for coluna in RE_RI_PAI.findall(condicao):
            tabela.ri_referenciada[coluna.lower()] += 1
            tabela.coluna(coluna.lower()).usos += 1
        for coluna in RE_RI_FILHO.findall(condicao):
            tabela.ri_referenciadora[coluna.lower()] += 1
            tabela.coluna(coluna.lower()).usos += 1

    def _consumir_insert(self, sql: str, parametros: Dict[int, Optional[str]]) -> None:
        if "INSERT" not in sql.upper():
            return
        casado = RE_INSERT.search(sql)
        if not casado:
            return
        nome = casado.group("tabela").lower()
        if not self._relevante(nome):
            return
        tabela = self._tabela(nome)
        tabela.origens.add("sql")
        colunas = [_desaspas(c.strip()).lower()
                   for c in partes_topo(casado.group("colunas") or "")
                   if re.fullmatch(_ID_DDL, c.strip())]
        if not colunas:
            return
        if len(colunas) > len(tabela.ordem_insert):
            tabela.ordem_insert = colunas
        valores = partes_topo(casado.group("valores") or "")
        for indice, coluna in enumerate(colunas):
            alvo = tabela.coluna(coluna)
            alvo.usos += 1
            if indice >= len(valores):
                continue
            self._tipar_por_valor(alvo, valores[indice], parametros)

    def _consumir_update(self, sql: str, parametros: Dict[int, Optional[str]]) -> None:
        if "UPDATE" not in sql.upper():
            return
        casado = RE_UPDATE_SET.search(sql)
        if not casado:
            return
        nome = casado.group("tabela").lower()
        if not self._relevante(nome):
            return
        tabela = self._tabela(nome)
        tabela.origens.add("sql")
        for atribuicao in partes_topo(casado.group("atribuicoes")):
            item = RE_ATRIBUICAO.match(atribuicao)
            if not item:
                continue
            coluna = item.group(1).lower()
            if coluna in PALAVRAS_NAO_COLUNA:
                continue
            alvo = tabela.coluna(coluna)
            alvo.usos += 1
            bruto = item.group(2) or item.group(3) or item.group(4)
            self._tipar_por_valor(alvo, bruto, parametros)

    def _tipar_por_valor(self, coluna: Coluna, bruto: Optional[str],
                         parametros: Dict[int, Optional[str]]) -> None:
        if not bruto:
            return
        bruto = bruto.strip()
        if bruto.startswith("$") and bruto[1:].isdigit():
            numero = int(bruto[1:])
            if numero in parametros:
                valor = parametros[numero]
                coluna.votar(tipo_do_valor(valor), "parametro")
                coluna.exemplo(valor)
        elif bruto.startswith("'") and bruto.endswith("'") and len(bruto) >= 2:
            valor = bruto[1:-1].replace("''", "'")
            coluna.votar(tipo_do_valor(valor), "literal_sql")
            coluna.exemplo(valor)
        elif re.fullmatch(r'[-+]?\d+(?:\.\d+)?', bruto):
            coluna.votar("numeric" if "." in bruto else "integer", "literal_sql")
            coluna.exemplo(bruto)

    # -------------------------------------------------- DDL

    def _consumir_ddl(self, sql: str) -> None:
        limpo = " ".join(sql.split()) if len(sql) < 200_000 else sql

        casado = RE_DDL_CREATE_TABLE.search(sql)
        if casado:
            self._ddl_create_table(sql, casado)

        casado = RE_DDL_CREATE_VIEW.search(limpo)
        if casado:
            nome = casado.group("nome").lower()
            if self._relevante(nome):
                tabela = self._tabela(nome)
                tabela.origens.add("ddl")
                tabela.especie = ("view materializada" if casado.group("mat") else "view")
                self.estat["ddl_views"] += 1
                self._colunas_da_view(tabela, limpo, casado.end())

        casado = RE_DDL_CREATE_ROTINA.search(limpo)
        if casado:
            self.rotinas[casado.group("nome").lower()] += 1
            self.estat["ddl_rotinas"] += 1
            return  # corpo de função é outro mundo; os statements internos vêm sozinhos

        for casado in RE_DDL_INDICE.finditer(limpo):
            nome_tabela = casado.group("tabela").lower()
            if not self._relevante(nome_tabela):
                continue
            nome_indice = (casado.group("indice") or f"idx_{nome_tabela}").lower()
            indice = self._indice(nome_indice, nome_tabela)
            indice.fontes.add("ddl")
            indice.unico = indice.unico or bool(casado.group("unico"))
            indice.registrar_colunas(self._colunas_de_lista(casado.group("colunas")))
            self.estat["ddl_indices"] += 1

        for casado in RE_DDL_PK.finditer(limpo):
            nome_tabela = casado.group("tabela").lower()
            if not self._relevante(nome_tabela):
                continue
            tabela = self._tabela(nome_tabela)
            tabela.pk = self._colunas_de_lista(casado.group("colunas"))
            tabela.pk_fonte = "ddl"
            for coluna in tabela.pk:
                tabela.coluna(coluna).not_null = True
            self.estat["ddl_pks"] += 1

        for casado in RE_DDL_UNIQUE.finditer(limpo):
            nome_tabela = casado.group("tabela").lower()
            if not self._relevante(nome_tabela):
                continue
            colunas = self._colunas_de_lista(casado.group("colunas"))
            indice = self._indice(f"{nome_tabela}_{'_'.join(colunas)}_key", nome_tabela)
            indice.unico = True
            indice.fontes.add("ddl")
            indice.registrar_colunas(colunas)

        for casado in RE_DDL_FK.finditer(limpo):
            nome_tabela = casado.group("tabela").lower()
            alvo = casado.group("alvo").lower()
            if not self._relevante(nome_tabela) or not self._relevante(alvo):
                continue
            colunas = self._colunas_de_lista(casado.group("colunas"))
            alvo_colunas = self._colunas_de_lista(casado.group("alvo_colunas") or "")
            alvo_tabela = self._tabela(nome_tabela)
            chave = (tuple(colunas), alvo, tuple(alvo_colunas))
            if chave not in alvo_tabela.fks_ddl:  # o mesmo DDL pode rodar de novo
                alvo_tabela.fks_ddl.append(chave)
                self.estat["ddl_fks"] += 1

    def _ddl_create_table(self, sql: str, casado: re.Match) -> None:
        nome = casado.group("tabela").lower()
        if not self._relevante(nome):
            return
        corpo = corpo_parenteses(sql, casado.end() - 1)
        if corpo is None:
            return
        tabela = self._tabela(nome)
        tabela.origens.add("ddl")
        tabela.especie = "tabela"
        self.estat["ddl_tabelas"] += 1
        for item in partes_topo(corpo):
            if RE_ITEM_TABELA_IGNORADO.match(item):
                pk = re.match(r'^\s*(?:CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY\s*\(([^)]*)\)',
                              item, re.I)
                if pk:
                    tabela.pk = self._colunas_de_lista(pk.group(1))
                    tabela.pk_fonte = "ddl"
                fk = re.match(r'^\s*(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)\s*'
                              r'REFERENCES\s+(?:"?[^".\s(]+"?\.)?"?([A-Za-z_][A-Za-z0-9_$]*)"?'
                              r'\s*(?:\(([^)]*)\))?', item, re.I)
                if fk:
                    chave = (tuple(self._colunas_de_lista(fk.group(1))),
                             fk.group(2).lower(),
                             tuple(self._colunas_de_lista(fk.group(3) or "")))
                    if chave not in tabela.fks_ddl:
                        tabela.fks_ddl.append(chave)
                continue
            campo = RE_COLUNA_DDL.match(item)
            if not campo:
                continue
            nome_coluna = _desaspas(campo.group("col")).lower()
            resto = campo.group("resto").strip()
            corte = RE_MODIFICADOR.search(resto)
            declarado = resto[:corte.start()] if corte else resto
            coluna = tabela.coluna(nome_coluna)
            coluna.tipo_ddl = canonizar_tipo(declarado)
            coluna.votar(canonizar_tipo(tipo_base(coluna.tipo_ddl)), "ddl")
            coluna.not_null = bool(re.search(r'\bNOT\s+NULL\b', resto, re.I))
            # serial vira "integer DEFAULT nextval(...)" no catálogo; para o DDL
            # estimado vale mais reemitir a forma curta, que recria a sequência.
            if declarado.strip().lower() in ("serial", "bigserial", "smallserial"):
                coluna.tipo_serial = declarado.strip().lower()
            else:
                valor = RE_DDL_DEFAULT.search(resto)
                if valor:
                    coluna.default_ddl = " ".join(valor.group("valor").split())
            if nome_coluna not in tabela.ordem_ddl:
                tabela.ordem_ddl.append(nome_coluna)
            if re.search(r'\bPRIMARY\s+KEY\b', resto, re.I):
                tabela.pk = [nome_coluna]
                tabela.pk_fonte = "ddl"
                coluna.not_null = True
            referencia = RE_INLINE_REFERENCES.search(resto)
            if referencia and self._relevante(referencia.group("alvo").lower()):
                chave = ((nome_coluna,), referencia.group("alvo").lower(),
                         tuple(self._colunas_de_lista(
                             referencia.group("alvo_colunas") or "")))
                if chave not in tabela.fks_ddl:
                    tabela.fks_ddl.append(chave)

    def _colunas_da_view(self, tabela: Tabela, sql: str, depois_do_nome: int) -> None:
        """Colunas de saída de um ``CREATE VIEW``.

        Vêm da lista explícita, quando existe (``CREATE VIEW v (a, b) AS``), e
        senão da projeção do SELECT.
        """
        resto = sql[depois_do_nome:].lstrip()
        colunas: List[str] = []
        if resto.startswith("("):
            declarada = corpo_parenteses(resto, 0)
            if declarada:
                colunas = self._colunas_de_lista(declarada)
        if not colunas:
            selecao = re.compile(r'\bAS\s+SELECT\b', re.I).search(sql, depois_do_nome)
            if selecao:
                colunas = colunas_de_projecao(fatia_projecao(sql, selecao.end()))
        for nome in colunas:
            tabela.coluna(nome)
            if nome not in tabela.ordem_ddl:
                tabela.ordem_ddl.append(nome)

    def _colunas_de_lista(self, bruto: str) -> List[str]:
        colunas = []
        for parte in partes_topo(bruto or ""):
            parte = parte.strip()
            # descarta DESC/ASC/NULLS LAST/opclass e expressões
            item = re.match(rf'^({_ID_DDL})(?:\s|$)', parte)
            if not item:
                continue
            nome = _desaspas(item.group(1)).lower()
            if nome not in PALAVRAS_NAO_COLUNA:
                colunas.append(nome)
        return colunas

    # -------------------------------------------------- auxiliares

    def _relevante(self, nome: str) -> bool:
        return bool(nome) and (self.incluir_sistema or not e_sistema(nome))

    def _tabela(self, nome: str) -> Tabela:
        chave = nome.lower()
        tabela = self.tabelas.get(chave)
        if tabela is None:
            tabela = self.tabelas[chave] = Tabela(chave)
        return tabela

    def _indice(self, nome: str, tabela: str) -> Indice:
        alvo = self._tabela(tabela)
        indice = alvo.indices.get(nome)
        if indice is None:
            indice = alvo.indices[nome] = Indice(nome, tabela)
        return indice

    # -------------------------------------------------- fechamento

    def finalizar(self, usar_ddl: bool = True) -> None:
        self._resolver_pendentes()
        self._propagar_tipos_por_juncao()
        self._definir_especies()
        self._definir_pks(usar_ddl)
        self._definir_fks(usar_ddl)

    def _resolver_pendentes(self) -> None:
        """Coluna sem dono (sem apelido, fora de nó de scan) só é aceita quando
        o nome existe em exatamente uma tabela conhecida."""
        dono: Dict[str, List[str]] = defaultdict(list)
        for nome, tabela in self.tabelas.items():
            for coluna in tabela.colunas:
                dono[coluna].append(nome)
        for coluna, n in self.pendentes_uso.items():
            candidatas = dono.get(coluna, ())
            if len(candidatas) == 1:
                self.tabelas[candidatas[0]].coluna(coluna).usos += n
                self.estat["pendentes_resolvidos"] += 1
            else:
                self.estat["pendentes_descartados"] += 1
        for (coluna, tipo, fonte), n in self.pendentes_tipo.items():
            candidatas = dono.get(coluna, ())
            if len(candidatas) == 1:
                self.tabelas[candidatas[0]].coluna(coluna).votar(tipo, fonte, n)

    def _propagar_tipos_por_juncao(self, rodadas: int = 3, minimo: int = 3) -> None:
        """Uma coluna comparada por igualdade com outra tem o tipo dela.

        É o que fecha as chaves: num schema deste feitio a maior parte das
        colunas nunca aparece comparada com literal — só em junção. Propaga a
        partir do tipo inferido pela carga (nunca do DDL), para que a
        conferência continue medindo o que a carga sozinha alcança.
        """
        for _ in range(rodadas):
            novos: List[Tuple[Coluna, str, int]] = []
            for ((t1, c1), (t2, c2)), n in self.juncoes.items():
                if n < minimo:
                    continue
                tab1, tab2 = self.tabelas.get(t1), self.tabelas.get(t2)
                if not tab1 or not tab2:
                    continue
                col1, col2 = tab1.colunas.get(c1), tab2.colunas.get(c2)
                if not col1 or not col2:
                    continue
                tipo1, conf1, _ = col1.resolver(usar_ddl=False)
                tipo2, conf2, _ = col2.resolver(usar_ddl=False)
                if tipo1 and not tipo2 and conf1 >= 0.6:
                    novos.append((col2, tipo_base(tipo1) or tipo1, n))
                elif tipo2 and not tipo1 and conf2 >= 0.6:
                    novos.append((col1, tipo_base(tipo2) or tipo2, n))
            if not novos:
                break
            for coluna, tipo, n in novos:
                coluna.votar(tipo, "juncao", n)
                self.estat["tipos_por_juncao"] += 1

    def _definir_especies(self) -> None:
        self._relacoes_so_do_sql()
        for nome, tabela in self.tabelas.items():
            if tabela.especie in ("tabela", "view", "view materializada"):
                continue
            if "plano" in tabela.origens:
                tabela.especie = "tabela ou view materializada"
            elif nome in self.ctes:
                tabela.especie = "cte"
            elif nome in self.rotinas:
                tabela.especie = "rotina"
            elif self.so_no_sql.get(nome):
                tabela.especie = "view (nunca aparece como relação em plano)"
            else:
                tabela.especie = "relação"

    def _relacoes_so_do_sql(self) -> None:
        """Nome que aparece no FROM mas nunca como relação de plano é view.

        O planner expande a definição da view dentro da consulta, então ela
        some do plano — é assim que dá para separá-la de uma tabela. Descarta
        o que só parece relação: CTE, função, e coluna colhida por engano de um
        ``FROM`` que era de outra coisa.
        """
        conhecidas = {c for t in self.tabelas.values() for c in t.colunas}
        for nome, vezes in self.so_no_sql.items():
            if nome in self.tabelas or nome in self.ctes or nome in self.rotinas:
                continue
            if nome in conhecidas or nome in PALAVRAS_NAO_COLUNA:
                continue
            tabela = self._tabela(nome)
            tabela.origens.add("sql")
            self.estat["relacoes_so_no_sql"] += vezes

    def _definir_pks(self, usar_ddl: bool) -> None:
        for tabela in self.tabelas.values():
            if tabela.pk and tabela.pk_fonte == "ddl":
                if usar_ddl:
                    continue
                tabela.pk, tabela.pk_fonte = [], "-"
            candidatos = [i for i in tabela.indices.values()
                          if i.primaria and i.colunas]
            if not candidatos:
                candidatos = [i for i in tabela.indices.values()
                              if i.unico and i.colunas and "ddl" in i.fontes]
            if candidatos:
                melhor = max(candidatos, key=lambda i: (i.primaria, i.usos))
                tabela.pk = list(melhor.colunas)
                tabela.pk_fonte = f"índice {melhor.nome}"
                for coluna in tabela.pk:
                    tabela.coluna(coluna).not_null = True

        # Índice primário usado sem Index Cond (varredura completa do índice)
        # não mostra coluna nenhuma; a chave primária sabe quais são.
        for tabela in self.tabelas.values():
            for indice in tabela.indices.values():
                if indice.primaria and not indice.colunas and tabela.pk:
                    indice.registrar_colunas(tabela.pk)
                    indice.fontes.add("colunas da PK")

    def _pk_de(self, nome: str) -> List[str]:
        tabela = self.tabelas.get(nome)
        return list(tabela.pk) if tabela else []

    def _unicidade(self, tabela: str, colunas: Sequence[str]) -> Tuple[bool, bool]:
        """(é chave única, a evidência veio do DDL).

        Fora do DDL a lista de colunas de um índice vem dos ``Index Cond`` que
        o corpus mostrou, que podem cobrir só as colunas iniciais — daí a
        distinção: unicidade observada em plano é palpite, não fato.
        """
        alvo = self.tabelas.get(tabela)
        if not alvo:
            return False, False
        conjunto = set(colunas)
        if not conjunto:
            return False, False
        if conjunto == set(alvo.pk):
            return True, alvo.pk_fonte == "ddl"
        for indice in alvo.indices.values():
            if indice.unico and set(indice.colunas) == conjunto:
                return True, "ddl" in indice.fontes
        return False, False

    def _e_unica(self, tabela: str, colunas: Sequence[str]) -> bool:
        return self._unicidade(tabela, colunas)[0]

    def _definir_fks(self, usar_ddl: bool) -> None:
        for tabela in self.tabelas.values():
            tabela.fks = []

        confirmadas: Set[Tuple[str, Tuple[str, ...], str]] = set()
        if usar_ddl:
            for tabela in self.tabelas.values():
                for colunas, alvo, alvo_colunas in tabela.fks_ddl:
                    destino = list(alvo_colunas) or self._pk_de(alvo)
                    tabela.fks.append(ChaveEstrangeira(
                        tabela.nome, list(colunas), alvo, destino, 1.0, ["ddl"]))
                    confirmadas.add((tabela.nome, tuple(colunas), alvo))

        # Junções observadas: agrupa por par de tabelas e decide a direção.
        por_par: Dict[Tuple[str, str], Dict[Tuple[str, str], int]] = defaultdict(dict)
        for ((t1, c1), (t2, c2)), n in self.juncoes.items():
            filho, pai, cf, cp = self._direcao(t1, c1, t2, c2)
            if filho is None:
                continue
            atual = por_par[(filho, pai)]
            atual[(cf, cp)] = atual.get((cf, cp), 0) + n

        for (filho, pai), pares in por_par.items():
            tabela = self.tabelas.get(filho)
            if tabela is None:
                continue
            colunas_filho = [cf for cf, _ in pares]
            colunas_pai = [cp for _, cp in pares]
            composta = len(pares) > 1 and self._e_unica(pai, colunas_pai)
            grupos = ([(colunas_filho, colunas_pai)] if composta
                      else [([cf], [cp]) for cf, cp in pares])
            for cf, cp in grupos:
                if (filho, tuple(cf), pai) in confirmadas:
                    continue
                if any(set(f.colunas) == set(cf) and f.alvo == pai for f in tabela.fks):
                    continue
                observacoes = sum(n for (a, b), n in pares.items()
                                  if a in cf and b in cp)
                evidencias = []
                confianca = 0.35
                if self._e_unica(pai, cp):
                    confianca = 0.60
                    evidencias.append("lado referenciado é chave única")
                if all(self.tabelas[filho].ri_referenciadora.get(c) for c in cf) and \
                        all(self.tabelas[pai].ri_referenciada.get(c) for c in cp):
                    confianca = max(confianca, 0.90)
                    evidencias.append("checagem de trigger RI nos dois lados")
                evidencias.append(f"{fmt_num(observacoes)} junções observadas")
                tabela.fks.append(ChaveEstrangeira(
                    filho, list(cf), pai, list(cp), round(confianca, 2),
                    evidencias, observacoes))

        for tabela in self.tabelas.values():
            tabela.fks.sort(key=lambda f: (-f.confianca, f.alvo, f.colunas))

    def _direcao(self, t1: str, c1: str, t2: str, c2: str
                 ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Quem referencia quem numa junção ``t1.c1 = t2.c2``."""
        tab1, tab2 = self.tabelas.get(t1), self.tabelas.get(t2)
        if not tab1 or not tab2:
            return None, None, None, None

        # 1) trigger RI é prova: um lado é o pai, o outro é o filho
        pai1 = tab1.ri_referenciada.get(c1, 0)
        pai2 = tab2.ri_referenciada.get(c2, 0)
        filho1 = tab1.ri_referenciadora.get(c1, 0)
        filho2 = tab2.ri_referenciadora.get(c2, 0)
        if pai2 and filho1:
            return t1, t2, c1, c2
        if pai1 and filho2:
            return t2, t1, c2, c1

        # 2) o lado que é chave única é o pai
        n1 = tab1.linhas() or 0
        n2 = tab2.linhas() or 0
        (u1, ddl1), (u2, ddl2) = self._unicidade(t1, [c1]), self._unicidade(t2, [c2])
        if u1 != u2:
            filho, pai, cf, cp = ((t1, t2, c1, c2) if u2 else (t2, t1, c2, c1))
            # Unicidade só observada em plano pode ser de índice composto visto
            # pela metade. Se ela aponta um pai maior que o filho, o tamanho
            # (regra 3) é a evidência mais confiável das duas.
            confiavel = ddl2 if u2 else ddl1
            npai, nfilho = (n2, n1) if u2 else (n1, n2)
            if confiavel or not (npai and nfilho) or npai <= nfilho:
                return filho, pai, cf, cp

        # 3) sem unicidade que decida: quem tem mais linhas é quem referencia
        if n1 and n2 and n1 != n2:
            return (t1, t2, c1, c2) if n1 > n2 else (t2, t1, c2, c1)
        return None, None, None, None

    # -------------------------------------------------- conferência

    def conferir_ddl(self) -> Dict[str, object]:
        """Compara o tipo inferido só pela carga com o tipo declarado no DDL.

        É a autoavaliação do estimador: mede o que ele conseguiria num corpus
        que não tivesse capturado o DDL.
        """
        acertos = familia = errados = sem_evidencia = 0
        divergencias: List[str] = []
        for tabela in self.tabelas.values():
            for nome, coluna in tabela.colunas.items():
                if not coluna.tipo_ddl:
                    continue
                inferido, _, _ = coluna.resolver(usar_ddl=False)
                esperado = tipo_base(coluna.tipo_ddl)
                if not inferido:
                    sem_evidencia += 1
                    continue
                if tipo_base(inferido) == esperado:
                    acertos += 1
                    continue
                mesma = (FAMILIA_DO_TIPO.get(tipo_base(inferido) or "")
                         == FAMILIA_DO_TIPO.get(esperado or "")
                         and FAMILIA_DO_TIPO.get(esperado or "") is not None)
                if mesma:
                    familia += 1
                else:
                    errados += 1
                divergencias.append(
                    f"{tabela.nome}.{nome}: DDL {coluna.tipo_ddl}, carga {inferido}"
                    + ("  (mesma família)" if mesma else ""))
        return {"acertos": acertos, "familia": familia, "errados": errados,
                "sem_evidencia": sem_evidencia, "divergencias": divergencias}


# --------------------------------------------------------------------------
# Saídas
# --------------------------------------------------------------------------

def tipo_para_ddl(coluna: Coluna, usar_ddl: bool) -> Tuple[str, str]:
    """Tipo a escrever no DDL estimado e o comentário que o acompanha."""
    tipo, confianca, fonte = coluna.resolver(usar_ddl=usar_ddl)
    notas = []
    if tipo is None:
        return "", "tipo não inferido"
    notas.append(f"{fonte}, confiança {confianca:.2f}")
    if fonte != "ddl":
        if tipo_base(tipo) in ("character", "character varying", "text") and coluna.tam_max:
            notas.append(f"valores de {coluna.tam_min} a {coluna.tam_max} caracteres")
        if coluna.exemplos:
            amostra = ", ".join(e[:18] for e in coluna.exemplos[:3])
            notas.append(f"ex.: {amostra}")
    return tipo, "; ".join(notas)


def gerar_ddl(est: Estimador, usar_ddl: bool, esquema: str) -> str:
    linhas: List[str] = []
    linhas.append("-- Esquema estimado a partir do corpus de logs do Sindri.")
    linhas.append("-- Gerado por src/schema_estimator.py — cada item traz a evidência")
    linhas.append("-- que o sustenta. Nada aqui foi lido do catálogo do banco.")
    linhas.append("")

    fisicas = [t for t in est.tabelas.values()
               if t.especie.startswith("tabela") or t.especie == "view materializada"]

    # Coluna sem tipo inferido sai comentada; toda restrição que dependa dela
    # sai comentada junto, senão o arquivo não roda.
    emitidas = {t.nome: {n for n in t.colunas
                         if tipo_para_ddl(t.colunas[n], usar_ddl)[0]}
                for t in est.tabelas.values()}

    def completa(tabela_alvo: str, colunas: Sequence[str]) -> bool:
        return bool(colunas) and all(c in emitidas.get(tabela_alvo, ()) for c in colunas)

    chaves: List[str] = []
    for tabela in sorted(fisicas, key=lambda t: t.nome):
        if not tabela.colunas:
            continue
        linhas.append(f"-- {'-' * 70}")
        cabecalho = [f"-- {tabela.nome}: {tabela.especie}"]
        if tabela.linhas():
            cabecalho.append(f"~{fmt_num(tabela.linhas())} linhas")
        if tabela.operacoes:
            cabecalho.append(", ".join(f"{k} {fmt_num(v)}x"
                                       for k, v in tabela.operacoes.most_common()))
        linhas.append(" | ".join(cabecalho))
        linhas.append(f"CREATE TABLE {esquema}.{tabela.nome} (")
        colunas = tabela.ordem()
        corpo = []
        for nome in colunas:
            coluna = tabela.colunas[nome]
            tipo, nota = tipo_para_ddl(coluna, usar_ddl)
            sufixo = "NOT NULL" if coluna.not_null else ""
            if usar_ddl:
                if coluna.tipo_serial:
                    tipo = coluna.tipo_serial
                elif coluna.default_ddl:
                    sufixo = f"{sufixo} DEFAULT {coluna.default_ddl}".strip()
            if not tipo:
                corpo.append((f"--  {nome}", "???", sufixo, nota))
            else:
                corpo.append((f"    {nome}", tipo, sufixo, nota))
        largura_nome = max((len(c[0]) for c in corpo), default=4)
        largura_tipo = max((len(c[1]) for c in corpo), default=4)
        reais = [i for i, c in enumerate(corpo) if not c[0].lstrip().startswith("--")]
        ultima = reais[-1] if reais else -1
        for i, (nome, tipo, nulo, nota) in enumerate(corpo):
            virgula = "," if i < ultima else ""
            texto = f"{nome:<{largura_nome}}  {tipo:<{largura_tipo}} {nulo}".rstrip()
            linhas.append(f"{texto}{virgula}".ljust(64) + f"  -- {nota}")
        linhas.append(");")

        if tabela.pk:
            comando = (f"ALTER TABLE {esquema}.{tabela.nome} ADD PRIMARY KEY "
                       f"({', '.join(tabela.pk)});")
            if completa(tabela.nome, tabela.pk):
                linhas.append(f"{comando}  -- {tabela.pk_fonte}")
            else:
                linhas.append(f"-- {comando}  -- {tabela.pk_fonte}; "
                              f"coluna sem tipo inferido")
        for indice in sorted(tabela.indices.values(), key=lambda i: i.nome):
            if indice.primaria or not indice.colunas:
                continue
            unico = "UNIQUE " if indice.unico else ""
            fonte = "+".join(sorted(indice.fontes)) or "plano"
            uso = f", {fmt_num(indice.usos)} usos no corpus" if indice.usos else ""
            comando = (f"CREATE {unico}INDEX {indice.nome} ON {esquema}.{tabela.nome} "
                       f"({', '.join(indice.colunas)});")
            if completa(tabela.nome, indice.colunas):
                linhas.append(f"{comando}  -- {fonte}{uso}")
            else:
                linhas.append(f"-- {comando}  -- {fonte}{uso}; "
                              f"coluna sem tipo inferido")
        # As FKs vão todas para o fim do arquivo: aqui elas apontariam para
        # tabela ainda não criada (customer -> nation, por exemplo).
        for fk in tabela.fks:
            comando = (f"ALTER TABLE {esquema}.{tabela.nome} ADD FOREIGN KEY "
                       f"({', '.join(fk.colunas)}) REFERENCES {esquema}.{fk.alvo} "
                       f"({', '.join(fk.alvo_colunas) or '?'});")
            nota = f"  -- {', '.join(fk.evidencias)}"
            valida = (completa(tabela.nome, fk.colunas)
                      and completa(fk.alvo, fk.alvo_colunas))
            if fk.confianca >= 0.85 and valida:
                chaves.append(comando + nota)
            elif valida:
                chaves.append(f"-- candidata (confiança {fk.confianca:.2f}): "
                              f"{comando}{nota}")
            else:
                chaves.append(f"-- candidata (confiança {fk.confianca:.2f}, coluna sem "
                              f"tipo inferido): {comando}{nota}")
        linhas.append("")

    if chaves:
        linhas.append(f"-- {'-' * 70}")
        linhas.append("-- Chaves estrangeiras. Ficam no fim porque cada uma depende de")
        linhas.append("-- duas tabelas; as comentadas são palpite de junção, não fato.")
        linhas.extend(chaves)
        linhas.append("")

    outras = [t for t in est.tabelas.values() if t not in fisicas]
    if outras:
        linhas.append(f"-- {'-' * 70}")
        linhas.append("-- Relações vistas na carga sem CREATE TABLE correspondente:")
        for tabela in sorted(outras, key=lambda t: t.nome):
            colunas = ", ".join(tabela.ordem()[:12]) or "colunas não identificadas"
            linhas.append(f"--   {tabela.nome} ({tabela.especie}): {colunas}")
        linhas.append("")
    return "\n".join(linhas)


def gerar_json(est: Estimador, usar_ddl: bool, esquema: str) -> dict:
    saida = {
        "esquema": esquema,
        "estatisticas": dict(est.estat),
        "ctes": sorted(est.ctes),
        "rotinas": sorted(est.rotinas),
        "tabelas": [],
    }
    for tabela in sorted(est.tabelas.values(), key=lambda t: t.nome):
        colunas = []
        for nome in tabela.ordem():
            coluna = tabela.colunas[nome]
            tipo, confianca, fonte = coluna.resolver(usar_ddl=usar_ddl)
            inferido, conf_inferido, _ = coluna.resolver(usar_ddl=False)
            colunas.append({
                "nome": nome,
                "tipo": tipo,
                "confianca": confianca,
                "fonte": fonte,
                "tipo_ddl": coluna.tipo_serial or coluna.tipo_ddl,
                "default": coluna.default_ddl,
                "tipo_inferido_pela_carga": inferido,
                "confianca_carga": conf_inferido,
                "not_null": coluna.not_null,
                "usos": coluna.usos,
                "exemplos": coluna.exemplos,
                "tamanho_min": coluna.tam_min,
                "tamanho_max": coluna.tam_max,
                "votos": [{"tipo": t, "fonte": f, "ocorrencias": n}
                          for (t, f), n in coluna.votos.most_common()],
            })
        saida["tabelas"].append({
            "nome": tabela.nome,
            "especie": tabela.especie,
            "origens": sorted(tabela.origens),
            "linhas_estimadas": tabela.linhas(),
            "linhas_planner": tabela.linhas_planner,
            "linhas_lidas": tabela.linhas_lidas,
            "operacoes": dict(tabela.operacoes),
            "nos_de_plano": dict(tabela.nos),
            "chave_primaria": {"colunas": tabela.pk, "fonte": tabela.pk_fonte},
            "colunas": colunas,
            "indices": [{"nome": i.nome, "colunas": i.colunas, "unico": i.unico,
                         "primaria": i.primaria, "usos": i.usos,
                         "fontes": sorted(i.fontes)}
                        for i in sorted(tabela.indices.values(), key=lambda i: i.nome)],
            "chaves_estrangeiras": [{"colunas": f.colunas, "alvo": f.alvo,
                                     "alvo_colunas": f.alvo_colunas,
                                     "confianca": f.confianca,
                                     "evidencias": f.evidencias}
                                    for f in tabela.fks],
        })
    return saida


def gerar_relatorio(est: Estimador, usar_ddl: bool, esquema: str,
                    arquivos: List[Path], segundos: float) -> str:
    linhas: List[str] = []
    total_bytes = sum(a.stat().st_size for a in arquivos)
    linhas.append("Sindri / BrokkPG — esquema estimado a partir dos logs")
    linhas.append("=" * 72)
    megas = f"{total_bytes / 1e6:,.1f}".replace(",", ".")
    linhas.append(f"corpus     : {len(arquivos)} arquivos, {megas} MB")
    linhas.append(f"registros  : {fmt_num(est.estat['registros'])} "
                  f"(planos {fmt_num(est.estat['planos'])}, "
                  f"statements {fmt_num(est.estat['statements'])}, "
                  f"ilegíveis {fmt_num(est.estat['registros_ilegiveis'])})")
    if est.ignorar_ddl:
        linhas.append(f"DDL no log : {fmt_num(est.estat['ddl_comandos'])} comandos, "
                      f"todos descartados por --ignorar-ddl")
    else:
        linhas.append(f"DDL no log : tabelas {est.estat['ddl_tabelas']}, "
                      f"pks {est.estat['ddl_pks']}, fks {est.estat['ddl_fks']}, "
                      f"índices {est.estat['ddl_indices']}, "
                      f"views {est.estat['ddl_views']} "
                      f"(de {fmt_num(est.estat['ddl_comandos'])} comandos)")
    if est.bancos:
        nomes = ", ".join(f"{b} ({fmt_num(n)})" for b, n in est.bancos.most_common())
        linhas.append(f"bancos     : {nomes}"
                      + (f"   (usando só {est.banco})" if est.banco else ""))
    linhas.append(f"tempo      : {segundos:.1f} s")
    if not est.banco and len(est.bancos) > 1:
        principal = est.bancos.most_common(1)[0][0]
        linhas.append("")
        linhas.append(f"AVISO: o corpus cobre {len(est.bancos)} bancos e o esquema "
                      f"abaixo é a mistura dos dois.")
        linhas.append(f"       Rode com --banco {principal} para ficar só com um.")
    linhas.append("")

    fisicas = sorted((t for t in est.tabelas.values()
                      if t.especie.startswith("tabela")
                      or t.especie == "view materializada"),
                     key=lambda t: -(t.linhas() or 0))
    linhas.append("Relações físicas")
    linhas.append("-" * 72)
    linhas.append(f"{'relação':<28}{'colunas':>8}{'linhas':>14}{'índices':>9}  operações")
    for tabela in fisicas:
        ops = ", ".join(f"{k} {fmt_num(v)}" for k, v in tabela.operacoes.most_common(4))
        linhas.append(f"{tabela.nome:<28}{len(tabela.colunas):>8}"
                      f"{fmt_num(tabela.linhas()):>14}{len(tabela.indices):>9}  {ops}")
    linhas.append("")

    for tabela in sorted(fisicas, key=lambda t: t.nome):
        titulo = f"{esquema}.{tabela.nome}"
        detalhe = [tabela.especie, f"{len(tabela.colunas)} colunas"]
        if tabela.linhas():
            detalhe.append(f"~{fmt_num(tabela.linhas())} linhas")
        detalhe.append("evidência: " + ", ".join(sorted(tabela.origens)))
        linhas.append(f"{titulo}  ({'; '.join(detalhe)})")
        linhas.append("-" * 72)
        linhas.append(f"  {'coluna':<18}{'tipo':<26}{'conf':>5} {'fonte':<12}"
                      f"{'usos':>9}  exemplos")
        for nome in tabela.ordem():
            coluna = tabela.colunas[nome]
            tipo, confianca, fonte = coluna.resolver(usar_ddl=usar_ddl)
            marca = " PK" if nome in tabela.pk else ""
            exemplos = ", ".join(e[:14] for e in coluna.exemplos[:3])
            linhas.append(f"  {nome + marca:<18}{tipo or '?':<26}{confianca:>5.2f} "
                          f"{fonte:<12}{fmt_num(coluna.usos):>9}  {exemplos}")
        if tabela.pk:
            linhas.append(f"  PK  ({', '.join(tabela.pk)})   [{tabela.pk_fonte}]")
        for indice in sorted(tabela.indices.values(), key=lambda i: i.nome):
            atributos = []
            if indice.primaria:
                atributos.append("primária")
            elif indice.unico:
                atributos.append("única")
            atributos.append("+".join(sorted(indice.fontes)) or "plano")
            if indice.usos:
                atributos.append(f"{fmt_num(indice.usos)} usos")
            colunas = ", ".join(indice.colunas) or "colunas não observadas"
            linhas.append(f"  IX  {indice.nome} ({colunas})   [{'; '.join(atributos)}]")
        for fk in tabela.fks:
            rotulo = "FK " if fk.confianca >= 0.85 else "fk?"
            linhas.append(f"  {rotulo} ({', '.join(fk.colunas)}) -> {fk.alvo}"
                          f"({', '.join(fk.alvo_colunas) or '?'})   "
                          f"[confiança {fk.confianca:.2f}: {', '.join(fk.evidencias)}]")
        linhas.append("")

    outras = sorted((t for t in est.tabelas.values() if t not in fisicas),
                    key=lambda t: t.nome)
    if outras:
        linhas.append("Outras relações citadas pela carga")
        linhas.append("-" * 72)
        for tabela in outras:
            colunas = ", ".join(tabela.ordem()[:10]) or "colunas não identificadas"
            linhas.append(f"  {tabela.nome} ({tabela.especie}): {colunas}")
        linhas.append("")
    if est.ctes or est.rotinas:
        if est.ctes:
            linhas.append("CTEs observadas: " + ", ".join(sorted(est.ctes)))
        if est.rotinas:
            linhas.append("Rotinas observadas: " + ", ".join(sorted(est.rotinas)))
        linhas.append("")

    conferencia = est.conferir_ddl()
    total = sum(int(conferencia[k]) for k in
                ("acertos", "familia", "errados", "sem_evidencia"))
    if total:
        acertos = int(conferencia["acertos"])
        tipadas = total - int(conferencia["sem_evidencia"])
        divergencias = list(conferencia["divergencias"])  # type: ignore[arg-type]
        linhas.append("Conferência: tipo inferido pela carga x tipo declarado no DDL")
        linhas.append("-" * 72)
        linhas.append(f"  {acertos} de {total} colunas ({acertos / total:.0%}) tipadas "
                      f"exatamente só com a carga; {conferencia['familia']} na família "
                      f"certa e {conferencia['errados']} erradas.")
        if tipadas:
            linhas.append(f"  Entre as {tipadas} que a carga conseguiu tipar, "
                          f"{acertos} exatas ({acertos / tipadas:.0%}).")
        linhas.append(f"  {conferencia['sem_evidencia']} colunas ficaram sem evidência "
                      f"nenhuma: a carga só as projeta, nunca as compara.")
        for item in divergencias[:15]:
            linhas.append(f"    - {item}")
        if len(divergencias) > 15:
            linhas.append(f"    ... e mais {len(divergencias) - 15}")
        linhas.append("")
    return "\n".join(linhas)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def descobrir_arquivos(pasta: Path, padrao: str) -> List[Path]:
    return sorted(p for p in pasta.glob(padrao) if p.is_file())


def main(argv: Optional[Sequence[str]] = None) -> int:
    raiz = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Reconstrói o esquema do banco a partir dos logs do Postgres.")
    ap.add_argument("--logs", type=Path, default=raiz / "logs",
                    help="pasta com os arquivos de log (padrão: ./logs)")
    ap.add_argument("--padrao", default="*.json",
                    help="glob dos arquivos a ler (padrão: *.json)")
    ap.add_argument("--sql", type=Path, help="grava o DDL estimado neste arquivo")
    ap.add_argument("--json", type=Path, dest="json_saida",
                    help="grava a evidência completa neste arquivo JSON")
    ap.add_argument("--esquema", default=None,
                    help="nome do schema no DDL gerado (padrão: o visto no log)")
    ap.add_argument("--banco", metavar="NOME",
                    help="considera só os registros deste banco (dbname do log)")
    ap.add_argument("--ignorar-ddl", action="store_true",
                    help="descarta a evidência de DDL e usa só o que a carga revela")
    ap.add_argument("--incluir-sistema", action="store_true",
                    help="não filtra as relações do catálogo (pg_*)")
    ap.add_argument("--limite", type=int, default=0,
                    help="lê no máximo N registros por arquivo (para testes)")
    ap.add_argument("--sem-relatorio", action="store_true",
                    help="não imprime o relatório no stdout")
    ap.add_argument("--quieto", action="store_true", help="não mostra progresso")
    args = ap.parse_args(argv)

    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if not args.logs.is_dir():
        print(f"pasta de logs não encontrada: {args.logs}", file=sys.stderr)
        return 2
    arquivos = descobrir_arquivos(args.logs, args.padrao)
    if not arquivos:
        print(f"nenhum arquivo casou com {args.padrao} em {args.logs}", file=sys.stderr)
        return 2

    est = Estimador(incluir_sistema=args.incluir_sistema,
                    ignorar_ddl=args.ignorar_ddl, banco=args.banco)
    inicio = time.time()
    for posicao, arquivo in enumerate(arquivos, 1):
        marca = time.time()
        antes = est.estat["registros"]
        for registro in iter_registros(arquivo, est.estat, args.limite):
            est.consumir_registro(registro)
        if not args.quieto:
            print(f"[{posicao}/{len(arquivos)}] {arquivo.name}  "
                  f"{arquivo.stat().st_size / 1e6:6.1f} MB  "
                  f"{fmt_num(est.estat['registros'] - antes):>9} registros  "
                  f"{time.time() - marca:5.1f}s", file=sys.stderr)
    est.estat["arquivos"] = len(arquivos)

    usar_ddl = not args.ignorar_ddl
    est.finalizar(usar_ddl=usar_ddl)
    segundos = time.time() - inicio
    esquema = args.esquema or (est.esquemas.most_common(1)[0][0]
                               if est.esquemas else "public")

    if not args.sem_relatorio:
        print(gerar_relatorio(est, usar_ddl, esquema, arquivos, segundos))

    if args.sql:
        args.sql.parent.mkdir(parents=True, exist_ok=True)
        args.sql.write_text(gerar_ddl(est, usar_ddl, esquema), encoding="utf-8")
        print(f"DDL estimado: {args.sql}", file=sys.stderr)
    if args.json_saida:
        args.json_saida.parent.mkdir(parents=True, exist_ok=True)
        args.json_saida.write_text(
            json.dumps(gerar_json(est, usar_ddl, esquema), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"evidência: {args.json_saida}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
