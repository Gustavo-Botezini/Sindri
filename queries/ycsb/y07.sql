-- Y07ContagemJanela, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 619176

SELECT count(*), min(length(u.field1))
FROM usertable u, (SELECT 619176::int AS k) p
WHERE u.ycsb_key BETWEEN p.k AND p.k + 1000;
