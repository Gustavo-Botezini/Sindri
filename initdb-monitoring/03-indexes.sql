-- ---------------------------------------------------------------------------
-- BrokkPG — índices de pg_log_event.
--
-- Criados no pai particionado: o Postgres propaga para as partições que já
-- existem e para as que forem criadas depois por monitoring.ensure_partition.
--
-- São cinco, não um por coluna. A ingestão normal é incremental (~25 mil
-- linhas por arquivo de 50 MB), e cinco índices nesse tamanho de lote custam
-- pouco. Para uma carga inicial de um corpus inteiro de uma vez, vale dropar
-- os quatro não-únicos, carregar e recriar — o único tem que ficar, porque é
-- ele que o ON CONFLICT usa.
-- ---------------------------------------------------------------------------

-- A chave natural do log do Postgres. Faz dois trabalhos:
--
--   1. Deduplicação: é o que permite re-ingerir um arquivo (ou reler a partir
--      de um offset errado) sem duplicar nada.
--   2. Correlação plano -> query: a ordem no log é bind -> plano(s) -> execute,
--      e o execute imediatamente seguinte, na mesma sessão, carrega o query_id
--      correto. A varredura do worker é uma window function
--      OVER (PARTITION BY session_id ORDER BY line_num), e é este índice que a
--      alimenta ordenada.
--
-- event_time entra porque índice único em tabela particionada tem que conter
-- a chave de partição. Não enfraquece a garantia: o event_time é derivado do
-- próprio registro, então o mesmo (session_id, line_num) sempre cai na mesma
-- partição.
CREATE UNIQUE INDEX uq_pg_log_event_session_line
    ON monitoring.pg_log_event (session_id, line_num, event_time);

-- O recorte principal do worker. Planos são 21,4% do corpus; o índice parcial
-- é ~1/5 do que um índice cheio em event_time custaria.
CREATE INDEX ix_pg_log_event_plan
    ON monitoring.pg_log_event (event_time)
    WHERE record_type = 'plan';

-- Junção com pg_stat_statements do banco observado.
--
-- O filtro é <> 0, NÃO "IS NOT NULL": o Postgres escreve "query_id": 0 quando
-- não há id (25,8% dos registros do corpus), nunca ausente. Um índice parcial
-- com IS NOT NULL não excluiria uma linha sequer.
CREATE INDEX ix_pg_log_event_query_id
    ON monitoring.pg_log_event (query_id, event_time)
    WHERE query_id <> 0;

-- Erros. No corpus medido são 33 registros em 567.811 (22 ERROR, 11 FATAL) —
-- 0,006%. O índice fica com dezenas de entradas e custa praticamente nada,
-- mas sem ele achar um erro é seq scan em meio milhão de linhas.
--
-- O contrário também vale: não indexe error_severity inteira. Com 99,994% de
-- um valor só ('LOG'), o planner nunca escolheria o índice cheio.
CREATE INDEX ix_pg_log_event_problema
    ON monitoring.pg_log_event (event_time, error_severity)
    WHERE error_severity <> 'LOG';

-- Para reprocessar ou apagar o que veio de um arquivo específico.
CREATE INDEX ix_pg_log_event_file
    ON monitoring.pg_log_event (ingestion_file_id);

-- Não foi criado, e é de propósito: um índice em (database_name, event_time).
-- Neste projeto existem dois bancos (tpch e postgres). Com cardinalidade 2, o
-- planner prefere seq scan a percorrer um índice que aponta para metade da
-- tabela. Acrescente índice novo quando o workload pedir, não antes.
