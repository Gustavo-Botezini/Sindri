-- Objetos consultados pelos templates de benchbase/config/templates_tpch_extra.xml.
-- Ficam aqui para serem recriados junto com a base numa subida limpa.

-- ---------------------------------------------------------------- view
-- Agregação por pedido. Como é view comum (não materializada), o planner
-- expande a definição dentro da query que a consulta: o plano que aparece no
-- log é o da query expandida, não um "scan de view".
CREATE OR REPLACE VIEW v_order_revenue AS
SELECT o.o_orderkey,
       o.o_orderdate,
       o.o_custkey,
       c.c_nationkey,
       sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue,
       count(*)                                    AS itens
FROM orders o
JOIN customer c ON c.c_custkey  = o.o_custkey
JOIN lineitem l ON l.l_orderkey = o.o_orderkey
GROUP BY o.o_orderkey, o.o_orderdate, o.o_custkey, c.c_nationkey;

-- ------------------------------------------------------ view materializada
-- Receita por nação e mês. ~2 mil linhas resumindo 6 milhões: consultar isto
-- gera plano trivial, e é justamente o contraste que interessa no corpus —
-- a mesma pergunta respondida pela tabela base custa segundos.
DROP MATERIALIZED VIEW IF EXISTS mv_nation_month_revenue;
CREATE MATERIALIZED VIEW mv_nation_month_revenue AS
SELECT n.n_nationkey,
       n.n_name,
       date_trunc('month', o.o_orderdate)::date AS mes,
       sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue,
       count(*)                                    AS itens
FROM lineitem l
JOIN orders   o ON o.o_orderkey = l.l_orderkey
JOIN customer c ON c.c_custkey  = o.o_custkey
JOIN nation   n ON n.n_nationkey = c.c_nationkey
GROUP BY 1, 2, 3;

-- Índice único: além de acelerar a consulta, é pré-requisito de
-- REFRESH ... CONCURRENTLY, caso você queira testar depois fora do BenchBase
-- (dentro dele não dá: CONCURRENTLY não roda em bloco de transação).
CREATE UNIQUE INDEX mv_nation_month_pk ON mv_nation_month_revenue (n_nationkey, mes);

-- ------------------------------------------------------------ tabela de apoio
-- Destino das gravações da procedure. Mantida pequena por ela mesma.
CREATE TABLE IF NOT EXISTS bench_report (
    id          bigserial PRIMARY KEY,
    nationkey   integer     NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    revenue     numeric,
    pedidos     bigint
);

-- ------------------------------------------------------------- procedure
-- Três statements num CALL só: um agregado pesado, um INSERT e um DELETE.
-- Com auto_explain.log_nested_statements ligado, os três aparecem no log
-- separadamente, o que normalmente é o ponto cego de qualquer lógica que viva
-- dentro do banco.
CREATE OR REPLACE PROCEDURE sp_snapshot_nation(p_nationkey integer)
LANGUAGE plpgsql AS $$
DECLARE
    v_revenue numeric;
    v_pedidos bigint;
BEGIN
    SELECT coalesce(sum(l.l_extendedprice * (1 - l.l_discount)), 0),
           count(DISTINCT o.o_orderkey)
      INTO v_revenue, v_pedidos
      FROM lineitem l
      JOIN orders   o ON o.o_orderkey = l.l_orderkey
      JOIN customer c ON c.c_custkey  = o.o_custkey
     WHERE c.c_nationkey = p_nationkey
       AND o.o_orderdate >= DATE '1997-01-01';

    INSERT INTO bench_report (nationkey, revenue, pedidos)
    VALUES (p_nationkey, v_revenue, v_pedidos);

    DELETE FROM bench_report
     WHERE id < (SELECT max(id) - 500 FROM bench_report);
END;
$$;

-- -------------------------------------------------------------- função
-- Em plpgsql de propósito. Uma função SQL simples seria "inlined" pelo
-- planner e sumiria dentro do plano da query chamadora; em plpgsql ela vira
-- statement aninhado próprio, visível no log.
CREATE OR REPLACE FUNCTION fn_customer_revenue(p_custkey integer)
RETURNS numeric
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v numeric;
BEGIN
    SELECT coalesce(sum(l.l_extendedprice * (1 - l.l_discount)), 0)
      INTO v
      FROM orders   o
      JOIN lineitem l ON l.l_orderkey = o.o_orderkey
     WHERE o.o_custkey = p_custkey;
    RETURN v;
END;
$$;
