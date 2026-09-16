-- Idempotente: dropa constraints antes de recriar, pode rodar múltiplas vezes.
ALTER TABLE lineitem DROP CONSTRAINT IF EXISTS lineitem_pkey CASCADE;
ALTER TABLE orders   DROP CONSTRAINT IF EXISTS orders_pkey CASCADE;
ALTER TABLE customer DROP CONSTRAINT IF EXISTS customer_pkey CASCADE;
ALTER TABLE partsupp DROP CONSTRAINT IF EXISTS partsupp_pkey CASCADE;
ALTER TABLE supplier DROP CONSTRAINT IF EXISTS supplier_pkey CASCADE;
ALTER TABLE part     DROP CONSTRAINT IF EXISTS part_pkey CASCADE;
ALTER TABLE nation   DROP CONSTRAINT IF EXISTS nation_pkey CASCADE;
ALTER TABLE region   DROP CONSTRAINT IF EXISTS region_pkey CASCADE;

TRUNCATE region, nation, part, supplier, partsupp, customer, orders, lineitem;

\copy region   FROM '/tpch-data/region.tbl'   WITH (FORMAT csv, DELIMITER '|');
\copy nation   FROM '/tpch-data/nation.tbl'   WITH (FORMAT csv, DELIMITER '|');
\copy part     FROM '/tpch-data/part.tbl'     WITH (FORMAT csv, DELIMITER '|');
\copy supplier FROM '/tpch-data/supplier.tbl' WITH (FORMAT csv, DELIMITER '|');
\copy partsupp FROM '/tpch-data/partsupp.tbl' WITH (FORMAT csv, DELIMITER '|');
\copy customer FROM '/tpch-data/customer.tbl' WITH (FORMAT csv, DELIMITER '|');
\copy orders   FROM '/tpch-data/orders.tbl'   WITH (FORMAT csv, DELIMITER '|');
\copy lineitem FROM '/tpch-data/lineitem.tbl' WITH (FORMAT csv, DELIMITER '|');

ALTER TABLE region   ADD PRIMARY KEY (r_regionkey);
ALTER TABLE nation   ADD PRIMARY KEY (n_nationkey);
ALTER TABLE part     ADD PRIMARY KEY (p_partkey);
ALTER TABLE supplier ADD PRIMARY KEY (s_suppkey);
ALTER TABLE partsupp ADD PRIMARY KEY (ps_partkey, ps_suppkey);
ALTER TABLE customer ADD PRIMARY KEY (c_custkey);
ALTER TABLE orders   ADD PRIMARY KEY (o_orderkey);
ALTER TABLE lineitem ADD PRIMARY KEY (l_orderkey, l_linenumber);

ALTER TABLE nation   ADD FOREIGN KEY (n_regionkey) REFERENCES region (r_regionkey);
ALTER TABLE supplier ADD FOREIGN KEY (s_nationkey) REFERENCES nation (n_nationkey);
ALTER TABLE customer ADD FOREIGN KEY (c_nationkey) REFERENCES nation (n_nationkey);
ALTER TABLE partsupp ADD FOREIGN KEY (ps_partkey) REFERENCES part (p_partkey);
ALTER TABLE partsupp ADD FOREIGN KEY (ps_suppkey) REFERENCES supplier (s_suppkey);
ALTER TABLE orders   ADD FOREIGN KEY (o_custkey)  REFERENCES customer (c_custkey);
ALTER TABLE lineitem ADD FOREIGN KEY (l_orderkey) REFERENCES orders (o_orderkey);
ALTER TABLE lineitem ADD FOREIGN KEY (l_partkey, l_suppkey) REFERENCES partsupp (ps_partkey, ps_suppkey);

ANALYZE;
