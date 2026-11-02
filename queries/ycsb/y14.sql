-- Y14RankPorFaixa, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 735392

SELECT faixa, ycsb_key, field5
FROM (
  SELECT u.ycsb_key, u.ycsb_key / 1000 AS faixa, u.field5,
         row_number() OVER (PARTITION BY u.ycsb_key / 1000 ORDER BY u.field5) AS pos
  FROM usertable u, (SELECT 735392::int AS k) p
  WHERE u.ycsb_key BETWEEN p.k AND p.k + 100000
) t
WHERE pos <= 3
ORDER BY faixa, pos;
