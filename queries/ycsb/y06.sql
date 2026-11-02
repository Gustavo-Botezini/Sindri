-- Y06BuscaPrefixoCampo, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 79, 79, 79

SELECT ycsb_key, field2
FROM usertable
WHERE left(field2, 3) = chr(79) || chr(79) || chr(79)
LIMIT 10;
