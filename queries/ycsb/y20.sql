-- Y20MedianaPorPrefixo, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 471029

SELECT left(u.field10, 1) AS prefixo,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY ascii(substr(u.field10, 2, 1))) AS mediana,
       count(*) AS linhas
FROM usertable u, (SELECT 471029::int AS k) p
WHERE u.ycsb_key BETWEEN p.k AND p.k + 200000
GROUP BY 1
ORDER BY 1;
