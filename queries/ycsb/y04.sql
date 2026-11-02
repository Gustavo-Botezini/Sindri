-- Y04PaginaDescendente, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 936518

SELECT ycsb_key, field2
FROM usertable
WHERE ycsb_key < 936518
ORDER BY ycsb_key DESC
LIMIT 50;
