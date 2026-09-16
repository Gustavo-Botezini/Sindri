-- ---------------------------------------------------------------------------
-- BrokkPG — funções de ingestão.
--
-- O contrato com o ingestor Python é curto de propósito:
--
--   1. INSERT em monitoring.ingestion_file (ou UPDATE, se o arquivo já existe)
--   2. COPY monitoring.pg_log_event_staging FROM STDIN
--   3. SELECT * FROM monitoring.ingest_staging(<file_id>)
--   4. UPDATE do offset em monitoring.ingestion_file
--
-- Os passos 2 a 4 cabem numa transação só. Nada disso é trigger: o ingestor
-- chama explicitamente, e o worker roda depois, por conta própria.
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- Criação de partição sob demanda
-- ---------------------------------------------------------------------------
-- Não existe partição DEFAULT neste schema, e é intencional: uma vez que a
-- DEFAULT tenha linhas de um dia, criar a partição daquele dia exige varrer a
-- DEFAULT inteira e falha se houver conflito. Melhor criar antes de inserir.
--
-- Os limites são escritos com deslocamento +00 explícito em vez de deixar o
-- literal ser interpretado pelo TimeZone da sessão: o jsonlog do Postgres sai
-- em UTC, e uma sessão com outro fuso criaria partições deslocadas.
CREATE FUNCTION monitoring.ensure_partition(p_day date)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_name text := 'pg_log_event_' || to_char(p_day, 'YYYYMMDD');
BEGIN
    IF to_regclass('monitoring.' || quote_ident(v_name)) IS NOT NULL THEN
        RETURN v_name;
    END IF;

    BEGIN
        EXECUTE format(
            'CREATE TABLE monitoring.%I PARTITION OF monitoring.pg_log_event '
            'FOR VALUES FROM (%L) TO (%L)',
            v_name,
            to_char(p_day,     'YYYY-MM-DD') || ' 00:00:00+00',
            to_char(p_day + 1, 'YYYY-MM-DD') || ' 00:00:00+00'
        );
    EXCEPTION
        -- Outro ingestor criou a mesma partição entre o to_regclass e o
        -- CREATE. Não é erro: o que importa é que ela exista.
        WHEN duplicate_table THEN NULL;
    END;

    RETURN v_name;
END;
$fn$;

COMMENT ON FUNCTION monitoring.ensure_partition(date) IS
    'Cria a partição diária de pg_log_event se ela ainda não existir.';


CREATE FUNCTION monitoring.ensure_partition(p_ts timestamptz)
RETURNS text
LANGUAGE sql
AS $fn$
    SELECT monitoring.ensure_partition((p_ts AT TIME ZONE 'UTC')::date);
$fn$;


-- ---------------------------------------------------------------------------
-- Staging -> tabela bruta
-- ---------------------------------------------------------------------------
-- Devolve quantas linhas entraram e quantas já estavam lá. O segundo número
-- não é erro: re-ingerir um arquivo inteiro é uma operação legítima aqui, e o
-- índice único (session_id, line_num, event_time) é o que a torna segura.
CREATE FUNCTION monitoring.ingest_staging(p_file_id bigint)
RETURNS TABLE (loaded bigint, skipped bigint)
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_staged bigint;
    v_loaded bigint;
    v_min    timestamptz;
    v_max    timestamptz;
    v_day    date;
BEGIN
    SELECT count(*), min(s.event_time), max(s.event_time)
      INTO v_staged, v_min, v_max
      FROM monitoring.pg_log_event_staging s
     WHERE s.ingestion_file_id = p_file_id;

    IF v_staged = 0 THEN
        RETURN QUERY SELECT 0::bigint, 0::bigint;
        RETURN;
    END IF;

    -- Um arquivo de log normalmente cobre um dia só, mas atravessa a
    -- meia-noite quando a rotação é por tamanho.
    FOR v_day IN
        SELECT DISTINCT (s.event_time AT TIME ZONE 'UTC')::date
          FROM monitoring.pg_log_event_staging s
         WHERE s.ingestion_file_id = p_file_id
    LOOP
        PERFORM monitoring.ensure_partition(v_day);
    END LOOP;

    WITH inseridos AS (
        INSERT INTO monitoring.pg_log_event (
            event_time, session_id, line_num, ingestion_file_id, record_type,
            duration_ms, pid, leader_pid, remote_port, transaction_id,
            query_id, session_start, username, database_name, application_name,
            backend_type, remote_host, vxid, error_severity, state_code,
            command, message, detail, hint, context, raw_extra
        )
        SELECT
            s.event_time, s.session_id, s.line_num, s.ingestion_file_id,
            s.record_type, s.duration_ms, s.pid, s.leader_pid, s.remote_port,
            s.transaction_id, s.query_id, s.session_start, s.username,
            s.database_name, s.application_name, s.backend_type, s.remote_host,
            s.vxid, s.error_severity, s.state_code, s.command, s.message,
            s.detail, s.hint, s.context, s.raw_extra
        FROM monitoring.pg_log_event_staging s
        WHERE s.ingestion_file_id = p_file_id
        ON CONFLICT (session_id, line_num, event_time) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO v_loaded FROM inseridos;

    DELETE FROM monitoring.pg_log_event_staging s
     WHERE s.ingestion_file_id = p_file_id;

    UPDATE monitoring.ingestion_file f
       SET rows_loaded       = f.rows_loaded  + v_loaded,
           rows_skipped      = f.rows_skipped + (v_staged - v_loaded),
           -- least/greatest ignoram NULL, então a primeira passada preenche.
           first_event_time  = least(f.first_event_time, v_min),
           last_event_time   = greatest(f.last_event_time, v_max)
     WHERE f.id = p_file_id;

    RETURN QUERY SELECT v_loaded, (v_staged - v_loaded);
END;
$fn$;

COMMENT ON FUNCTION monitoring.ingest_staging(bigint) IS
    'Move o lote da staging para pg_log_event, deduplicando, e atualiza os contadores do arquivo.';


-- ---------------------------------------------------------------------------
-- Descarte de corpus
-- ---------------------------------------------------------------------------
-- O motivo de a tabela ser particionada. DROP da partição do dia devolve o
-- espaço na hora; DELETE equivalente levaria minutos e deixaria bloat.
CREATE FUNCTION monitoring.drop_partition(p_day date)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_name text := 'pg_log_event_' || to_char(p_day, 'YYYYMMDD');
BEGIN
    IF to_regclass('monitoring.' || quote_ident(v_name)) IS NULL THEN
        RETURN NULL;
    END IF;
    EXECUTE format('DROP TABLE monitoring.%I', v_name);
    RETURN v_name;
END;
$fn$;
