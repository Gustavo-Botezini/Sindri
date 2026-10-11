#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""O que a query lê e por onde: FROM, JOIN/ON e WHERE, sem o SELECT.

O agrupamento por texto completo junta queries que *falam* parecido, e o
SELECT é a maior parte da fala: Q1, Q6, Q14 e Q17 caíram juntas por causa de
``sum(l_extendedprice * (1 - l_discount))``, que nenhum índice toca. Índice só
responde ao que está no FROM (qual tabela) e no WHERE/ON (por qual coluna se
filtra ou se junta). Este módulo extrai isso, para duas representações que
entram no pipeline como modos de normalização (ver ``corpus.normalizar``):

**``from-where`` (método A).** O texto de FROM, JOIN/ON e WHERE, recursivo nas
tabelas derivadas, e só ele vai para o mesmo modelo de embedding. Mede quanto
do problema era o SELECT.

**``estrutura`` (método B).** Uma assinatura de atributos com o papel de cada
um, para TF-IDF:

    FROM:lineitem
    JOIN:lineitem.l_partkey        coluna dos dois lados de uma comparação
    JOIN:part.p_partkey            entre relações diferentes (inclui IN e
                                   EXISTS com subquery, que são semijunções)
    FILTER:lineitem.l_quantity:<   coluna contra constante, parâmetro ou
    FILTER:part.p_brand:=          subquery escalar, com o operador

O papel fica porque ``l_partkey`` numa junção e ``l_partkey`` num filtro não
são a mesma evidência para indexação; o operador fica porque igualdade e
intervalo pedem índices diferentes. JOIN guarda a coluna e não o par
(``l_partkey=p_partkey``): é a coluna que vira índice, e é ela que Q17
(``l_partkey = p_partkey``) e Q20 (``l_partkey = ps_partkey``) compartilham.

O parsing é do pglast, que usa o parser do próprio Postgres: aliases,
subqueries, ``$n``, ``JOIN ... ON`` e formas sintáticas equivalentes saem
certas sem regra escrita à mão. A extração roda sobre o SQL original, antes de
qualquer normalização. Colunas sem qualificador são resolvidas pelo catálogo do
banco observado, subindo pelos escopos — é assim que o ``p_partkey`` dentro da
subquery da Q17 chega à ``part`` de fora.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

# Conectar ao banco observado sem deixar rastro no log dele: as opções de
# inicialização valem desde o primeiro statement, um SET viria tarde demais.
OPCOES_SEM_LOG = ("-c log_min_duration_statement=-1 "
                  "-c auto_explain.log_min_duration=-1")


@lru_cache(maxsize=1)
def catalogo() -> Dict[str, FrozenSet[str]]:
    """Colunas de cada tabela do banco observado, lidas uma vez."""
    import psycopg

    from .banco import DSN_OBSERVADO

    with psycopg.connect(DSN_OBSERVADO, options=OPCOES_SEM_LOG,
                         application_name="brokk_estrutura") as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT table_name, column_name
                             FROM information_schema.columns
                            WHERE table_schema = 'public'""")
            colunas: Dict[str, Set[str]] = {}
            for tabela, coluna in cur.fetchall():
                colunas.setdefault(tabela, set()).add(coluna)
    return {t: frozenset(c) for t, c in colunas.items()}


# --------------------------------------------------------------------------
# Escopo e resolução de colunas
# --------------------------------------------------------------------------

@dataclass
class _Escopo:
    """Os nomes visíveis num SELECT: apelido (ou o próprio nome) -> relação."""
    apelidos: Dict[str, str] = field(default_factory=dict)
    pai: Optional["_Escopo"] = None

    def cadeia(self):
        e = self
        while e is not None:
            yield e
            e = e.pai


# Uma coluna resolvida: ("lineitem.l_partkey", instância). A instância é o
# apelido dentro do seu escopo, e é ela que decide se uma comparação é junção:
# l1.l_orderkey = l2.l_orderkey (a autojunção da Q21) liga duas instâncias da
# mesma tabela, e pelo nome da tabela pareceria filtro.
Coluna = Tuple[str, str]


def _resolver(campos, esc: _Escopo) -> Optional[Coluna]:
    from pglast import ast

    nomes = [f.sval for f in campos if isinstance(f, ast.String)]
    if not nomes:                       # tabela.* ou *
        return None
    col = nomes[-1]
    if len(nomes) >= 2:
        apelido = nomes[-2]
        for e in esc.cadeia():
            if apelido in e.apelidos:
                return f"{e.apelidos[apelido]}.{col}", f"{id(e)}:{apelido}"
        return f"{apelido}.{col}", f"?:{apelido}"

    cat = catalogo()
    # Primeiro quem tem a coluna no catálogo, do escopo mais interno para fora:
    # é a regra do SQL para referência correlacionada.
    for e in esc.cadeia():
        donos = [(a, r) for a, r in e.apelidos.items() if col in cat.get(r, ())]
        if len(donos) == 1:
            a, r = donos[0]
            return f"{r}.{col}", f"{id(e)}:{a}"
        if donos:
            return f"?.{col}", "?"
    # Depois, view ou tabela derivada (fora do catálogo), se for a única
    # candidata do escopo — o revenue0 da Q15.
    for e in esc.cadeia():
        fora = [(a, r) for a, r in e.apelidos.items() if r not in cat]
        if len(fora) == 1:
            a, r = fora[0]
            return f"{r}.{col}", f"{id(e)}:{a}"
    return f"?.{col}", "?"


def _colunas(no, esc: _Escopo) -> List[Coluna]:
    """Colunas citadas numa expressão, sem descer em subquery (escopo próprio)."""
    from pglast import ast

    achadas: List[Coluna] = []

    def visita(n):
        if n is None:
            return
        if isinstance(n, (list, tuple)):
            for x in n:
                visita(x)
            return
        if isinstance(n, ast.ColumnRef):
            r = _resolver(n.fields, esc)
            if r:
                achadas.append(r)
            return
        if isinstance(n, ast.SubLink):
            return
        if isinstance(n, ast.Node):
            for attr in getattr(type(n), "__slots__", ()):
                v = getattr(n, attr, None)
                if isinstance(v, (ast.Node, list, tuple)):
                    visita(v)

    visita(no)
    return achadas


def _sublinks(no):
    """As subqueries de uma expressão, sem entrar nelas."""
    from pglast import ast

    if no is None:
        return
    if isinstance(no, (list, tuple)):
        for x in no:
            yield from _sublinks(x)
        return
    if isinstance(no, ast.SubLink):
        yield no
        return
    if isinstance(no, ast.Node):
        for attr in getattr(type(no), "__slots__", ()):
            v = getattr(no, attr, None)
            if isinstance(v, (ast.Node, list, tuple)):
                yield from _sublinks(v)


# --------------------------------------------------------------------------
# Método B: a assinatura
# --------------------------------------------------------------------------

_OPS_LIKE = {"~~": "LIKE", "!~~": "NOT_LIKE", "~~*": "ILIKE", "!~~*": "NOT_ILIKE"}


def _operador(expr, negado: bool) -> str:
    from pglast.enums.parsenodes import A_Expr_Kind as K

    nome = expr.name[-1].sval if expr.name else "?"
    if expr.kind == K.AEXPR_IN:
        op = "IN" if nome == "=" else "NOT_IN"
    elif expr.kind in (K.AEXPR_LIKE, K.AEXPR_ILIKE):
        op = _OPS_LIKE.get(nome, nome)
    elif expr.kind in (K.AEXPR_BETWEEN, K.AEXPR_BETWEEN_SYM):
        op = "BETWEEN"
    elif expr.kind in (K.AEXPR_NOT_BETWEEN, K.AEXPR_NOT_BETWEEN_SYM):
        op = "NOT_BETWEEN"
    elif expr.kind == K.AEXPR_OP:
        op = nome
    else:
        op = expr.kind.name.replace("AEXPR_", "")
    return f"NOT_{op}" if negado else op


def _comparacao(esq: List[Coluna], dir_: List[Coluna], op: str,
                feats: Set[str]) -> None:
    # Junção: colunas dos dois lados, de mais de uma instância. Duas colunas da
    # mesma instância (l_commitdate < l_receiptdate) continuam sendo filtro.
    if esq and dir_ and len({i for _, i in esq + dir_}) > 1:
        for c, _ in esq + dir_:
            feats.add(f"JOIN:{c}")
        return
    for c, _ in esq + dir_:
        feats.add(f"FILTER:{c}:{op}")


def _predicado(no, esc: _Escopo, feats: Set[str], negado: bool = False) -> None:
    from pglast import ast
    from pglast.enums.primnodes import BoolExprType

    if no is None:
        return
    if isinstance(no, ast.BoolExpr):
        neg = negado ^ (no.boolop == BoolExprType.NOT_EXPR)
        for a in no.args:
            _predicado(a, esc, feats, neg)
        return
    if isinstance(no, ast.SubLink):
        _sublink(no, esc, feats)
        return
    if isinstance(no, ast.A_Expr):
        _comparacao(_colunas(no.lexpr, esc), _colunas(no.rexpr, esc),
                    _operador(no, negado), feats)
        for s in _sublinks([no.lexpr, no.rexpr]):
            _sublink(s, esc, feats)
        return
    if isinstance(no, ast.NullTest):
        for c, _ in _colunas(no.arg, esc):
            feats.add(f"FILTER:{c}:IS_NULL")
        return
    for c, _ in _colunas(no, esc):
        feats.add(f"FILTER:{c}:EXPR")
    for s in _sublinks(no):
        _sublink(s, esc, feats)


def _sublink(no, esc: _Escopo, feats: Set[str]) -> None:
    from pglast.enums.primnodes import SubLinkType as T

    interno = _select(no.subselect, feats, pai=esc)
    # IN (subquery) e = ANY são semijunção: a coluna de fora contra a coluna
    # que a subquery devolve. Como junção, e não como filtro.
    if no.subLinkType == T.ANY_SUBLINK and interno is not None:
        fora = _colunas(no.testexpr, esc)
        dentro = []
        for alvo in no.subselect.targetList or ():
            dentro += _colunas(alvo.val, interno)
        for c, _ in fora + dentro:
            feats.add(f"JOIN:{c}")


def _de(item, esc: _Escopo, feats: Set[str]) -> None:
    from pglast import ast

    if isinstance(item, ast.RangeVar):
        rel = item.relname
        # Com apelido, o SQL só aceita o apelido; sem ele, o nome da tabela.
        esc.apelidos[item.alias.aliasname if item.alias else rel] = rel
        feats.add(f"FROM:{rel}")
    elif isinstance(item, ast.JoinExpr):
        _de(item.larg, esc, feats)
        _de(item.rarg, esc, feats)
        _predicado(item.quals, esc, feats)
    elif isinstance(item, ast.RangeSubselect):
        _select(item.subquery, feats, pai=esc if item.lateral else None)
        if item.alias:
            esc.apelidos[item.alias.aliasname] = item.alias.aliasname


def _select(sel, feats: Set[str], pai: Optional[_Escopo]) -> Optional[_Escopo]:
    from pglast.enums.parsenodes import SetOperation

    if sel.op != SetOperation.SETOP_NONE:
        _select(sel.larg, feats, pai)
        _select(sel.rarg, feats, pai)
        return None
    esc = _Escopo(pai=pai)
    if sel.withClause:
        for cte in sel.withClause.ctes:
            _select(cte.ctequery, feats, pai)
            esc.apelidos[cte.ctename] = cte.ctename
    for item in sel.fromClause or ():
        _de(item, esc, feats)
    _predicado(sel.whereClause, esc, feats)
    return esc


def assinatura(sql: str) -> List[str]:
    """Os atributos da query com o papel de cada um, em ordem estável."""
    from pglast import parse_sql

    feats: Set[str] = set()
    for raw in parse_sql(sql):
        _select(raw.stmt, feats, None)
    return sorted(feats)


# --------------------------------------------------------------------------
# Método A: o texto de FROM e WHERE
# --------------------------------------------------------------------------

def _texto_de(item) -> str:
    from pglast import ast
    from pglast.stream import RawStream

    if isinstance(item, ast.JoinExpr):
        tipo = {0: "JOIN", 1: "LEFT JOIN", 2: "FULL JOIN", 3: "RIGHT JOIN"}.get(
            int(item.jointype), "JOIN")
        on = f" ON {RawStream()(item.quals)}" if item.quals is not None else ""
        return f"{_texto_de(item.larg)} {tipo} {_texto_de(item.rarg)}{on}"
    if isinstance(item, ast.RangeSubselect):
        apelido = f" AS {item.alias.aliasname}" if item.alias else ""
        return f"({_texto_select(item.subquery)}){apelido}"
    return RawStream()(item)


def _texto_select(sel) -> str:
    from pglast.enums.parsenodes import SetOperation
    from pglast.stream import RawStream

    if sel.op != SetOperation.SETOP_NONE:
        return f"({_texto_select(sel.larg)}) UNION ({_texto_select(sel.rarg)})"
    partes = []
    if sel.withClause:
        ctes = ", ".join(f"{c.ctename} AS ({_texto_select(c.ctequery)})"
                         for c in sel.withClause.ctes)
        partes.append(f"WITH {ctes}")
    if sel.fromClause:
        partes.append("FROM " + ", ".join(_texto_de(i) for i in sel.fromClause))
    if sel.whereClause is not None:
        # As subqueries do WHERE vão inteiras: o que elas devolvem é parte do
        # predicado (l_quantity < (SELECT 0.2 * avg(l_quantity) ...)).
        partes.append("WHERE " + RawStream()(sel.whereClause))
    return " ".join(partes)


def texto_from_where(sql: str) -> str:
    """FROM, JOIN/ON e WHERE da query, recursivo nas tabelas derivadas."""
    from pglast import parse_sql

    return " ; ".join(_texto_select(raw.stmt) for raw in parse_sql(sql))
