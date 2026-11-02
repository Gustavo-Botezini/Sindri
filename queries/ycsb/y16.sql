-- Y16DistintosPrefixo, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 219949

SELECT count(DISTINCT left(field6, 2)) AS prefixos
FROM usertable
WHERE ycsb_key >= 219949;
