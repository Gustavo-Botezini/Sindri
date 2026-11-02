-- Y02ListaChaves, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 116739, 26225, 777572, 288389, 256787, 234053, 146316, 772246, 107473, 709570

SELECT ycsb_key, field1
FROM usertable
WHERE ycsb_key IN (116739, 26225, 777572, 288389, 256787, 234053, 146316, 772246, 107473, 709570);
