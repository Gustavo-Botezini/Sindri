-- Y15AutoJuncaoConsecutivas, de benchbase/config/templates_ycsb_mixed.xml
-- parâmetros fixos: 571412

SELECT count(*) AS pares
FROM (SELECT 571412::int AS k) p
JOIN usertable a ON a.ycsb_key BETWEEN p.k AND p.k + 200000
JOIN usertable b ON b.ycsb_key = a.ycsb_key + 1
                AND b.ycsb_key BETWEEN p.k AND p.k + 200001
WHERE left(a.field1, 1) = left(b.field1, 1);
