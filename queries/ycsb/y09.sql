-- Y09Vizinhos, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 33327

SELECT (SELECT max(ycsb_key) FROM usertable WHERE ycsb_key < p.k) AS anterior,
       (SELECT min(ycsb_key) FROM usertable WHERE ycsb_key > p.k) AS proxima
FROM (SELECT 33327::int AS k) p;
