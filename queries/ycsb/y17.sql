-- Y17CteMaterializada, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 79

WITH sel AS MATERIALIZED (
    SELECT ycsb_key, field8
    FROM usertable
    WHERE left(field7, 1) = chr(79)
)
SELECT left(field8, 1) AS prefixo, count(*) AS linhas
FROM sel
GROUP BY 1
ORDER BY 2 DESC
LIMIT 10;
