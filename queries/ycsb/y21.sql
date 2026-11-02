-- Y21LacunasAntiJoin, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 617889

SELECT count(*) AS lacunas
FROM (SELECT 617889::int AS k) p,
     generate_series(p.k, p.k + 100000) AS g(id)
WHERE NOT EXISTS (SELECT 1 FROM usertable u WHERE u.ycsb_key = g.id);
