-- ---------------------------------------------------------------------------
-- Documentação de monitoring.query_resumo no próprio catálogo.
--
-- Não é enfeite: esta é a tabela onde a IA vai trabalhar, e um COMMENT é a
-- única documentação que ela alcança sozinha, com \d+ ou lendo pg_description.
-- Cada coluna abaixo tem alguma armadilha de leitura; as óbvias ficam sem
-- comentário de propósito.
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN monitoring.query_resumo.execucoes IS
    'Execuções COM PLANO REGISTRADO. Não é o total de execuções da query: só '
    'entra aqui o que o auto_explain registrou, e ele respeita '
    'auto_explain.sample_rate e log_min_duration.';

COMMENT ON COLUMN monitoring.query_resumo.duracao_media_ms IS
    'Coluna gerada: duracao_total_ms / execucoes. Média aritmética, sensível a '
    'outlier — compare sempre com duracao_min_ms e duracao_max_ms antes de '
    'concluir alguma coisa. Não existe mediana nem percentil nesta tabela '
    'porque nenhum dos dois é agregável de forma incremental, e a tabela é '
    'mantida por trigger.';

COMMENT ON COLUMN monitoring.query_resumo.fator_estimativa IS
    'linhas_reais / linhas_estimadas NO NÓ DE TOPO, isto é, sobre o tamanho do '
    'resultado final. Para query com GROUP BY ou agregação o topo devolve '
    'poucas linhas e este número tende a zero sem que haja nada errado. '
    'NÃO use este campo para achar plano ruim: use pior_fator_no, que olha '
    'todos os nós da árvore.';

COMMENT ON COLUMN monitoring.query_resumo.pior_fator_no IS
    'O maior erro de estimativa entre TODOS os nós do plano (real/estimado, '
    'já multiplicado por loops). Acima de ~10 costuma indicar estatística '
    'desatualizada ou correlação que o planner não enxerga. É o campo certo '
    'para achar plano ruim.';

COMMENT ON COLUMN monitoring.query_resumo.seq_scan_descartadas IS
    'Soma das linhas que os Seq Scan desta query leram e jogaram fora por '
    'filtro, em todas as execuções. Número grande com relacoes pequeno é o '
    'padrão clássico de índice faltando — cruze com o campo filtro de '
    'monitoring.plan_node para saber sobre qual coluna.';

COMMENT ON COLUMN monitoring.query_resumo.sorts_em_disco IS
    'Quantos nós Sort usaram "external merge", ou seja, derramaram para disco '
    'por falta de work_mem. Ao contrário de índice faltando, isto se resolve '
    'com configuração, não com DDL.';

COMMENT ON COLUMN monitoring.query_resumo.analise_em IS
    'Quando as colunas node_types..exemplo_event_time foram calculadas. Elas '
    'vêm de plan_node e NÃO são mantidas por trigger — só por '
    'monitoring.analisar_resumo(), que o worker chama ao fim de cada execução. '
    'NULL significa que nunca foram calculadas. As colunas acima de analise_em '
    'são mantidas por trigger e estão sempre em dia.';

COMMENT ON COLUMN monitoring.query_resumo.query_id IS
    'O query_id do Postgres (compute_query_id), para juntar com '
    'pg_stat_statements do banco observado. Pode ser NULL: em 3,8% dos planos '
    'do corpus não há como determiná-lo. Ver plan_execution.query_id_origem.';

COMMENT ON COLUMN monitoring.query_resumo.exemplo_event_id IS
    'A execução mais lenta desta query. Com (exemplo_event_id, '
    'exemplo_event_time) dá para puxar a árvore inteira de monitoring.plan_node '
    'e o JSON original de monitoring.pg_log_event.';

COMMENT ON COLUMN monitoring.query_resumo.linhas_reais_total IS
    'Soma de Actual Rows do nó de topo. Fracionário porque o PG18 emite '
    'contagem de linha por loop com decimais.';
