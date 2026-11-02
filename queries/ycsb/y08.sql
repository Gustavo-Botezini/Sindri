-- Y08FiltroCompostoJanela, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 442417, 79

SELECT u.ycsb_key, u.field3
FROM usertable u, (SELECT 442417::int AS k) p
WHERE u.ycsb_key BETWEEN p.k AND p.k + 50000
  AND left(u.field3, 1) = chr(79)
ORDER BY u.ycsb_key
LIMIT 100;
