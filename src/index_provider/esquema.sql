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

    -- Estourou o statement_timeout: tempo_ms é o limite, e o tempo real é
    -- maior. Vale como piso, não como medida — ver experimento_resultado.
    censurado      boolean NOT NULL DEFAULT false,

    erro           text,

    PRIMARY KEY (experimento_id, query_ref, fase, repeticao),
    CONSTRAINT ck_medida_fase CHECK (fase IN ('antes', 'depois'))
);

ALTER TABLE monitoring.experimento_medida
    ADD COLUMN IF NOT EXISTS censurado boolean NOT NULL DEFAULT false;


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
    -- repeticao = 1 é aquecimento de cache e não entra na conta. A exceção é
    -- a medida censurada: a bancada para de repetir no primeiro timeout, então
    -- ela pode ser a única amostra da fase, e vale como piso.
    SELECT experimento_id, query_ref, fase, tempo_ms, usou_indice, censurado
      FROM monitoring.experimento_medida
     WHERE tempo_ms IS NOT NULL
       AND (censurado OR (repeticao > 1 AND erro IS NULL))
),
por_fase AS (
    SELECT experimento_id, query_ref, fase,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY tempo_ms) AS mediana_ms,
           min(tempo_ms)        AS min_ms,
           max(tempo_ms)        AS max_ms,
           count(*)             AS amostras,
           bool_or(usou_indice) AS usou_indice,
           bool_or(censurado)   AS censurado
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
       -- 'antes' censurado: o ganho é um piso, o real é maior. 'depois'
       -- censurado: não se sabe quanto sobrou, e o ganho fica nulo em vez de
       -- virar um número inventado.
       CASE WHEN NOT d.censurado
            THEN round((a.mediana_ms - d.mediana_ms)::numeric, 1)
       END                                              AS ganho_ms,
       CASE WHEN NOT d.censurado
            THEN round((100.0 * (a.mediana_ms - d.mediana_ms)
                        / nullif(a.mediana_ms, 0))::numeric, 1)
       END                                              AS ganho_pct,
       d.usou_indice,
       a.amostras AS amostras_antes,
       d.amostras AS amostras_depois,
       a.censurado AS antes_censurado,
       d.censurado AS depois_censurado
  FROM monitoring.experimento e
  JOIN por_fase a ON a.experimento_id = e.id AND a.fase = 'antes'
  JOIN por_fase d ON d.experimento_id = e.id AND d.fase = 'depois'
                 AND d.query_ref = a.query_ref;

COMMENT ON VIEW monitoring.experimento_resultado IS
    'Ganho por query. Mediana das repeticoes, descartada a primeira (cache frio). Com antes_censurado, o ganho e um piso.';

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
       round(min(ganho_ms) FILTER (WHERE usou), 1)      AS pior_ms,
       -- Quantas queries entram nas somas acima com um piso em vez de uma
       -- medida. Diferente de zero, os ganhos do experimento são "pelo menos".
       count(*) FILTER (WHERE antes_censurado)          AS antes_censuradas,
       count(*) FILTER (WHERE depois_censurado)         AS depois_censuradas
  FROM base
 GROUP BY 1, 2, 3, 4, 5;

COMMENT ON VIEW monitoring.experimento_veredito IS
    'Um indice por cluster: quantas queries ele de fato ajudou, e quanto fora da query de origem.';


-- ---------------------------------------------------------------------------
-- 7. A matriz índice × query
-- ---------------------------------------------------------------------------
-- `medir` testa cada índice só dentro do cluster de onde ele saiu, e por isso
-- não vê quem ele ajudaria fora dele — foi assim que lineitem(l_partkey)
-- ajudou queries de três clusters diferentes sem que nenhum veredito
-- mostrasse. A matriz mede cada índice distinto contra o corpus inteiro, numa
-- sessão, contra uma linha de base única. Qualquer agrupamento é avaliado
-- depois sobre a mesma medição: a diferença entre dois métodos deixa de poder
-- ser ruído entre duas corridas de bancada.
CREATE TABLE IF NOT EXISTS monitoring.matriz_sessao (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rodadas      integer[] NOT NULL,     -- de onde vieram os índices
    repeticoes   integer NOT NULL,
    timeout_ms   integer NOT NULL,
    criado_em    timestamptz NOT NULL DEFAULT now(),
    concluida_em timestamptz
);

CREATE TABLE IF NOT EXISTS monitoring.matriz_indice (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sessao_id     bigint NOT NULL
                      REFERENCES monitoring.matriz_sessao(id) ON DELETE CASCADE,
    -- O DDL sem o nome do índice: o mesmo índice proposto por dois métodos,
    -- com nomes diferentes, é medido uma vez.
    chave         text   NOT NULL,
    ddl           text   NOT NULL,
    relacao       text,
    build_ms      double precision,
    tamanho_bytes bigint,
    erro          text,
    UNIQUE (sessao_id, chave)
);

CREATE TABLE IF NOT EXISTS monitoring.matriz_medida (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sessao_id   bigint  NOT NULL
                    REFERENCES monitoring.matriz_sessao(id) ON DELETE CASCADE,
    -- NULL é a linha de base, sem índice nenhum.
    indice_id   bigint  REFERENCES monitoring.matriz_indice(id) ON DELETE CASCADE,
    query_ref   bigint  NOT NULL
                    REFERENCES monitoring.query(id) ON DELETE CASCADE,
    -- 0 é a pergunta "o plano usa o índice?", feita com EXPLAIN sem ANALYZE;
    -- de 1 em diante, execuções medidas.
    repeticao   integer NOT NULL,
    tempo_ms    double precision,
    usou_indice boolean,
    censurado   boolean NOT NULL DEFAULT false,
    erro        text
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_matriz_medida
    ON monitoring.matriz_medida (sessao_id, coalesce(indice_id, 0), query_ref, repeticao);

-- `matriz --globais` mede também o que a heurística propõe para o corpus
-- inteiro, sem rodada de clusterização por trás. A sessão guarda quantos
-- candidatos foram pedidos, e cada índice, de onde veio: 'rodada',
-- 'heuristica' ou 'rodada+heuristica' quando as duas fontes o propuseram.
ALTER TABLE monitoring.matriz_sessao
    ADD COLUMN IF NOT EXISTS globais integer;
-- Se a heurística ofereceu GIN com gin_trgm_ops (`matriz --trigramas`).
ALTER TABLE monitoring.matriz_sessao
    ADD COLUMN IF NOT EXISTS trigramas boolean NOT NULL DEFAULT false;
ALTER TABLE monitoring.matriz_indice
    ADD COLUMN IF NOT EXISTS origem text;

DROP VIEW IF EXISTS monitoring.matriz_resultado;

-- Uma linha por (índice, query). Quem não usa o índice tem ganho 0, e não a
-- diferença entre duas medidas: o índice não pode ter ajudado uma query cujo
-- plano não o cita, e o que a rodada 2 mostrou nessas queries (de -58% a +20%)
-- era cache, não efeito.
CREATE VIEW monitoring.matriz_resultado AS
WITH util AS (
    SELECT sessao_id, indice_id, query_ref, tempo_ms, censurado
      FROM monitoring.matriz_medida
     WHERE tempo_ms IS NOT NULL
       AND (censurado OR (repeticao > 1 AND erro IS NULL))
),
fase AS (
    SELECT sessao_id, indice_id, query_ref,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY tempo_ms) AS mediana_ms,
           bool_or(censurado) AS censurado,
           count(*)           AS amostras
      FROM util
     GROUP BY 1, 2, 3
),
uso AS (
    SELECT sessao_id, indice_id, query_ref, bool_or(usou_indice) AS usou_indice
      FROM monitoring.matriz_medida
     WHERE indice_id IS NOT NULL AND repeticao = 0
     GROUP BY 1, 2, 3
)
SELECT i.sessao_id,
       i.id                                   AS indice_id,
       i.chave,
       i.relacao,
       u.query_ref,
       coalesce(u.usou_indice, false)         AS usou_indice,
       round(b.mediana_ms::numeric, 1)        AS antes_ms,
       round(d.mediana_ms::numeric, 1)        AS depois_ms,
       coalesce(b.censurado, false)           AS antes_censurado,
       coalesce(d.censurado, false)           AS depois_censurado,
       CASE WHEN NOT coalesce(u.usou_indice, false) THEN 0
            WHEN d.censurado OR d.mediana_ms IS NULL OR b.mediana_ms IS NULL
                 THEN NULL
            ELSE round((100.0 * (b.mediana_ms - d.mediana_ms)
                        / nullif(b.mediana_ms, 0))::numeric, 2)
       END                                    AS ganho_pct
  FROM monitoring.matriz_indice i
  JOIN uso u   ON u.sessao_id = i.sessao_id AND u.indice_id = i.id
  LEFT JOIN fase b ON b.sessao_id = i.sessao_id AND b.indice_id IS NULL
                  AND b.query_ref = u.query_ref
  LEFT JOIN fase d ON d.sessao_id = i.sessao_id AND d.indice_id = i.id
                  AND d.query_ref = u.query_ref;

COMMENT ON VIEW monitoring.matriz_resultado IS
    'Cada indice contra cada query, com linha de base unica. Sem usar o indice, ganho 0.';


-- ---------------------------------------------------------------------------
-- 8. A seleção como problema de otimização
-- ---------------------------------------------------------------------------
-- Sem agrupamento: o conjunto inteiro de índices escolhido de uma vez, para a
-- carga inteira, por um programa inteiro resolvido sobre uma sessão da
-- matriz. Uma linha por problema resolvido (um orçamento, um λ, um modelo de
-- interação). `validar` cria o conjunto escolhido inteiro e grava em
-- otimizacao_medida o que de fato aconteceu, para pôr ao lado do previsto.
CREATE TABLE IF NOT EXISTS monitoring.otimizacao (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sessao_id             bigint NOT NULL
                              REFERENCES monitoring.matriz_sessao(id) ON DELETE CASCADE,
    -- orçamento, λ, custo de escrita, peso, limiar e modelo de interação.
    parametros            jsonb  NOT NULL,
    -- A mensagem do solver. Ótimo aqui é o ótimo do MODELO, não da carga:
    -- quem diz o quanto o modelo acertou é a validação.
    status                text   NOT NULL,
    -- matriz_indice.id dos escolhidos. Vazio é resposta possível: nenhum
    -- índice paga o que custa.
    indices               bigint[] NOT NULL,
    armazenamento_bytes   bigint,
    beneficio_previsto_ms double precision,
    objetivo              double precision,
    -- Por query: peso, Δ previsto por execução, o índice que a atende e os
    -- que a pioram.
    previsao              jsonb,
    criado_em             timestamptz NOT NULL DEFAULT now(),
    validado_em           timestamptz
);

CREATE TABLE IF NOT EXISTS monitoring.otimizacao_medida (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    otimizacao_id  bigint  NOT NULL
                       REFERENCES monitoring.otimizacao(id) ON DELETE CASCADE,
    -- Qual validação do conjunto: 1, 2, ... Ver otimizacao_validacao.
    validacao      integer NOT NULL DEFAULT 1,
    query_ref      bigint  NOT NULL
                       REFERENCES monitoring.query(id) ON DELETE CASCADE,
    fase           text    NOT NULL,
    -- 0 é a pergunta "quais índices do conjunto o plano usa?", feita com
    -- EXPLAIN sem ANALYZE e o conjunto inteiro criado; de 1 em diante,
    -- execuções medidas.
    repeticao      integer NOT NULL,
    tempo_ms       double precision,
    -- matriz_indice.id dos índices do conjunto que o plano cita. Com mais de
    -- um índice criado, "usou o índice" deixa de ser sim ou não.
    indices_usados bigint[],
    censurado      boolean NOT NULL DEFAULT false,
    erro           text,
    CONSTRAINT ck_otimizacao_medida_fase CHECK (fase IN ('antes', 'depois'))
);

-- Uma otimização pode ser validada mais de uma vez (a seleção adaptativa
-- remede quando a evidência não decide). Até aqui a chave não tinha a
-- validação, e `validar --refazer` apagava a medição anterior: foi por isso
-- que o mesmo conjunto de 44 MB do TPC-H com PGTune virou duas otimizações
-- (4 e 8) para poder ser medido duas vezes. Em banco que já existe, a coluna
-- entra com 1 e as medições antigas viram a validação 1.
ALTER TABLE monitoring.otimizacao_medida
    ADD COLUMN IF NOT EXISTS validacao integer NOT NULL DEFAULT 1;

DROP INDEX IF EXISTS monitoring.ux_otimizacao_medida;
CREATE UNIQUE INDEX IF NOT EXISTS ux_otimizacao_medida_validacao
    ON monitoring.otimizacao_medida
       (otimizacao_id, validacao, query_ref, fase, repeticao);

DROP VIEW IF EXISTS monitoring.otimizacao_resultado;

-- Uma linha por (otimização, validação, query), com a regra de
-- matriz_resultado: se o plano não cita nenhum índice do conjunto, o ganho é
-- 0, e o que as medianas mostrarem é cache. Validações diferentes nunca se
-- misturam aqui: cada uma tem a sua linha de base.
CREATE VIEW monitoring.otimizacao_resultado AS
WITH util AS (
    SELECT otimizacao_id, validacao, query_ref, fase, tempo_ms, censurado
      FROM monitoring.otimizacao_medida
     WHERE tempo_ms IS NOT NULL
       AND (censurado OR (repeticao > 1 AND erro IS NULL))
),
fase AS (
    SELECT otimizacao_id, validacao, query_ref, fase,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY tempo_ms) AS mediana_ms,
           bool_or(censurado) AS censurado
      FROM util
     GROUP BY 1, 2, 3, 4
),
uso AS (
    SELECT otimizacao_id, validacao, query_ref, indices_usados, erro
      FROM monitoring.otimizacao_medida
     WHERE fase = 'depois' AND repeticao = 0
)
SELECT u.otimizacao_id,
       u.validacao,
       u.query_ref,
       coalesce(cardinality(u.indices_usados), 0) > 0 AS usou_indice,
       u.indices_usados,
       u.erro,
       round(b.mediana_ms::numeric, 1)        AS antes_ms,
       round(d.mediana_ms::numeric, 1)        AS depois_ms,
       coalesce(b.censurado, false)           AS antes_censurado,
       coalesce(d.censurado, false)           AS depois_censurado,
       CASE WHEN coalesce(cardinality(u.indices_usados), 0) = 0 THEN 0
            WHEN d.censurado OR d.mediana_ms IS NULL OR b.mediana_ms IS NULL
                 THEN NULL
            ELSE round((b.mediana_ms - d.mediana_ms)::numeric, 1)
       END                                    AS ganho_ms,
       CASE WHEN coalesce(cardinality(u.indices_usados), 0) = 0 THEN 0
            WHEN d.censurado OR d.mediana_ms IS NULL OR b.mediana_ms IS NULL
                 THEN NULL
            ELSE round((100.0 * (b.mediana_ms - d.mediana_ms)
                        / nullif(b.mediana_ms, 0))::numeric, 2)
       END                                    AS ganho_pct
  FROM uso u
  LEFT JOIN fase b ON b.otimizacao_id = u.otimizacao_id
                  AND b.validacao = u.validacao
                  AND b.query_ref = u.query_ref AND b.fase = 'antes'
  LEFT JOIN fase d ON d.otimizacao_id = u.otimizacao_id
                  AND d.validacao = u.validacao
                  AND d.query_ref = u.query_ref AND d.fase = 'depois';

COMMENT ON VIEW monitoring.otimizacao_resultado IS
    'O conjunto escolhido pela otimizacao, criado inteiro, contra cada query, uma linha por validacao. Sem usar indice do conjunto, ganho 0.';


-- ---------------------------------------------------------------------------
-- 9. A seleção adaptativa
-- ---------------------------------------------------------------------------
-- `otimizar` escolhe sob um orçamento dado e prevê o conjunto a partir de
-- medidas de um índice por vez. `adaptar` decide com o conjunto inteiro
-- MEDIDO, quantas vezes for preciso para separar ganho de ruído, e para de
-- explorar quando o benefício adicional não paga mais. Ver adaptativa.py.
--
-- O contrato dos dados experimentais, que estatistica.py e evidencia.py
-- assumem e que o esquema abaixo garante:
--
--   sessão de medida  uma corrida da bancada sobre um estado do banco: a
--                     linha de base de uma matriz_sessao, ou uma validação
--                     (otimizacao_validacao). Cada uma tem o seu ambiente.
--   configuração      o conjunto de índices criados além dos existentes,
--                     pelos matriz_indice.id da sessão da matriz; vazio = só
--                     os existentes. Duas otimizações com o mesmo conjunto
--                     são a mesma configuração, e a evidência delas soma.
--   bloco             uma fase de uma sessão: (sessão, configuração, fase).
--                     Numa validação, 'antes' é a configuração vazia e
--                     'depois' a da otimização. Um bloco nunca é medida de
--                     outra configuração.
--   repetição         uma execução dentro do bloco; a 1 é aquecimento. As
--                     repetições de um bloco NÃO são sessões independentes:
--                     a inferência usa o bloco como unidade, e as
--                     repetições só para a variância dentro da sessão.
--   censura           censurado = true e tempo_ms = timeout: o tempo real é
--                     maior ou igual a tempo_ms, nunca igual.
--   finalidade        'selecao' é evidência para decidir; 'confirmacao' é a
--                     medida independente do que foi decidido, que a decisão
--                     não lê; 'legado' é validação anterior a este registro,
--                     cuja finalidade nunca foi declarada.

-- O ambiente de cada sessão: valores brutos (para auditar) e o hash do
-- subconjunto que precisa ser igual para duas sessões serem comparáveis
-- (para conferir rápido). Ver ambiente.py. NULL nas sessões anteriores a esta
-- coluna: o ambiente não foi registrado, e o relatório diz isso.
ALTER TABLE monitoring.matriz_sessao
    ADD COLUMN IF NOT EXISTS ambiente jsonb;
ALTER TABLE monitoring.matriz_sessao
    ADD COLUMN IF NOT EXISTS ambiente_hash text;

-- Uma execução de `adaptar`: os parâmetros, quantas validações ela rodou e
-- leu, por que parou e qual configuração recomendou. As decisões são função
-- da evidência: rodar de novo sobre a mesma evidência dá as mesmas decisões.
CREATE TABLE IF NOT EXISTS monitoring.adaptacao (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sessao_id         bigint NOT NULL
                          REFERENCES monitoring.matriz_sessao(id) ON DELETE CASCADE,
    parametros        jsonb  NOT NULL,
    -- 'analise' (só lê a evidência), 'execucao' (roda validações de
    -- seleção), 'confirmacao' (mede à parte o que foi recomendado)
    modo              text   NOT NULL,
    estado            text   NOT NULL DEFAULT 'em_andamento',
    -- Validações que esta execução rodou, e as que a decisão final leu.
    medicoes          integer NOT NULL DEFAULT 0,
    validacoes_lidas  integer,
    motivo            text,
    recomendada       bigint REFERENCES monitoring.otimizacao(id) ON DELETE SET NULL,
    resultado         jsonb,
    -- Preenchidos pela confirmação: o veredito da medida independente.
    -- false reabre a exploração; as validações de confirmação que falharam
    -- passam a ser evidência de seleção, e a nova recomendação pede outra
    -- confirmação.
    confirmada        boolean,
    confirmacao       jsonb,
    iniciada_em       timestamptz NOT NULL DEFAULT now(),
    concluida_em      timestamptz,
    CONSTRAINT ck_adaptacao_modo CHECK (modo IN ('analise', 'execucao', 'confirmacao')),
    CONSTRAINT ck_adaptacao_estado CHECK (estado IN (
        'em_andamento',
        'concluida',            -- o critério de parada foi atingido
        'aguardando_medicao',   -- a decisão pede uma validação que não foi feita
        'interrompida'))
);

-- Cada passo da exploração, na ordem: o que foi comparado com o quê, o
-- benefício marginal com o intervalo, a margem e a decisão. É a resposta a
-- "por que continuou" e "por que parou".
CREATE TABLE IF NOT EXISTS monitoring.adaptacao_passo (
    adaptacao_id     bigint  NOT NULL
                         REFERENCES monitoring.adaptacao(id) ON DELETE CASCADE,
    passo            integer NOT NULL,
    -- 'comparacao' | 'salto' | 'medicao' | 'parada'
    tipo             text    NOT NULL,
    -- 'adicao' | 'remocao' | 'substituicao' | 'combinacao' | 'fronteira'
    -- | 'medida' | 'resolucao'
    movimento        text,
    de_otimizacao    bigint,
    para_otimizacao  bigint,
    -- o teste usado: 'superioridade' | 'nao_inferioridade'
    teste            text,
    -- 'aceita' | 'rejeitada' | 'inconclusiva' | 'nao_ordenavel'
    decisao          text,
    delta_b_ms       double precision,
    li_ms            double precision,
    ls_ms            double precision,
    margem_ms        double precision,
    delta_mb         double precision,
    detalhe          jsonb,
    criado_em        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (adaptacao_id, passo)
);

-- Cada validação de uma otimização. concluida_em NULL é validação
-- interrompida, e não entra em conta nenhuma.
CREATE TABLE IF NOT EXISTS monitoring.otimizacao_validacao (
    otimizacao_id      bigint  NOT NULL
                           REFERENCES monitoring.otimizacao(id) ON DELETE CASCADE,
    validacao          integer NOT NULL,
    finalidade         text    NOT NULL,
    -- quem a criou: 'validar' (à mão), 'adaptar', 'migracao' (anterior)
    origem             text    NOT NULL,
    -- 'base_primeiro': linha de base, CREATE INDEX, conjunto.
    -- 'conjunto_primeiro': CREATE INDEX, conjunto, DROP, linha de base.
    -- A seleção adaptativa alterna as duas entre validações do mesmo
    -- conjunto, para a deriva temporal da sessão não cair sempre do mesmo
    -- lado da comparação.
    ordem              text    NOT NULL DEFAULT 'base_primeiro',
    ambiente           jsonb,
    ambiente_hash      text,
    -- A execução de `adaptar` que pediu esta validação.
    adaptacao_id       bigint REFERENCES monitoring.adaptacao(id) ON DELETE SET NULL,
    -- Só em 'confirmacao': a execução cuja recomendação está sendo
    -- confirmada.
    confirma_adaptacao bigint REFERENCES monitoring.adaptacao(id) ON DELETE SET NULL,
    -- O que se sabe que falta nesta validação (texto livre, para o relatório).
    limitacoes         text,
    iniciada_em        timestamptz NOT NULL DEFAULT now(),
    concluida_em       timestamptz,
    PRIMARY KEY (otimizacao_id, validacao),
    CONSTRAINT ck_otimizacao_validacao_finalidade
        CHECK (finalidade IN ('selecao', 'confirmacao', 'legado')),
    CONSTRAINT ck_otimizacao_validacao_origem
        CHECK (origem IN ('validar', 'adaptar', 'migracao')),
    CONSTRAINT ck_otimizacao_validacao_ordem
        CHECK (ordem IN ('base_primeiro', 'conjunto_primeiro'))
);

-- O movimento da caminhada que pediu a validação, quando foi `adaptar`:
-- 'fronteira', 'adicao', 'remocao', 'substituicao', 'combinacao',
-- 'medida' ou 'resolucao' (a remedição de uma alternativa já medida que
-- ficou inconclusiva perto da margem). NULL nas validações à mão e nas
-- anteriores a esta coluna. É o que conta o orçamento de resolução, e o que
-- documenta quantas medições cada parte do procedimento gastou.
ALTER TABLE monitoring.otimizacao_validacao
    ADD COLUMN IF NOT EXISTS movimento text;

-- As validações feitas antes desta tabela: uma por otimização validada. O
-- que se sabe delas pelo código da época vai registrado (a ordem era sempre
-- linha de base primeiro); o que não se sabe fica dito em limitacoes, e a
-- finalidade é 'legado', não 'selecao': ninguém a declarou. Idempotente.
INSERT INTO monitoring.otimizacao_validacao
       (otimizacao_id, validacao, finalidade, origem, ordem, limitacoes,
        iniciada_em, concluida_em)
SELECT o.id, 1, 'legado', 'migracao', 'base_primeiro',
       'validação anterior ao registro de validações: finalidade e ambiente '
       'não registrados; início desconhecido (iniciada_em = validado_em)',
       o.validado_em, o.validado_em
  FROM monitoring.otimizacao o
 WHERE o.validado_em IS NOT NULL
   AND EXISTS (SELECT 1 FROM monitoring.otimizacao_medida m
                WHERE m.otimizacao_id = o.id AND m.validacao = 1)
ON CONFLICT (otimizacao_id, validacao) DO NOTHING;

-- Custo de manutenção MEDIDO de um índice, quando houver. A bancada só
-- reexecuta leitura e não mede isto; a tabela existe para receber o
-- resultado de um experimento A/B de escrita (a mesma carga de escrita com e
-- sem o índice), e `adaptar` a lê quando há linha. Sem linha, nada é
-- estimado: o relatório mostra o ponto de equilíbrio (quanto a manutenção
-- pode custar antes de o índice deixar de se pagar) e diz que só a leitura
-- foi avaliada. A contagem estática de manutencao.py ("escritas que PODEM
-- manter o índice") nunca é gravada aqui: ela não é medida.
CREATE TABLE IF NOT EXISTS monitoring.custo_manutencao (
    id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sessao_id              bigint NOT NULL
                               REFERENCES monitoring.matriz_sessao(id) ON DELETE CASCADE,
    -- matriz_indice.chave: o DDL sem o nome.
    chave                  text   NOT NULL,
    -- ms a mais por escrita da carga, com o índice contra sem ele.
    ms_por_escrita         double precision,
    wal_bytes_por_escrita  double precision,
    -- throughput de escrita com o índice / sem o índice.
    tps_relativo           double precision,
    -- n_tup_hot_upd / n_tup_upd observados com o índice.
    fracao_hot             double precision,
    escritas_medidas       bigint,
    metodo                 text   NOT NULL,
    ambiente               jsonb,
    observacao             text,
    medido_em              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_custo_manutencao_sessao
    ON monitoring.custo_manutencao (sessao_id, chave);

