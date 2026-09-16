-- ---------------------------------------------------------------------------
-- BrokkPG — tabelas derivadas do worker.
--
-- O coletor grava o evento bruto e para. Este schema é o que o worker produz a
-- partir dos registros com record_type = 'plan': o JSON do auto_explain vira
-- linha e árvore de nós.
--
-- Três tabelas, na forma que a pergunta do projeto pede — onde o planner erra a
-- estimativa, onde falta índice, o que custa I/O:
--
--   query           dimensão: uma linha por texto de query distinto
--   plan_execution  fato: uma linha por registro de plano
--   plan_node       uma linha por nó da árvore, com o pai
--
-- Medido no corpus atual: 193.087 planos, média de 2,4 nós por plano
-- (mediana 2, máximo 21, profundidade máxima 12).
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- Partição das tabelas derivadas
-- ---------------------------------------------------------------------------
-- Mesma faixa diária de pg_log_event, de propósito: descartar um corpus é um
-- DROP do dia nas três tabelas, e não um DELETE em cascata.
CREATE FUNCTION monitoring.ensure_partition_de(p_tabela text, p_day date)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_name text := p_tabela || '_' || to_char(p_day, 'YYYYMMDD');
BEGIN
    IF to_regclass('monitoring.' || quote_ident(v_name)) IS NOT NULL THEN
        RETURN v_name;
    END IF;
    BEGIN
        EXECUTE format(
            'CREATE TABLE monitoring.%I PARTITION OF monitoring.%I '
            'FOR VALUES FROM (%L) TO (%L)',
            v_name, p_tabela,
            to_char(p_day,     'YYYY-MM-DD') || ' 00:00:00+00',
            to_char(p_day + 1, 'YYYY-MM-DD') || ' 00:00:00+00'
        );
    EXCEPTION
        WHEN duplicate_table THEN NULL;
    END;
    RETURN v_name;
END;
$fn$;


-- ---------------------------------------------------------------------------
-- 1. Dimensão de query
-- ---------------------------------------------------------------------------
-- A chave é o hash do TEXTO, não o query_id, e isso é deliberado: o query_id
-- do registro de plano é confiável em 96% dos casos, mas não nos 3,8% do
-- regime de portal adiado (ver plan_execution.query_id_origem). O Query Text,
-- esse o plano sempre carrega. Agrupar por texto funciona sempre; o query_id
-- fica como atributo, para juntar com pg_stat_statements quando der.
CREATE TABLE monitoring.query (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    database_name  text NOT NULL,
    -- sha1 do texto com espaços colapsados. Não é a normalização do
    -- pg_stat_statements (que troca literal por $n); é só identidade estável.
    query_hash     text NOT NULL,
    query_text     text NOT NULL,

    -- O id do Postgres, quando o worker conseguiu resolvê-lo sem ambiguidade.
    query_id       bigint,

    criado_em      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_query_hash UNIQUE (database_name, query_hash)
);

-- Esta tabela é identidade e nada mais: sem contadores, sem datas de primeira
-- e última execução. Quem conta é monitoring.query_resumo (05-warehouse.sql),
-- mantida por trigger. Dois contadores para a mesma coisa é como um deles
-- fica errado sem ninguém perceber — foi o que aconteceu na primeira versão,
-- em que um --reprocessar dobrava o número aqui sem duplicar um fato sequer.

CREATE INDEX ix_query_query_id
    ON monitoring.query (query_id) WHERE query_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 2. Fato: uma execução com plano
-- ---------------------------------------------------------------------------
CREATE TABLE monitoring.plan_execution (
    event_id           bigint      NOT NULL,   -- pg_log_event.id
    event_time         timestamptz NOT NULL,

    query_ref          bigint      NOT NULL REFERENCES monitoring.query(id),

    -- Como o query_id foi obtido. Sem esta coluna, 3,8% dos planos entrariam
    -- em qualquer agregação com o id do COMMIT, silenciosamente:
    --   proprio    o registro de plano já trazia o id certo
    --   seguinte   veio do execute/statement seguinte da mesma sessão
    --   indefinido o seguinte era um COMMIT — o id do plano é o do COMMIT, e
    --              o vxid cobre mais de uma query, então não dá para escolher
    query_id_resolvido bigint,
    query_id_origem    text        NOT NULL,

    session_id         text        NOT NULL,
    line_num           bigint      NOT NULL,
    database_name      text,
    username           text,
    application_name   text,
    command            text,

    -- O "duration: X ms" do próprio registro de log.
    duration_ms        double precision,

    -- Forma da árvore
    nos                integer     NOT NULL,
    profundidade       integer     NOT NULL,
    node_type_topo     text,

    -- Estimado x real no nó de topo: a pergunta central do projeto.
    --
    -- As contagens de linha são double precision, não bigint, porque o PG18
    -- passou a emitir "Actual Rows" fracionário: o valor é a média por loop, e
    -- um nó do lado interno de um Nested Loop que devolve 1 linha a cada 3
    -- execuções sai como 0.33. Arredondar aqui jogaria fora justamente a
    -- resolução que torna o número útil em nó com muitos loops.
    custo_estimado     double precision,
    linhas_estimadas   double precision,
    linhas_reais       double precision,
    tempo_total_ms     double precision,

    -- Onde o tempo foi
    shared_hit         bigint,
    shared_read        bigint,
    temp_read          bigint,
    temp_written       bigint,
    io_read_ms         double precision,
    wal_records        bigint,
    wal_bytes          bigint,

    jit                boolean     NOT NULL DEFAULT false,
    -- GUCs de planejamento fora do default: sob qual configuração este plano
    -- foi gerado. Vem "{}" quando nenhum foi mexido.
    settings           jsonb,
    parametros         text,       -- "Query Parameters"

    PRIMARY KEY (event_id, event_time)
) PARTITION BY RANGE (event_time);

COMMENT ON TABLE monitoring.plan_execution IS
    'Uma linha por registro de plano do auto_explain, com os totais do nó de topo.';


-- ---------------------------------------------------------------------------
-- 3. Os nós da árvore
-- ---------------------------------------------------------------------------
CREATE TABLE monitoring.plan_node (
    event_id        bigint      NOT NULL,
    event_time      timestamptz NOT NULL,

    -- Índice em pré-ordem dentro do plano. 0 é o topo.
    node_idx        integer     NOT NULL,
    parent_idx      integer,
    profundidade    integer     NOT NULL,

    node_type       text        NOT NULL,
    parent_rel      text,                   -- "Parent Relationship"
    relation_name   text,
    alias           text,
    index_name      text,
    scan_direction  text,
    join_type       text,
    strategy        text,                   -- Aggregate/SetOp
    operacao        text,                   -- ModifyTable: Insert/Update/Delete

    custo_inicial   double precision,
    custo_total     double precision,
    -- Fracionárias no PG18, e por loop: o total do nó é linhas_reais * loops.
    linhas_est      double precision,       -- "Plan Rows"
    linhas_reais    double precision,       -- "Actual Rows"
    loops           double precision,
    largura         integer,

    tempo_inicial_ms double precision,
    tempo_total_ms   double precision,

    shared_hit      bigint,
    shared_read     bigint,
    temp_read       bigint,
    temp_written    bigint,
    io_read_ms      double precision,
    wal_bytes       bigint,

    paralelo        boolean,
    workers_lancados integer,

    -- As expressões. São o que aponta índice faltando: um Seq Scan com Filter
    -- e muita linha removida é candidato a índice.
    index_cond      text,
    filtro          text,
    linhas_removidas_filtro double precision,
    hash_cond       text,
    sort_key        text,
    sort_method     text,
    sort_espaco_kb  bigint,

    PRIMARY KEY (event_id, event_time, node_idx)
) PARTITION BY RANGE (event_time);

COMMENT ON TABLE monitoring.plan_node IS
    'Um nó da árvore do plano. parent_idx liga ao pai dentro do mesmo event_id.';


-- ---------------------------------------------------------------------------
-- Índices em pg_log_event para o padrão de acesso do worker
-- ---------------------------------------------------------------------------
-- Estes dois são sobre a tabela BRUTA, mas existem por causa do worker — por
-- isso moram aqui, e não em 03-indexes.sql.

-- O worker varre por id buscando só os planos. Sem isto ele lê o índice da PK
-- e descarta o resto: medido, 7.273 linhas removidas por filtro para cada
-- 2.000 planos encontrados.
CREATE INDEX ix_pg_log_event_plano_por_id
    ON monitoring.pg_log_event (id)
    WHERE record_type = 'plan';

-- A correlação plano -> query: "qual o próximo execute/statement desta sessão".
-- O índice único (session_id, line_num, event_time) já serve, mas não carrega
-- o record_type, então cada busca precisa filtrar. Com o tipo no predicado, a
-- primeira entrada encontrada já é a resposta.
CREATE INDEX ix_pg_log_event_fim_de_statement
    ON monitoring.pg_log_event (session_id, line_num)
    WHERE record_type IN ('execute', 'statement');


-- ---------------------------------------------------------------------------
-- Índices das tabelas derivadas
-- ---------------------------------------------------------------------------
CREATE INDEX ix_plan_execution_query
    ON monitoring.plan_execution (query_ref, event_time);

-- Só o que dá para confiar numa agregação por query_id.
CREATE INDEX ix_plan_execution_qid
    ON monitoring.plan_execution (query_id_resolvido, event_time)
    WHERE query_id_origem <> 'indefinido';

CREATE INDEX ix_plan_execution_lenta
    ON monitoring.plan_execution (duration_ms DESC, event_time);

-- "quais Seq Scan em lineitem", "onde tem Sort derramando para disco".
CREATE INDEX ix_plan_node_tipo_rel
    ON monitoring.plan_node (node_type, relation_name, event_time);

CREATE INDEX ix_plan_node_relacao
    ON monitoring.plan_node (relation_name, event_time)
    WHERE relation_name IS NOT NULL;

-- Varredura sequencial que descarta muita linha: candidata a índice. É o
-- recorte que o BrokkPG existe para produzir, então ganha índice próprio.
CREATE INDEX ix_plan_node_candidato_indice
    ON monitoring.plan_node (relation_name, linhas_removidas_filtro DESC)
    WHERE node_type = 'Seq Scan' AND linhas_removidas_filtro > 0;
