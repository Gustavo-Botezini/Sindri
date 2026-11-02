-- Y13HistogramaFaixas, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 981453

SELECT ycsb_key / 10000 AS faixa, count(*) AS linhas,
       count(*) FILTER (WHERE field5 < field6) AS f5_menor_f6
FROM usertable
WHERE ycsb_key < 981453
GROUP BY 1
ORDER BY 1;
