-- Y12DistribuicaoPrefixo, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 93850

SELECT left(field1, 1) AS prefixo, count(*) AS linhas,
       avg(ascii(substr(field1, 2, 1))) AS media_segundo
FROM usertable
WHERE ycsb_key >= 93850
GROUP BY 1
ORDER BY 2 DESC;
