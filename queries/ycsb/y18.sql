-- Y18RollupPrefixos, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 157787

SELECT left(field1, 1) AS p1, left(field2, 1) AS p2, count(*) AS linhas
FROM usertable
WHERE ycsb_key < 157787
GROUP BY ROLLUP (left(field1, 1), left(field2, 1))
ORDER BY 1, 2;
