-- Y10LeituraLoteUnnest, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 31244, 98246, 229258, 243962, 529903, 631262, 27824, 588508

SELECT u.ycsb_key, u.field1, u.field5
FROM unnest(ARRAY[31244, 98246, 229258, 243962, 529903, 631262, 27824, 588508]::int[]) AS c(id)
JOIN usertable u ON u.ycsb_key = c.id;
