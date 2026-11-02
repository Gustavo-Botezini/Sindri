-- Banco separado para o YCSB, no mesmo servidor observado: os planos caem em
-- logs/ como os do TPC-H, mas o DDL do BenchBase (DROP TABLE usertable) e a
-- carga não encostam no tpch. A tabela é criada pelo próprio BenchBase, com
-- --create=true (ver ycsb-load.sh); aqui só nasce o banco vazio.
CREATE DATABASE ycsb OWNER sindri;

\connect ycsb
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
