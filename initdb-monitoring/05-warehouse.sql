-- ---------------------------------------------------------------------------
-- BrokkPG — o tabelão.
--
-- Uma linha por query, com tudo que se sabe dela já denormalizado: é a tabela
-- onde a IA trabalha, e a intenção é que ela se baste — sem join, sem window
-- function, sem precisar entender o modelo.
--
-- Alimentada por TRIGGER, ao contrário do resto do pipeline. O motivo não é
-- comodidade: é que a trigger é a única forma de o resumo não poder divergir
-- do fato, venha o INSERT de onde vier. E o custo que normalmente condena
-- trigger em carga de lote não se aplica aqui, porque estas são de STATEMENT
-- com transition table — disparam uma vez por INSERT, não uma vez por linha.
-- Medido: 10.000 linhas inseridas, uma única passada de agregação.
--
-- Há um buraco, e ele é explícito: DROP de partição não dispara trigger
-- nenhuma. Por isso monitoring.descartar_dia() existe, e por isso existe
-- monitoring.conferir_resumo().
-- ---------------------------------------------------------------------------

CREATE TABLE monitoring.query_resumo (
    query_ref            bigint PRIMARY KEY
                             REFERENCES monitoring.query(id) ON DELETE CASCADE,

    -- Denormalizado da dimensão de propósito: a tabela tem que se bastar.
    database_name        text NOT NULL,
    query_text           text NOT NULL,
    query_id             bigint,

    -- === Mantido pelas triggers. Sempre vivo. =============================
    -- Só entra aqui o que é agregável de forma incremental: contagem, soma,
    -- mínimo e máximo. Percentil e mediana NÃO são, e por isso não existem
    -- nesta seção — estariam sempre errados.
    execucoes            bigint NOT NULL DEFAULT 0,
    primeiro_visto       timestamptz,
    ultimo_visto         timestamptz,

    duracao_total_ms     double precision NOT NULL DEFAULT 0,
    duracao_min_ms       double precision,
    duracao_max_ms       double precision,
    -- Coluna gerada: não há como a média divergir da soma e da contagem.
    duracao_media_ms     double precision
                             GENERATED ALWAYS AS
                             (duracao_total_ms / nullif(execucoes, 0)) STORED,

    linhas_reais_total   double precision NOT NULL DEFAULT 0,
    linhas_est_total     double precision NOT NULL DEFAULT 0,
    -- >1 o planner subestimou, <1 superestimou.
    fator_estimativa     double precision
                             GENERATED ALWAYS AS
                             (linhas_reais_total
                              / nullif(linhas_est_total, 0)) STORED,

    shared_hit_total     bigint NOT NULL DEFAULT 0,
    shared_read_total    bigint NOT NULL DEFAULT 0,
    temp_written_total   bigint NOT NULL DEFAULT 0,
    io_read_ms_total     double precision NOT NULL DEFAULT 0,
    wal_bytes_total      bigint NOT NULL DEFAULT 0,
    nos_total            bigint NOT NULL DEFAULT 0,

    -- === Preenchido por monitoring.analisar_resumo(). ====================
    -- Vem de plan_node, e não é incremental: exige varrer os nós da query.
    -- analise_em diz quando foi calculado; NULL significa "ainda não".
    analise_em           timestamptz,
    node_types           text[],
    relacoes             text[],
    -- Só os nomes, para filtro rápido: WHERE 'idx_x' = ANY (indices_usados).
    indices_usados       text[],
    -- O detalhe de cada índice: em que relação, varrido como, quantas vezes,
    -- quanto rendeu e sob qual condição. Array de objetos, um por par
    -- (índice, relação). Vazio quando a query não usou índice nenhum — que é
    -- em si a informação mais interessante para quem vai recomendar índice.
    indices              jsonb,
    seq_scans            bigint,
    seq_scan_descartadas double precision,
    sorts_em_disco       bigint,
    pior_fator_no        double precision,
    -- Para descer ao caso concreto: o plano mais lento desta query.
    exemplo_event_id     bigint,
    exemplo_event_time   timestamptz
);

COMMENT ON TABLE monitoring.query_resumo IS
    'Uma linha por query, denormalizada. Colunas até nos_total são mantidas por trigger; as demais por monitoring.analisar_resumo().';

CREATE INDEX ix_query_resumo_caras
    ON monitoring.query_resumo (duracao_total_ms DESC);
CREATE INDEX ix_query_resumo_estimativa
    ON monitoring.query_resumo (fator_estimativa DESC)
    WHERE fator_estimativa IS NOT NULL;
CREATE INDEX ix_query_resumo_banco
    ON monitoring.query_resumo (database_name, duracao_total_ms DESC);


-- ---------------------------------------------------------------------------
-- Recálculo a partir do fato
-- ---------------------------------------------------------------------------
-- A fonte de verdade continua sendo plan_execution. Esta função reconstrói o
-- resumo de um conjunto de queries (ou de todas, com NULL) e é o que fecha os
-- casos que a trigger não cobre: DELETE, DROP de partição, e conferência.
CREATE FUNCTION monitoring.recalcular_resumo(p_refs bigint[] DEFAULT NULL)
RETURNS bigint
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_n bigint;
BEGIN
    WITH alvo AS (
        SELECT q.id, q.database_name, q.query_text, q.query_id
          FROM monitoring.query q
         WHERE p_refs IS NULL OR q.id = ANY (p_refs)
    ),
    agregado AS (
        SELECT a.id, a.database_name, a.query_text, a.query_id,
               count(x.event_id)                          AS execucoes,
               min(x.event_time)                          AS primeiro_visto,
               max(x.event_time)                          AS ultimo_visto,
               coalesce(sum(x.duration_ms), 0)            AS duracao_total_ms,
               min(x.duration_ms)                         AS duracao_min_ms,
               max(x.duration_ms)                         AS duracao_max_ms,
               coalesce(sum(x.linhas_reais), 0)           AS linhas_reais_total,
               coalesce(sum(x.linhas_estimadas), 0)       AS linhas_est_total,
               coalesce(sum(x.shared_hit), 0)             AS shared_hit_total,
               coalesce(sum(x.shared_read), 0)            AS shared_read_total,
               coalesce(sum(x.temp_written), 0)           AS temp_written_total,
               coalesce(sum(x.io_read_ms), 0)             AS io_read_ms_total,
               coalesce(sum(x.wal_bytes), 0)              AS wal_bytes_total,
               coalesce(sum(x.nos), 0)                    AS nos_total
          FROM alvo a
          LEFT JOIN monitoring.plan_execution x ON x.query_ref = a.id
         GROUP BY a.id, a.database_name, a.query_text, a.query_id
    ),
    gravado AS (
        INSERT INTO monitoring.query_resumo AS r (
            query_ref, database_name, query_text, query_id,
            execucoes, primeiro_visto, ultimo_visto,
            duracao_total_ms, duracao_min_ms, duracao_max_ms,
            linhas_reais_total, linhas_est_total,
            shared_hit_total, shared_read_total, temp_written_total,
            io_read_ms_total, wal_bytes_total, nos_total)
        SELECT * FROM agregado
        ON CONFLICT (query_ref) DO UPDATE
           SET database_name      = EXCLUDED.database_name,
               query_text         = EXCLUDED.query_text,
               query_id           = EXCLUDED.query_id,
               execucoes          = EXCLUDED.execucoes,
               primeiro_visto     = EXCLUDED.primeiro_visto,
               ultimo_visto       = EXCLUDED.ultimo_visto,
               duracao_total_ms   = EXCLUDED.duracao_total_ms,
               duracao_min_ms     = EXCLUDED.duracao_min_ms,
               duracao_max_ms     = EXCLUDED.duracao_max_ms,
               linhas_reais_total = EXCLUDED.linhas_reais_total,
               linhas_est_total   = EXCLUDED.linhas_est_total,
               shared_hit_total   = EXCLUDED.shared_hit_total,
               shared_read_total  = EXCLUDED.shared_read_total,
               temp_written_total = EXCLUDED.temp_written_total,
               io_read_ms_total   = EXCLUDED.io_read_ms_total,
               wal_bytes_total    = EXCLUDED.wal_bytes_total,
               nos_total          = EXCLUDED.nos_total
        RETURNING 1
    )
    SELECT count(*) INTO v_n FROM gravado;
    RETURN v_n;
END;
$fn$;

COMMENT ON FUNCTION monitoring.recalcular_resumo(bigint[]) IS
    'Reconstrói as colunas incrementais do resumo a partir de plan_execution. NULL = todas as queries.';


-- ---------------------------------------------------------------------------
-- Uso de índice, com a relação resolvida
-- ---------------------------------------------------------------------------
-- Existe porque o EXPLAIN não diz a tabela de um Bitmap Index Scan: medido no
-- corpus, 3.704 nós de índice sem "Relation Name", todos eles bitmap. A tabela
-- está no nó de cima — no Bitmap Heap Scan (3.360 casos) ou, quando há um
-- BitmapAnd/BitmapOr no meio, no de cima dele (344 casos). Pai e avô cobrem
-- 100%; por isso são dois LEFT JOIN e não um WITH RECURSIVE.
--
-- Sem isto, "quais índices esta query usa" perde justamente os bitmap, que são
-- os que mais interessam a quem vai decidir se um índice vale a pena.
CREATE VIEW monitoring.v_uso_de_indice AS
SELECT n.event_id,
       n.event_time,
       n.node_idx,
       n.index_name AS indice,
       coalesce(n.relation_name, p.relation_name, a.relation_name) AS relacao,
       n.node_type  AS tipo_no,
       n.scan_direction,
       n.index_cond,
       n.linhas_est,
       n.linhas_reais,
       n.loops,
       n.tempo_total_ms,
       n.shared_hit,
       n.shared_read
  FROM monitoring.plan_node n
  LEFT JOIN monitoring.plan_node p
    ON p.event_id = n.event_id AND p.event_time = n.event_time
   AND p.node_idx = n.parent_idx
  LEFT JOIN monitoring.plan_node a
    ON a.event_id = n.event_id AND a.event_time = n.event_time
   AND a.node_idx = p.parent_idx
 WHERE n.index_name IS NOT NULL;

COMMENT ON VIEW monitoring.v_uso_de_indice IS
    'Um nó de varredura por índice, com a relação resolvida subindo até o pai ou o avô (o EXPLAIN não a informa em Bitmap Index Scan).';


-- ---------------------------------------------------------------------------
-- Análise da forma dos planos
-- ---------------------------------------------------------------------------
-- Estas colunas não são incrementais: exigem varrer plan_node. Ficam separadas
-- da parte que a trigger mantém, e analise_em diz de quando são.
CREATE FUNCTION monitoring.analisar_resumo(p_refs bigint[] DEFAULT NULL)
RETURNS bigint
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_n bigint;
BEGIN
    WITH alvo AS (
        SELECT query_ref FROM monitoring.query_resumo
         WHERE p_refs IS NULL OR query_ref = ANY (p_refs)
    ),
    nos AS (
        SELECT a.query_ref,
               array_agg(DISTINCT n.node_type ORDER BY n.node_type)
                 AS node_types,
               array_agg(DISTINCT n.relation_name)
                 FILTER (WHERE n.relation_name IS NOT NULL) AS relacoes,
               array_agg(DISTINCT n.index_name)
                 FILTER (WHERE n.index_name IS NOT NULL)    AS indices_usados,
               count(*) FILTER (WHERE n.node_type = 'Seq Scan')  AS seq_scans,
               coalesce(sum(n.linhas_removidas_filtro)
                        FILTER (WHERE n.node_type = 'Seq Scan'), 0)
                 AS seq_scan_descartadas,
               count(*) FILTER (WHERE n.sort_method = 'external merge')
                 AS sorts_em_disco,
               max((n.linhas_reais * n.loops + 1.0)
                 / (n.linhas_est * n.loops + 1.0))          AS pior_fator_no
          FROM alvo a
          JOIN monitoring.plan_execution x ON x.query_ref = a.query_ref
          JOIN monitoring.plan_node n
            ON n.event_id = x.event_id AND n.event_time = x.event_time
         GROUP BY a.query_ref
    ),
    -- Um agregado por par (índice, relação): o mesmo índice pode ser varrido
    -- de mais de um jeito na mesma query — idx_lineitem_partkey aparece como
    -- Index Scan e como Bitmap Index Scan no corpus.
    uso_idx AS (
        SELECT a.query_ref, u.indice, u.relacao,
               array_agg(DISTINCT u.tipo_no ORDER BY u.tipo_no)     AS tipos,
               count(*)                                             AS usos,
               round(avg(u.linhas_reais * u.loops)::numeric, 1)     AS linhas_media,
               round(sum(u.tempo_total_ms)::numeric, 1)             AS ms_total,
               min(u.index_cond)                                    AS cond
          FROM alvo a
          JOIN monitoring.plan_execution x ON x.query_ref = a.query_ref
          JOIN monitoring.v_uso_de_indice u
            ON u.event_id = x.event_id AND u.event_time = x.event_time
         GROUP BY a.query_ref, u.indice, u.relacao
    ),
    idx_json AS (
        SELECT query_ref,
               jsonb_agg(jsonb_build_object(
                   'indice',       indice,
                   'relacao',      relacao,
                   'tipos',        tipos,
                   'usos',         usos,
                   'linhas_media', linhas_media,
                   'ms_total',     ms_total,
                   'cond',         cond
               ) ORDER BY usos DESC) AS indices
          FROM uso_idx GROUP BY query_ref
    ),
    exemplo AS (
        SELECT DISTINCT ON (x.query_ref)
               x.query_ref, x.event_id, x.event_time
          FROM alvo a
          JOIN monitoring.plan_execution x ON x.query_ref = a.query_ref
         ORDER BY x.query_ref, x.duration_ms DESC NULLS LAST
    ),
    gravado AS (
        UPDATE monitoring.query_resumo r
           SET analise_em           = now(),
               node_types           = n.node_types,
               relacoes             = n.relacoes,
               indices_usados       = n.indices_usados,
               -- '[]' e não NULL quando a query não usou índice: a ausência é
               -- um fato medido, não uma informação que falta.
               indices              = coalesce(j.indices, '[]'::jsonb),
               seq_scans            = n.seq_scans,
               seq_scan_descartadas = n.seq_scan_descartadas,
               sorts_em_disco       = n.sorts_em_disco,
               pior_fator_no        = n.pior_fator_no,
               exemplo_event_id     = e.event_id,
               exemplo_event_time   = e.event_time
          FROM nos n
          LEFT JOIN exemplo e  ON e.query_ref = n.query_ref
          LEFT JOIN idx_json j ON j.query_ref = n.query_ref
         WHERE r.query_ref = n.query_ref
        RETURNING 1
    )
    SELECT count(*) INTO v_n FROM gravado;
    RETURN v_n;
END;
$fn$;


-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------

-- INSERT: incremental. A transition table já vem contendo APENAS as linhas
-- que o ON CONFLICT DO NOTHING deixou entrar — verificado — então reprocessar
-- o corpus inteiro não move um número sequer aqui.
CREATE FUNCTION monitoring.trg_resumo_inserido()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    INSERT INTO monitoring.query_resumo AS r (
        query_ref, database_name, query_text, query_id,
        execucoes, primeiro_visto, ultimo_visto,
        duracao_total_ms, duracao_min_ms, duracao_max_ms,
        linhas_reais_total, linhas_est_total,
        shared_hit_total, shared_read_total, temp_written_total,
        io_read_ms_total, wal_bytes_total, nos_total)
    SELECT n.query_ref, q.database_name, q.query_text, q.query_id,
           count(*), min(n.event_time), max(n.event_time),
           coalesce(sum(n.duration_ms), 0),
           min(n.duration_ms), max(n.duration_ms),
           coalesce(sum(n.linhas_reais), 0), coalesce(sum(n.linhas_estimadas), 0),
           coalesce(sum(n.shared_hit), 0), coalesce(sum(n.shared_read), 0),
           coalesce(sum(n.temp_written), 0), coalesce(sum(n.io_read_ms), 0),
           coalesce(sum(n.wal_bytes), 0), coalesce(sum(n.nos), 0)
      FROM novas n
      JOIN monitoring.query q ON q.id = n.query_ref
     GROUP BY n.query_ref, q.database_name, q.query_text, q.query_id
    ON CONFLICT (query_ref) DO UPDATE
       SET execucoes          = r.execucoes          + EXCLUDED.execucoes,
           primeiro_visto     = least(r.primeiro_visto, EXCLUDED.primeiro_visto),
           ultimo_visto       = greatest(r.ultimo_visto, EXCLUDED.ultimo_visto),
           duracao_total_ms   = r.duracao_total_ms   + EXCLUDED.duracao_total_ms,
           duracao_min_ms     = least(r.duracao_min_ms, EXCLUDED.duracao_min_ms),
           duracao_max_ms     = greatest(r.duracao_max_ms, EXCLUDED.duracao_max_ms),
           linhas_reais_total = r.linhas_reais_total + EXCLUDED.linhas_reais_total,
           linhas_est_total   = r.linhas_est_total   + EXCLUDED.linhas_est_total,
           shared_hit_total   = r.shared_hit_total   + EXCLUDED.shared_hit_total,
           shared_read_total  = r.shared_read_total  + EXCLUDED.shared_read_total,
           temp_written_total = r.temp_written_total + EXCLUDED.temp_written_total,
           io_read_ms_total   = r.io_read_ms_total   + EXCLUDED.io_read_ms_total,
           wal_bytes_total    = r.wal_bytes_total    + EXCLUDED.wal_bytes_total,
           nos_total          = r.nos_total          + EXCLUDED.nos_total;
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER resumo_inserido
    AFTER INSERT ON monitoring.plan_execution
    REFERENCING NEW TABLE AS novas
    FOR EACH STATEMENT
    EXECUTE FUNCTION monitoring.trg_resumo_inserido();


-- DELETE: NÃO é incremental. Subtrair da soma seria fácil, mas mínimo e máximo
-- não voltam — se a linha removida era a mais lenta, o novo máximo só se
-- descobre varrendo o resto. Então recalcula por inteiro as queries afetadas,
-- que são poucas.
CREATE FUNCTION monitoring.trg_resumo_removido()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_refs bigint[];
BEGIN
    SELECT array_agg(DISTINCT query_ref) INTO v_refs FROM antigas;
    IF v_refs IS NOT NULL THEN
        PERFORM monitoring.recalcular_resumo(v_refs);
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER resumo_removido
    AFTER DELETE ON monitoring.plan_execution
    REFERENCING OLD TABLE AS antigas
    FOR EACH STATEMENT
    EXECUTE FUNCTION monitoring.trg_resumo_removido();


-- TRUNCATE não tem transition table: não há como saber o que sumiu. Como o
-- efeito é sempre "sumiu tudo", zera o resumo inteiro.
CREATE FUNCTION monitoring.trg_resumo_truncado()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    PERFORM monitoring.recalcular_resumo(NULL);
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER resumo_truncado
    AFTER TRUNCATE ON monitoring.plan_execution
    FOR EACH STATEMENT
    EXECUTE FUNCTION monitoring.trg_resumo_truncado();


-- ---------------------------------------------------------------------------
-- O buraco das triggers: DROP de partição
-- ---------------------------------------------------------------------------
-- Verificado: DROP TABLE de uma partição não dispara trigger alguma. O resumo
-- continuaria contando execuções de linhas que não existem mais. Descartar um
-- dia tem que passar por aqui.
CREATE FUNCTION monitoring.descartar_dia(p_day date)
RETURNS TABLE (tabela text, removida boolean)
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_nome text;
    v_alvo text;
BEGIN
    FOREACH v_alvo IN ARRAY ARRAY['plan_node', 'plan_execution', 'pg_log_event']
    LOOP
        v_nome := v_alvo || '_' || to_char(p_day, 'YYYYMMDD');
        IF to_regclass('monitoring.' || quote_ident(v_nome)) IS NOT NULL THEN
            EXECUTE format('DROP TABLE monitoring.%I', v_nome);
            tabela := v_nome; removida := true;
        ELSE
            tabela := v_nome; removida := false;
        END IF;
        RETURN NEXT;
    END LOOP;

    -- O que a trigger não viu, o recálculo conserta.
    PERFORM monitoring.recalcular_resumo(NULL);
    PERFORM monitoring.analisar_resumo(NULL);
END;
$fn$;

COMMENT ON FUNCTION monitoring.descartar_dia(date) IS
    'Descarta um dia nas três tabelas particionadas e reconstrói o resumo. Use isto, não DROP TABLE direto.';


-- ---------------------------------------------------------------------------
-- Conferência
-- ---------------------------------------------------------------------------
-- Um resumo mantido por trigger que ninguém confere é um resumo em que não se
-- pode confiar. Devolve zero linha quando está tudo certo.
CREATE FUNCTION monitoring.conferir_resumo()
RETURNS TABLE (query_ref bigint, campo text,
               no_resumo double precision, no_fato double precision)
LANGUAGE sql
AS $fn$
    WITH fato AS (
        SELECT x.query_ref,
               count(*)::double precision      AS execucoes,
               coalesce(sum(x.duration_ms), 0) AS total,
               min(x.duration_ms)              AS menor,
               max(x.duration_ms)              AS maior
          FROM monitoring.plan_execution x
         GROUP BY x.query_ref
    )
    SELECT r.query_ref, campo, no_resumo, no_fato
      FROM monitoring.query_resumo r
      LEFT JOIN fato f ON f.query_ref = r.query_ref
      CROSS JOIN LATERAL (VALUES
          ('execucoes', r.execucoes::double precision, coalesce(f.execucoes, 0)),
          ('duracao_total_ms', r.duracao_total_ms, coalesce(f.total, 0)),
          ('duracao_min_ms', r.duracao_min_ms, f.menor),
          ('duracao_max_ms', r.duracao_max_ms, f.maior)
      ) AS v(campo, no_resumo, no_fato)
     WHERE no_resumo IS DISTINCT FROM no_fato
       -- Soma de ponto flutuante em ordem diferente não bate no último bit.
       AND (no_resumo IS NULL OR no_fato IS NULL
            OR abs(no_resumo - no_fato) > 0.001 * greatest(abs(no_fato), 1));
$fn$;


-- monitoring.drop_partition(), definida em 02-functions.sql, ficou obsoleta:
-- ela derruba só a partição de pg_log_event e não toca nas derivadas nem no
-- resumo. Marcada aqui em vez de removida porque remover função é o tipo de
-- mudança que quebra script de quem já usava.
COMMENT ON FUNCTION monitoring.drop_partition(date) IS
    'OBSOLETA: derruba só pg_log_event e deixa query_resumo divergente. Use monitoring.descartar_dia().';
