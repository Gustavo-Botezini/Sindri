-- Y03PaginaAscendente, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 776646

SELECT ycsb_key, field1
FROM usertable
WHERE ycsb_key > 776646
ORDER BY ycsb_key
LIMIT 20;
