-- ---------------------------------------------------------------------------
-- BrokkPG — schema de destino dos eventos do jsonlog.
--
-- Roda na PRIMEIRA subida do container sindri_monitoring (banco vazio). Não
-- roda de novo enquanto o volume monitordata existir; para reaplicar:
--
--     docker compose rm -sf monitoring && docker volume rm sindri_monitordata
--
-- Este banco é uma instância separada do Postgres observado de propósito. O
-- observado sobe com log_min_duration_statement=0 e
-- auto_explain.log_min_duration=0: se a ingestão gravasse ali, cada COPY do
-- ingestor viraria registro de log, que na passada seguinte seria ingerido,
-- gerando mais log. O corpus viraria o retrato do próprio coletor.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS monitoring;

-- Para observar o próprio ingestor: é o que diz se a ingestão está lenta por
-- I/O, por manutenção de índice ou pelo parse do JSON do lado do Python.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;


-- ---------------------------------------------------------------------------
-- 1. Controle dos arquivos de log
-- ---------------------------------------------------------------------------
-- O arquivo corrente do Postgres observado ainda está sendo escrito quando o
-- ingestor o lê: a rotação é por 50MB/1d, então há sempre um arquivo aberto.
-- Por isso o controle é por OFFSET (bytes_ingested) e não por um status
-- binário — sem ele, tudo que o servidor grava depois da leitura se perde.
CREATE TABLE monitoring.ingestion_file (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    file_name        text   NOT NULL,
    file_path        text   NOT NULL,

    -- Tamanho observado na última passada. Comparado com o tamanho atual do
    -- arquivo, é o que diz se há coisa nova para ler.
    file_size_bytes  bigint NOT NULL,
    -- Deslocamento, em bytes, do fim do último objeto JSON lido por inteiro.
    -- json.JSONDecoder().raw_decode devolve esse número de graça.
    bytes_ingested   bigint NOT NULL DEFAULT 0,

    -- sha256 dos primeiros 64 KB. Hash do arquivo inteiro não serve aqui: o
    -- arquivo cresce e o hash mudaria a cada passada. O começo nunca muda,
    -- então isto detecta nome de arquivo reaproveitado com outro conteúdo.
    head_fingerprint text,

    -- true quando o Postgres já rotacionou para o arquivo seguinte, isto é,
    -- quando este aqui não cresce mais.
    rotated          boolean NOT NULL DEFAULT false,

    status           text   NOT NULL DEFAULT 'pending',

    rows_loaded      bigint NOT NULL DEFAULT 0,
    -- Linhas que já estavam no banco (o ON CONFLICT DO NOTHING pegou). Não é
    -- erro: é o que torna re-ingerir um arquivo uma operação segura.
    rows_skipped     bigint NOT NULL DEFAULT 0,
    rows_failed      bigint NOT NULL DEFAULT 0,

    first_event_time timestamptz,
    last_event_time  timestamptz,

    started_at       timestamptz,
    finished_at      timestamptz,
    error_message    text,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_ingestion_file_path UNIQUE (file_path),
    CONSTRAINT ck_ingestion_file_status CHECK (status IN (
        'pending',      -- descoberto, nada lido
        'processing',   -- passada em andamento
        'partial',      -- lido até bytes_ingested; o arquivo ainda cresce
        'completed',    -- rotacionado e lido até o fim
        'failed'
    ))
);

COMMENT ON TABLE monitoring.ingestion_file IS
    'Um registro por arquivo de log do Postgres observado, com o offset já lido.';


-- ---------------------------------------------------------------------------
-- 2. Eventos brutos
-- ---------------------------------------------------------------------------
-- Particionada por dia. A razão não é o tamanho, é o descarte: o ciclo de
-- trabalho aqui é rodar benchmark, medir e jogar o corpus fora. DROP da
-- partição do dia é instantâneo; DELETE de meio milhão de linhas não é, e
-- ainda deixa bloat para o autovacuum recolher.
--
-- Medido no corpus atual (949 MB, 23 arquivos): 567.811 eventos.
CREATE TABLE monitoring.pg_log_event (
    id                bigint GENERATED ALWAYS AS IDENTITY,

    event_time        timestamptz NOT NULL,   -- "timestamp" do jsonlog

    -- Chave natural do registro. session_id é o %c do Postgres (início da
    -- sessão em hex + pid em hex) e line_num é um contador por sessão, então
    -- o par é único no servidor inteiro — 13.338 de 13.338 distintos na
    -- amostra medida. É o que permite re-ingerir arquivo sem duplicar nada.
    --
    -- line_num é NOT NULL porque não é só identidade: é a chave da correlação
    -- plano -> query. A ordem real no log é bind -> plano(s) -> execute, e o
    -- execute imediatamente seguinte carrega o query_id correto (3.673 pares
    -- verificados no corpus, zero divergência). Correlacionar por vxid não
    -- funciona: vxid é a transação inteira, e 95,6% dos pares
    -- (session_id, vxid) cobrem mais de um query_id.
    session_id        text   NOT NULL,
    line_num          bigint NOT NULL,

    ingestion_file_id bigint NOT NULL REFERENCES monitoring.ingestion_file(id),

    -- Classificado no ingestor por regex no prefixo do message. Sem isto,
    -- qualquer avg(duration_ms) mistura o tempo de um bind (0,04 ms) com o de
    -- um plano e com o de um execute — e 78% dos registros do corpus são
    -- parse/bind/execute, contra 21,4% de plano.
    record_type       text   NOT NULL,
    -- O "duration: X ms" do prefixo, já convertido.
    duration_ms       double precision,

    -- Largura fixa agrupada: economiza o padding de alinhamento que o Postgres
    -- insere quando um int8 cai depois de um text.
    pid               integer,
    leader_pid        integer,                -- só em worker paralelo
    remote_port       integer,
    transaction_id    bigint,                 -- "txid"
    -- Vem 0 (nunca NULL) em 25,8% dos registros. Ver o índice parcial em
    -- 03-indexes.sql: o filtro tem que ser <> 0; IS NOT NULL não exclui nada.
    query_id          bigint,
    session_start     timestamptz,

    username          text,                   -- "user"
    database_name     text,                   -- "dbname"
    application_name  text,
    backend_type      text,
    remote_host       text,
    vxid              text,
    error_severity    text,
    state_code        text,                   -- SQLSTATE; só aparece em erro
    command           text,                   -- "ps"

    -- Já sem o prefixo "duration: X ms  ": o que sobra é o payload (o SQL ou o
    -- JSON do plano do auto_explain). O plano NÃO é parseado na ingestão — o
    -- ingestor é burro de propósito, quem interpreta é o worker.
    message           text,
    detail            text,
    hint              text,
    context           text,

    -- Só as chaves do jsonlog que não têm coluna acima. Quase sempre NULL: o
    -- corpus inteiro tem 19 chaves distintas e todas as relevantes já estão
    -- mapeadas. Guardar o evento inteiro em jsonb custaria caro à toa —
    -- message sozinho é 63,4% dos bytes de um evento, e ficaria duplicado.
    raw_extra         jsonb,

    created_at        timestamptz NOT NULL DEFAULT now(),

    -- A PK de tabela particionada tem que conter a chave de partição.
    PRIMARY KEY (id, event_time),

    CONSTRAINT ck_pg_log_event_type CHECK (record_type IN (
        'plan', 'execute', 'bind', 'parse', 'statement', 'other'
    ))
) PARTITION BY RANGE (event_time);

COMMENT ON TABLE monitoring.pg_log_event IS
    'Evento bruto do jsonlog, um por registro do log. Fonte de verdade do worker.';


-- ---------------------------------------------------------------------------
-- 3. Staging da ingestão
-- ---------------------------------------------------------------------------
-- COPY não aceita ON CONFLICT. Para ter a idempotência que o índice único dá,
-- o caminho é COPY para cá e depois INSERT ... SELECT ... ON CONFLICT DO
-- NOTHING (é o que monitoring.ingest_staging faz).
--
-- UNLOGGED: o conteúdo é descartável por construção — se o banco cair no meio
-- da carga, o arquivo é relido a partir do offset anterior. Não gerar WAL aqui
-- corta boa parte do custo de escrita da ingestão.
--
-- ingestion_file_id fica na staging (e não só como argumento da função) para
-- que dois ingestores rodando ao mesmo tempo não misturem os lotes um do outro.
CREATE UNLOGGED TABLE monitoring.pg_log_event_staging (
    ingestion_file_id bigint NOT NULL,
    event_time        timestamptz,
    session_id        text,
    line_num          bigint,
    record_type       text,
    duration_ms       double precision,
    pid               integer,
    leader_pid        integer,
    remote_port       integer,
    transaction_id    bigint,
    query_id          bigint,
    session_start     timestamptz,
    username          text,
    database_name     text,
    application_name  text,
    backend_type      text,
    remote_host       text,
    vxid              text,
    error_severity    text,
    state_code        text,
    command           text,
    message           text,
    detail            text,
    hint              text,
    context           text,
    raw_extra         jsonb
);

-- A staging é esvaziada a cada lote, então o gatilho padrão (20% da tabela)
-- demoraria a disparar e ela acumularia bloat entre um vacuum e outro.
ALTER TABLE monitoring.pg_log_event_staging SET (
    autovacuum_vacuum_scale_factor = 0.0,
    autovacuum_vacuum_threshold    = 10000
);


-- ---------------------------------------------------------------------------
-- 4. Progresso do worker
-- ---------------------------------------------------------------------------
-- Substitui a coluna "processed boolean" na tabela bruta. Marcar
-- processed=true em ~570 mil linhas é reescrever a maior tabela do banco a
-- cada passada: dobra o tamanho até o vacuum recolher, e um índice parcial
-- WHERE processed=false sofreria churn total.
--
-- O avanço é por ingestion_file_id, não por id de evento: um arquivo só fica
-- visível ao worker quando tem o offset gravado, e isso continua correto se um
-- dia o ingestor rodar em paralelo (com inserts concorrentes um id maior pode
-- commitar antes de um menor, e marca d'água por id puraria linhas).
CREATE TABLE monitoring.worker_progress (
    worker_name      text PRIMARY KEY,
    last_file_id     bigint NOT NULL DEFAULT 0,
    last_event_id    bigint NOT NULL DEFAULT 0,
    events_processed bigint NOT NULL DEFAULT 0,
    last_run_at      timestamptz,
    last_error       text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
