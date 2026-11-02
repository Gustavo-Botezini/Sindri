-- Y05ExisteChave, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 182323

SELECT EXISTS (SELECT 1 FROM usertable WHERE ycsb_key = 182323);
