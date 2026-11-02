-- Y22IntersecaoPrefixos, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 291704

WITH p AS (SELECT 291704::int AS k)
SELECT count(*) AS comuns
FROM (
  SELECT left(u.field1, 2) FROM usertable u, p
  WHERE u.ycsb_key BETWEEN p.k AND p.k + 50000
  INTERSECT
  SELECT left(u.field2, 2) FROM usertable u, p
  WHERE u.ycsb_key BETWEEN p.k AND p.k + 50000
) x;
