-- Esquema estimado a partir do corpus de logs do Sindri.
-- Gerado por src/schema_estimator.py — cada item traz a evidência
-- que o sustenta. Nada aqui foi lido do catálogo do banco.

-- ----------------------------------------------------------------------
-- bench_report: tabela | ~920 linhas | insert 133x, delete 133x
CREATE TABLE public.bench_report (
    id           bigserial                NOT NULL,               -- ddl, confiança 1.00
    nationkey    integer                  NOT NULL,               -- ddl, confiança 1.00
    computed_at  timestamp with time zone NOT NULL DEFAULT now(),  -- ddl, confiança 1.00
    revenue      numeric,                                         -- ddl, confiança 1.00
    pedidos      bigint                                           -- ddl, confiança 1.00
);
ALTER TABLE public.bench_report ADD PRIMARY KEY (id);  -- ddl

-- ----------------------------------------------------------------------
-- customer: tabela | ~150.000 linhas
CREATE TABLE public.customer (
    c_custkey     integer                NOT NULL,                -- ddl, confiança 1.00
    c_name        character varying(25)  NOT NULL,                -- ddl, confiança 1.00
    c_address     character varying(40)  NOT NULL,                -- ddl, confiança 1.00
    c_nationkey   integer                NOT NULL,                -- ddl, confiança 1.00
    c_phone       character(15)          NOT NULL,                -- ddl, confiança 1.00
    c_acctbal     numeric(15,2)          NOT NULL,                -- ddl, confiança 1.00
    c_mktsegment  character(10)          NOT NULL,                -- ddl, confiança 1.00
    c_comment     character varying(117) NOT NULL                 -- ddl, confiança 1.00
);
ALTER TABLE public.customer ADD PRIMARY KEY (c_custkey);  -- ddl
CREATE INDEX idx_customer_nationkey ON public.customer (c_nationkey);  -- ddl+plano, 404 usos no corpus

-- ----------------------------------------------------------------------
-- lineitem: tabela | ~6.001.215 linhas | update 8.101x
CREATE TABLE public.lineitem (
    l_orderkey       integer               NOT NULL,              -- ddl, confiança 1.00
    l_partkey        integer               NOT NULL,              -- ddl, confiança 1.00
    l_suppkey        integer               NOT NULL,              -- ddl, confiança 1.00
    l_linenumber     integer               NOT NULL,              -- ddl, confiança 1.00
    l_quantity       numeric(15,2)         NOT NULL,              -- ddl, confiança 1.00
    l_extendedprice  numeric(15,2)         NOT NULL,              -- ddl, confiança 1.00
    l_discount       numeric(15,2)         NOT NULL,              -- ddl, confiança 1.00
    l_tax            numeric(15,2)         NOT NULL,              -- ddl, confiança 1.00
    l_returnflag     character(1)          NOT NULL,              -- ddl, confiança 1.00
    l_linestatus     character(1)          NOT NULL,              -- ddl, confiança 1.00
    l_shipdate       date                  NOT NULL,              -- ddl, confiança 1.00
    l_commitdate     date                  NOT NULL,              -- ddl, confiança 1.00
    l_receiptdate    date                  NOT NULL,              -- ddl, confiança 1.00
    l_shipinstruct   character(25)         NOT NULL,              -- ddl, confiança 1.00
    l_shipmode       character(10)         NOT NULL,              -- ddl, confiança 1.00
    l_comment        character varying(44) NOT NULL               -- ddl, confiança 1.00
);
ALTER TABLE public.lineitem ADD PRIMARY KEY (l_orderkey, l_linenumber);  -- ddl
CREATE INDEX idx_lineitem_orderkey ON public.lineitem (l_orderkey);  -- ddl+plano, 19.216 usos no corpus
CREATE INDEX idx_lineitem_partkey ON public.lineitem (l_partkey);  -- ddl+plano, 1.236 usos no corpus
CREATE INDEX idx_lineitem_suppkey ON public.lineitem (l_suppkey);  -- ddl+plano, 556 usos no corpus

-- ----------------------------------------------------------------------
-- mv_nation_month_revenue: view materializada | ~2.000 linhas
CREATE TABLE public.mv_nation_month_revenue (
--  n_nationkey  ???  NOT NULL,                                   -- tipo não inferido
--  n_name       ???,                                             -- tipo não inferido
    mes          date NOT NULL                                    -- cast, confiança 0.90; ex.: 1994-08-04, 1996-12-07, 1993-07-25
--  revenue      ???                                              -- tipo não inferido
--  itens        ???                                              -- tipo não inferido
);
-- ALTER TABLE public.mv_nation_month_revenue ADD PRIMARY KEY (n_nationkey, mes);  -- índice mv_nation_month_pk; coluna sem tipo inferido
-- CREATE UNIQUE INDEX mv_nation_month_pk ON public.mv_nation_month_revenue (n_nationkey, mes);  -- ddl+plano, 44 usos no corpus; coluna sem tipo inferido

-- ----------------------------------------------------------------------
-- nation: tabela | ~25 linhas
CREATE TABLE public.nation (
    n_nationkey  integer                NOT NULL,                 -- ddl, confiança 1.00
    n_name       character(25)          NOT NULL,                 -- ddl, confiança 1.00
    n_regionkey  integer                NOT NULL,                 -- ddl, confiança 1.00
    n_comment    character varying(152)                           -- ddl, confiança 1.00
);
ALTER TABLE public.nation ADD PRIMARY KEY (n_nationkey);  -- ddl
CREATE INDEX idx_nation_regionkey ON public.nation (n_regionkey);  -- ddl

-- ----------------------------------------------------------------------
-- orders: tabela | ~1.508.894 linhas | insert 21.691x, update 15.920x, delete 7.912x
CREATE TABLE public.orders (
    o_orderkey       integer               NOT NULL,              -- ddl, confiança 1.00
    o_custkey        integer               NOT NULL,              -- ddl, confiança 1.00
    o_orderstatus    character(1)          NOT NULL,              -- ddl, confiança 1.00
    o_totalprice     numeric(15,2)         NOT NULL,              -- ddl, confiança 1.00
    o_orderdate      date                  NOT NULL,              -- ddl, confiança 1.00
    o_orderpriority  character(15)         NOT NULL,              -- ddl, confiança 1.00
    o_clerk          character(15)         NOT NULL,              -- ddl, confiança 1.00
    o_shippriority   integer               NOT NULL,              -- ddl, confiança 1.00
    o_comment        character varying(79) NOT NULL               -- ddl, confiança 1.00
);
ALTER TABLE public.orders ADD PRIMARY KEY (o_orderkey);  -- ddl
CREATE INDEX idx_orders_custkey ON public.orders (o_custkey);  -- ddl+plano, 2.587 usos no corpus

-- ----------------------------------------------------------------------
-- part: tabela | ~200.000 linhas
CREATE TABLE public.part (
    p_partkey      integer               NOT NULL,                -- ddl, confiança 1.00
    p_name         character varying(55) NOT NULL,                -- ddl, confiança 1.00
    p_mfgr         character(25)         NOT NULL,                -- ddl, confiança 1.00
    p_brand        character(10)         NOT NULL,                -- ddl, confiança 1.00
    p_type         character varying(25) NOT NULL,                -- ddl, confiança 1.00
    p_size         integer               NOT NULL,                -- ddl, confiança 1.00
    p_container    character(10)         NOT NULL,                -- ddl, confiança 1.00
    p_retailprice  numeric(15,2)         NOT NULL,                -- ddl, confiança 1.00
    p_comment      character varying(23) NOT NULL                 -- ddl, confiança 1.00
);
ALTER TABLE public.part ADD PRIMARY KEY (p_partkey);  -- ddl

-- ----------------------------------------------------------------------
-- partsupp: tabela | ~800.000 linhas
CREATE TABLE public.partsupp (
    ps_partkey     integer                NOT NULL,               -- ddl, confiança 1.00
    ps_suppkey     integer                NOT NULL,               -- ddl, confiança 1.00
    ps_availqty    integer                NOT NULL,               -- ddl, confiança 1.00
    ps_supplycost  numeric(15,2)          NOT NULL,               -- ddl, confiança 1.00
    ps_comment     character varying(199) NOT NULL                -- ddl, confiança 1.00
);
ALTER TABLE public.partsupp ADD PRIMARY KEY (ps_partkey, ps_suppkey);  -- ddl
CREATE INDEX idx_partsupp_suppkey ON public.partsupp (ps_suppkey);  -- ddl+plano, 450 usos no corpus

-- ----------------------------------------------------------------------
-- region: tabela | ~5 linhas
CREATE TABLE public.region (
    r_regionkey  integer                NOT NULL,                 -- ddl, confiança 1.00
    r_name       character(25)          NOT NULL,                 -- ddl, confiança 1.00
    r_comment    character varying(152)                           -- ddl, confiança 1.00
);
ALTER TABLE public.region ADD PRIMARY KEY (r_regionkey);  -- ddl

-- ----------------------------------------------------------------------
-- supplier: tabela | ~10.000 linhas
CREATE TABLE public.supplier (
    s_suppkey    integer                NOT NULL,                 -- ddl, confiança 1.00
    s_name       character(25)          NOT NULL,                 -- ddl, confiança 1.00
    s_address    character varying(40)  NOT NULL,                 -- ddl, confiança 1.00
    s_nationkey  integer                NOT NULL,                 -- ddl, confiança 1.00
    s_phone      character(15)          NOT NULL,                 -- ddl, confiança 1.00
    s_acctbal    numeric(15,2)          NOT NULL,                 -- ddl, confiança 1.00
    s_comment    character varying(101) NOT NULL                  -- ddl, confiança 1.00
);
ALTER TABLE public.supplier ADD PRIMARY KEY (s_suppkey);  -- ddl
CREATE INDEX idx_supplier_nationkey ON public.supplier (s_nationkey);  -- ddl+plano, 498 usos no corpus

-- ----------------------------------------------------------------------
-- Chaves estrangeiras. Ficam no fim porque cada uma depende de
-- duas tabelas; as comentadas são palpite de junção, não fato.
ALTER TABLE public.customer ADD FOREIGN KEY (c_nationkey) REFERENCES public.nation (n_nationkey);  -- ddl
-- candidata (confiança 0.35): ALTER TABLE public.customer ADD FOREIGN KEY (c_nationkey) REFERENCES public.supplier (s_nationkey);  -- 187 junções observadas
ALTER TABLE public.lineitem ADD FOREIGN KEY (l_orderkey) REFERENCES public.orders (o_orderkey);  -- ddl
ALTER TABLE public.lineitem ADD FOREIGN KEY (l_partkey, l_suppkey) REFERENCES public.partsupp (ps_partkey, ps_suppkey);  -- ddl
-- candidata (confiança 0.60): ALTER TABLE public.lineitem ADD FOREIGN KEY (l_partkey) REFERENCES public.part (p_partkey);  -- lado referenciado é chave única, 1.482 junções observadas
-- candidata (confiança 0.60): ALTER TABLE public.lineitem ADD FOREIGN KEY (l_suppkey) REFERENCES public.supplier (s_suppkey);  -- lado referenciado é chave única, 1.191 junções observadas
ALTER TABLE public.nation ADD FOREIGN KEY (n_regionkey) REFERENCES public.region (r_regionkey);  -- ddl
ALTER TABLE public.orders ADD FOREIGN KEY (o_custkey) REFERENCES public.customer (c_custkey);  -- ddl
ALTER TABLE public.partsupp ADD FOREIGN KEY (ps_partkey) REFERENCES public.part (p_partkey);  -- ddl
ALTER TABLE public.partsupp ADD FOREIGN KEY (ps_suppkey) REFERENCES public.supplier (s_suppkey);  -- ddl
ALTER TABLE public.supplier ADD FOREIGN KEY (s_nationkey) REFERENCES public.nation (n_nationkey);  -- ddl

-- ----------------------------------------------------------------------
-- Relações vistas na carga sem CREATE TABLE correspondente:
--   revenue0 (view): supplier_no, total_revenue
--   v_order_revenue (view): o_orderkey, o_orderdate, o_custkey, c_nationkey, revenue, itens
