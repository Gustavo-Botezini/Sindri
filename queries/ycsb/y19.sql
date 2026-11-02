-- Y19BuscaSubstring, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 52, 77, 109

SELECT count(*) AS ocorrencias
FROM usertable
WHERE field9 LIKE '%' || chr(52) || chr(77) || chr(109) || '%';
