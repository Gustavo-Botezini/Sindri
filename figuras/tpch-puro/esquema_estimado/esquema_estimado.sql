-- Esquema estimado a partir do corpus de logs do Sindri.
-- Gerado por src/schema_estimator.py — cada item traz a evidência
-- que o sustenta. Nada aqui foi lido do catálogo do banco.

-- ----------------------------------------------------------------------
-- customer: tabela ou view materializada
CREATE TABLE public.customer (
--  c_custkey     ???,                                            -- tipo não inferido
--  c_nationkey   ???,                                            -- tipo não inferido
--  c_phone       ???,                                            -- tipo não inferido
    c_acctbal     numeric,                                        -- numero, confiança 0.60
    c_mktsegment  character                                       -- cast, confiança 0.90
);

-- ----------------------------------------------------------------------
-- lineitem: tabela ou view materializada | ~6.001.215 linhas
CREATE TABLE public.lineitem (
--  l_orderkey       ???       NOT NULL,                          -- tipo não inferido
--  l_suppkey        ???,                                         -- tipo não inferido
    l_shipdate       date,                                        -- cast, confiança 0.90
    l_quantity       numeric,                                     -- cast, confiança 0.90
--  l_partkey        ???,                                         -- tipo não inferido
    l_discount       numeric,                                     -- numero, confiança 0.60
    l_receiptdate    date,                                        -- cast, confiança 0.90
--  l_commitdate     ???,                                         -- tipo não inferido
    l_shipmode       character,                                   -- array, confiança 0.85
--  l_extendedprice  ???,                                         -- tipo não inferido
    l_returnflag     character,                                   -- cast, confiança 0.90
--  l_linestatus     ???,                                         -- tipo não inferido
    l_shipinstruct   character                                    -- cast, confiança 0.90
);
-- ALTER TABLE public.lineitem ADD PRIMARY KEY (l_orderkey);  -- índice lineitem_pkey; coluna sem tipo inferido

-- ----------------------------------------------------------------------
-- nation: tabela ou view materializada | ~25 linhas
CREATE TABLE public.nation (
    n_name       character                                        -- cast, confiança 0.90
--  n_nationkey  ???       NOT NULL                               -- tipo não inferido
--  n_regionkey  ???                                              -- tipo não inferido
);
-- ALTER TABLE public.nation ADD PRIMARY KEY (n_nationkey);  -- índice nation_pkey; coluna sem tipo inferido

-- ----------------------------------------------------------------------
-- orders: tabela ou view materializada
CREATE TABLE public.orders (
    o_orderdate      date,                                        -- cast, confiança 0.90
--  o_orderkey       ???               NOT NULL,                  -- tipo não inferido
--  o_custkey        ???,                                         -- tipo não inferido
--  o_orderpriority  ???,                                         -- tipo não inferido
--  o_shippriority   ???,                                         -- tipo não inferido
    o_comment        character varying,                           -- cast_saida, confiança 0.35
    o_orderstatus    character                                    -- cast, confiança 0.90
--  o_totalprice     ???                                          -- tipo não inferido
);
-- ALTER TABLE public.orders ADD PRIMARY KEY (o_orderkey);  -- índice orders_pkey; coluna sem tipo inferido

-- ----------------------------------------------------------------------
-- part: tabela ou view materializada | ~200.000 linhas
CREATE TABLE public.part (
    p_size       integer,                                         -- array, confiança 0.85
    p_brand      character,                                       -- cast, confiança 0.90
--  p_partkey    ???,                                             -- tipo não inferido
    p_container  character,                                       -- cast, confiança 0.90
    p_type       character varying,                               -- cast_saida, confiança 0.35
    p_name       character varying                                -- cast_saida, confiança 0.35
);

-- ----------------------------------------------------------------------
-- partsupp: tabela ou view materializada | ~800.000 linhas
CREATE TABLE public.partsupp (
--  ps_suppkey     ???                                            -- tipo não inferido
--  ps_partkey     ??? NOT NULL                                   -- tipo não inferido
--  ps_availqty    ???                                            -- tipo não inferido
--  ps_supplycost  ???                                            -- tipo não inferido
);
-- ALTER TABLE public.partsupp ADD PRIMARY KEY (ps_partkey);  -- índice partsupp_pkey; coluna sem tipo inferido

-- ----------------------------------------------------------------------
-- region: tabela ou view materializada | ~5 linhas
CREATE TABLE public.region (
    r_name       character                                        -- cast, confiança 0.90
--  r_regionkey  ???                                              -- tipo não inferido
);

-- ----------------------------------------------------------------------
-- supplier: tabela ou view materializada | ~10.000 linhas
CREATE TABLE public.supplier (
--  s_suppkey    ???               NOT NULL,                      -- tipo não inferido
--  s_nationkey  ???,                                             -- tipo não inferido
--  s_name       ???,                                             -- tipo não inferido
--  s_acctbal    ???,                                             -- tipo não inferido
    s_comment    character varying                                -- cast_saida, confiança 0.35
);
-- ALTER TABLE public.supplier ADD PRIMARY KEY (s_suppkey);  -- índice supplier_pkey; coluna sem tipo inferido

-- ----------------------------------------------------------------------
-- Chaves estrangeiras. Ficam no fim porque cada uma depende de
-- duas tabelas; as comentadas são palpite de junção, não fato.
-- candidata (confiança 0.60, coluna sem tipo inferido): ALTER TABLE public.customer ADD FOREIGN KEY (c_nationkey) REFERENCES public.nation (n_nationkey);  -- lado referenciado é chave única, 24 junções observadas
-- candidata (confiança 0.60, coluna sem tipo inferido): ALTER TABLE public.lineitem ADD FOREIGN KEY (l_partkey) REFERENCES public.partsupp (ps_partkey);  -- lado referenciado é chave única, 12 junções observadas
-- candidata (confiança 0.60, coluna sem tipo inferido): ALTER TABLE public.lineitem ADD FOREIGN KEY (l_suppkey) REFERENCES public.supplier (s_suppkey);  -- lado referenciado é chave única, 36 junções observadas
-- candidata (confiança 0.35, coluna sem tipo inferido): ALTER TABLE public.lineitem ADD FOREIGN KEY (l_partkey) REFERENCES public.part (p_partkey);  -- 36 junções observadas
-- candidata (confiança 0.35, coluna sem tipo inferido): ALTER TABLE public.lineitem ADD FOREIGN KEY (l_suppkey) REFERENCES public.partsupp (ps_suppkey);  -- 9 junções observadas
-- candidata (confiança 0.35, coluna sem tipo inferido): ALTER TABLE public.nation ADD FOREIGN KEY (n_regionkey) REFERENCES public.region (r_regionkey);  -- 24 junções observadas
-- candidata (confiança 0.60, coluna sem tipo inferido): ALTER TABLE public.partsupp ADD FOREIGN KEY (ps_suppkey) REFERENCES public.supplier (s_suppkey);  -- lado referenciado é chave única, 33 junções observadas
-- candidata (confiança 0.35, coluna sem tipo inferido): ALTER TABLE public.partsupp ADD FOREIGN KEY (ps_partkey) REFERENCES public.part (p_partkey);  -- 24 junções observadas
-- candidata (confiança 0.60, coluna sem tipo inferido): ALTER TABLE public.supplier ADD FOREIGN KEY (s_nationkey) REFERENCES public.nation (n_nationkey);  -- lado referenciado é chave única, 54 junções observadas

-- ----------------------------------------------------------------------
-- Relações vistas na carga sem CREATE TABLE correspondente:
--   lateral (view (nunca aparece como relação em plano)): colunas não identificadas
--   revenue0 (view): supplier_no, total_revenue
