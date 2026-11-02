-- Y11TopNTextoJanela, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 208496

SELECT u.ycsb_key, u.field4
FROM usertable u, (SELECT 208496::int AS k) p
WHERE u.ycsb_key BETWEEN p.k AND p.k + 20000
ORDER BY u.field4
LIMIT 10;
