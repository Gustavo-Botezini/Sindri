-- ---------------------------------------------------------------------------
-- BrokkPG — o laboratório de índices.
--
-- Etapa 3, modo de clusters: os vetores das queries, o resultado de cada
-- rodada de clusterização e o que a bancada mediu ao criar um índice de
-- verdade e reexecutar as queries do cluster.
--
-- Ao contrário de initdb-monitoring/*.sql, este arquivo NÃO roda só na
-- primeira subida do container. Ele é aplicado pelo próprio pacote, em toda
-- execução, e por isso é inteiro IF NOT EXISTS.
--
-- A razão é concreta: initdb só roda em banco vazio, e recriar o volume
-- monitordata custaria os 193.087 planos já coletados. Uma cópia deste
-- arquivo vive em initdb-monitoring/07-index-lab.sql para volumes novos —
-- mesmo conteúdo, dois caminhos, sem DDL duplicado em lugar nenhum.
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 1. Os vetores
-- ---------------------------------------------------------------------------
-- Um embedding por (query, modelo, normalização). A chave composta existe
-- porque trocar a normalização muda o texto que foi embedado: com PK só em
-- query_ref, uma rodada sem normalizar reaproveitaria silenciosamente o vetor
-- da rodada normalizada, e a comparação entre as duas viraria ficção.
CREATE TABLE IF NOT EXISTS monitoring.query_embedding (
    query_ref    bigint NOT NULL
                     REFERENCES monitoring.query(id) ON DELETE CASCADE,
    modelo       text   NOT NULL,
    -- 'bruto' | 'espaco' | 'espaco+caixa'
    normalizacao text   NOT NULL,

    dimensoes    integer NOT NULL,
    -- sha1 do texto JÁ normalizado. É ele que responde "precisa reembedar?".
    -- Comparar com query.query_hash não serviria: aquele é do texto cru.
    texto_hash   text   NOT NULL,
    -- real[] e não double precision[]: o modelo entrega float32, e guardar em
    -- float64 dobraria o espaço para acrescentar zeros.
    vetor        real[] NOT NULL,

    gerado_em    timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (query_ref, modelo, normalizacao)
);

COMMENT ON TABLE monitoring.query_embedding IS
    'Vetor denso do texto da query. Cache: so reembeda quando texto_hash muda.';


-- ---------------------------------------------------------------------------
-- 2. A rodada de clusterização
-- ---------------------------------------------------------------------------
-- Uma linha por execução de "agrupar". Existe por um motivo que o corpus
-- impõe: 117 queries é pouco para UMAP+HDBSCAN ser estável, e o resultado
-- muda com n_neighbors e min_cluster_size. Sem os parâmetros gravados ao lado
-- do resultado, um cluster é um número que apareceu; com eles, é um
-- experimento que outra pessoa refaz.
CREATE TABLE IF NOT EXISTS monitoring.rodada_cluster (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    database_name text NOT NULL,
    modelo        text NOT NULL,
    normalizacao  text NOT NULL,

    queries       integer NOT NULL,
    clusters      integer NOT NULL,
    ruido         integer NOT NULL,

    -- n_neighbors, min_dist, n_components, metric, min_cluster_size,
    -- min_samples, cluster_selection_method, semente.
    parametros    jsonb NOT NULL,

    criado_em     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE monitoring.rodada_cluster IS
    'Uma execucao de agrupar, com os parametros que produziram o resultado.';


-- ---------------------------------------------------------------------------
-- 3. Em que cluster cada query caiu
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.query_cluster (
    rodada_id     bigint NOT NULL
                      REFERENCES monitoring.rodada_cluster(id) ON DELETE CASCADE,
    query_ref     bigint NOT NULL
                      REFERENCES monitoring.query(id) ON DELETE CASCADE,

    -- -1 é ruído do HDBSCAN, e fica gravado como ruído. Não se inventa índice
    -- para um ponto que o algoritmo se recusou a agrupar.
    cluster_id    integer NOT NULL,
    probabilidade double precision,
    outlier       double precision,

    -- Projeção 2D, só para o gráfico. A clusterização roda em n_components
    -- dimensões (5 por padrão), não nestas duas.
    x             double precision,
    y             double precision,

    PRIMARY KEY (rodada_id, query_ref)
);

CREATE INDEX IF NOT EXISTS ix_query_cluster_rodada
    ON monitoring.query_cluster (rodada_id, cluster_id);


-- ---------------------------------------------------------------------------
-- 3b. Quais queries a bancada consegue reexecutar
-- ---------------------------------------------------------------------------
-- Nem tudo que o auto_explain registra roda fora do contexto em que rodou.
-- Medido na primeira rodada completa, 4 de 24 queries falharam, em três
-- classes — e duas delas não dava para prever lendo o texto:
--
--   view temporária   a Q15 do TPC-H cria a view revenue0, consulta e derruba.
--                     O corpus guardou só o SELECT, que sozinho não resolve.
--   corpo de função   query de dentro de uma função PL/pgSQL: o que parece
--                     coluna (p_custkey, p_nationkey) é argumento da função.
--                     Pior: em TPC-H "p_" é o prefixo das colunas de part,
--                     então nenhuma regra sintática distingue os dois casos.
--   tipo indecidível  parâmetro em posição que o PREPARE não infere, como
--                     concat($1, ' day')::interval, onde concat é variádica.
--
-- A terceira o fallback de literais resolve. As duas primeiras, não — e como
-- nenhuma regex as separa com segurança, quem responde é o banco: um EXPLAIN
-- sem ANALYZE, que planeja e não executa. O resultado mora aqui para não
-- refazer a pergunta a cada rodada, e para que essas queries parem de ocupar
-- vaga num cluster que a bancada nunca vai conseguir medir.
CREATE TABLE IF NOT EXISTS monitoring.query_replay (
    query_ref   bigint PRIMARY KEY
                    REFERENCES monitoring.query(id) ON DELETE CASCADE,
    replicavel  boolean NOT NULL,
    -- 'direto' | 'prepare' | 'literais': como a bancada conseguiu montar o
    -- comando. Serve para saber quantas dependem do fallback.
    via         text,
    erro        text,
    conferido_em timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE monitoring.query_replay IS
    'Se a bancada consegue reexecutar a query fora do contexto original.';


-- ---------------------------------------------------------------------------
-- 4. O índice candidato de um cluster
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.experimento (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    rodada_id     bigint NOT NULL
                      REFERENCES monitoring.rodada_cluster(id) ON DELETE CASCADE,
    cluster_id    integer NOT NULL,

    -- De qual query do cluster o candidato saiu. É o que permite separar
    -- "melhorou a query de origem" de "melhorou o cluster" — a hipótese em
    -- teste é a segunda, e ela pode ser falsa.
    query_origem  bigint REFERENCES monitoring.query(id),
    origem        text   NOT NULL,   -- 'heuristica' | 'claude'

    ddl           text   NOT NULL,
    relacao       text,
    justificativa text,
    -- Os candidatos que a heurística ofereceu, para conferir o que foi
    -- descartado junto com o que foi escolhido.
    candidatos    jsonb,

    -- O que a bancada mediu do índice em si.
    build_ms      double precision,
    tamanho_bytes bigint,

    estado        text   NOT NULL DEFAULT 'proposto',
    erro          text,
    criado_em     timestamptz NOT NULL DEFAULT now(),
    medido_em     timestamptz,

    CONSTRAINT ck_experimento_origem CHECK (origem IN ('heuristica', 'claude')),
    CONSTRAINT ck_experimento_estado CHECK (estado IN (
        'proposto',   -- o candidato existe, a bancada ainda não rodou
        'medido',
        'recusado',   -- não havia candidato que valesse medir; ddl fica vazio
        'falhou'      -- o CREATE INDEX ou o replay quebrou; veja erro
    ))
);

-- O estado 'recusado' chegou depois das primeiras rodadas. Como este arquivo
-- roda em banco que já existe, o CHECK acima não é reaplicado sozinho — o
-- CREATE TABLE IF NOT EXISTS não toca em tabela existente. Daí o par abaixo.
ALTER TABLE monitoring.experimento
    DROP CONSTRAINT IF EXISTS ck_experimento_estado;
ALTER TABLE monitoring.experimento
    ADD CONSTRAINT ck_experimento_estado CHECK (estado IN (
        'proposto', 'medido', 'recusado', 'falhou'));

CREATE INDEX IF NOT EXISTS ix_experimento_rodada
    ON monitoring.experimento (rodada_id, cluster_id);


-- ---------------------------------------------------------------------------
-- 5. Uma medição: uma query, uma fase, uma repetição
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.experimento_medida (
    experimento_id bigint  NOT NULL
                       REFERENCES monitoring.experimento(id) ON DELETE CASCADE,
    query_ref      bigint  NOT NULL
                       REFERENCES monitoring.query(id) ON DELETE CASCADE,
    fase           text    NOT NULL,
    repeticao      integer NOT NULL,

    tempo_ms       double precision,
    custo_estimado double precision,
    linhas_reais   double precision,
    shared_hit     bigint,
    shared_read    bigint,

    -- A coluna que separa "melhorou" de "melhorou POR CAUSA deste índice".
    -- Se o plano de 'depois' não cita o índice novo, a diferença é variação
    -- de cache e tem que aparecer como tal em vez de virar ganho.
    usou_indice    boolean,
    node_type_topo text,

    erro           text,

    PRIMARY KEY (experimento_id, query_ref, fase, repeticao),
    CONSTRAINT ck_medida_fase CHECK (fase IN ('antes', 'depois'))
);


-- ---------------------------------------------------------------------------
-- 6. O veredito
-- ---------------------------------------------------------------------------
-- Mediana, e não média: a primeira repetição é descartada e as que sobram
-- são poucas — média com um pico de I/O no meio não diz nada.
--
-- DROP antes de CREATE, e não CREATE OR REPLACE: trocar o conjunto de colunas
-- de uma view com OR REPLACE é erro, e estas duas ainda vão mudar.
DROP VIEW IF EXISTS monitoring.experimento_veredito;
DROP VIEW IF EXISTS monitoring.experimento_resultado;

CREATE VIEW monitoring.experimento_resultado AS
WITH util AS (
    -- repeticao = 1 é aquecimento de cache e não entra na conta.
    SELECT experimento_id, query_ref, fase, tempo_ms, usou_indice
      FROM monitoring.experimento_medida
     WHERE repeticao > 1 AND erro IS NULL AND tempo_ms IS NOT NULL
),
por_fase AS (
    SELECT experimento_id, query_ref, fase,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY tempo_ms) AS mediana_ms,
           min(tempo_ms)        AS min_ms,
           max(tempo_ms)        AS max_ms,
           count(*)             AS amostras,
           bool_or(usou_indice) AS usou_indice
      FROM util
     GROUP BY 1, 2, 3
)
SELECT e.rodada_id,
       e.cluster_id,
       e.id      AS experimento_id,
       e.relacao,
       e.ddl,
       a.query_ref,
       (e.query_origem = a.query_ref)                   AS e_a_origem,
       round(a.mediana_ms::numeric, 1)                  AS antes_ms,
       round(d.mediana_ms::numeric, 1)                  AS depois_ms,
       round((a.mediana_ms - d.mediana_ms)::numeric, 1) AS ganho_ms,
       round((100.0 * (a.mediana_ms - d.mediana_ms)
              / nullif(a.mediana_ms, 0))::numeric, 1)   AS ganho_pct,
       d.usou_indice,
       a.amostras AS amostras_antes,
       d.amostras AS amostras_depois
  FROM monitoring.experimento e
  JOIN por_fase a ON a.experimento_id = e.id AND a.fase = 'antes'
  JOIN por_fase d ON d.experimento_id = e.id AND d.fase = 'depois'
                 AND d.query_ref = a.query_ref;

COMMENT ON VIEW monitoring.experimento_resultado IS
    'Ganho por query. Mediana das repeticoes, descartada a primeira (cache frio).';

-- A resposta à pergunta do experimento: o índice tirado de UMA query serve ao
-- resto do cluster? Uma linha por experimento.
--
-- Duas distinções carregam esta view inteira.
--
-- **Atribuível x bruto.** Só conta como efeito do índice o que aconteceu em
-- query cujo plano de 'depois' cita o índice novo. O resto é variação de
-- cache entre as duas fases e entra em ganho_bruto_ms, não em
-- ganho_atribuivel_ms. Medido na primeira rodada: de 4 queries, 2 usaram o
-- índice e 2 mudaram de tempo sem tocá-lo — sem esta separação as duas
-- últimas virariam "ganho".
--
-- **Fora da origem.** ganho_fora_da_origem_ms é a coluna que responde à
-- pergunta. A query de onde o candidato saiu vai melhorar quase por
-- construção; o que interessa é o que sobra depois de tirá-la.
--
-- As contagens são simétricas de propósito: melhoraram e pioraram exigem as
-- duas o mesmo usou_indice, e quem não usou cai em sem_efeito. Um lado exigir
-- atribuição e o outro não daria um veredito que parece pior do que é.
CREATE VIEW monitoring.experimento_veredito AS
WITH base AS (
    SELECT *, coalesce(usou_indice, false) AS usou
      FROM monitoring.experimento_resultado
)
SELECT rodada_id,
       cluster_id,
       experimento_id,
       relacao,
       ddl,
       count(*)                                         AS queries,
       count(*) FILTER (WHERE usou)                     AS usaram_indice,
       count(*) FILTER (WHERE usou AND ganho_ms > 0)    AS melhoraram,
       count(*) FILTER (WHERE usou AND ganho_ms < 0)    AS pioraram,
       count(*) FILTER (WHERE NOT usou)                 AS sem_efeito,
       round(coalesce(sum(ganho_ms) FILTER (WHERE usou), 0), 1)
                                                        AS ganho_atribuivel_ms,
       round(coalesce(sum(ganho_ms) FILTER (WHERE usou AND NOT e_a_origem), 0), 1)
                                                        AS ganho_fora_da_origem_ms,
       round(sum(ganho_ms), 1)                          AS ganho_bruto_ms,
       round(max(ganho_ms) FILTER (WHERE usou), 1)      AS melhor_ms,
       round(min(ganho_ms) FILTER (WHERE usou), 1)      AS pior_ms
  FROM base
 GROUP BY 1, 2, 3, 4, 5;

COMMENT ON VIEW monitoring.experimento_veredito IS
    'Um indice por cluster: quantas queries ele de fato ajudou, e quanto fora da query de origem.';
