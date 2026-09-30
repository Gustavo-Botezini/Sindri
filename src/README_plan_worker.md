# `plan_worker.py` — o worker de planos

Etapa 2 do pipeline. Lê de `monitoring.pg_log_event` os registros com
`record_type = 'plan'`, parseia o JSON do `auto_explain` que está no `message` e
o transforma em três tabelas analisáveis:

| tabela | papel |
| --- | --- |
| `monitoring.query` | dimensão: uma linha por texto de query distinto |
| `monitoring.plan_execution` | fato: uma linha por registro de plano |
| `monitoring.plan_node` | uma linha por nó da árvore, com o pai |

O esquema está em `initdb-monitoring/04-worker-schema.sql`. O coletor grava o
bruto e para; este aqui roda por conta própria, quando quiser, quantas vezes
quiser.

```
monitoring.pg_log_event  ──▶  plan_worker.py  ──▶  query / plan_execution / plan_node
                                                          │
                                                          ▼
                                             monitoring.query_resumo
```

## As duas decisões que a medição impôs

**A dimensão é chaveada pelo texto, não pelo `query_id`.** O `query_id` do
registro de plano está certo em 96% dos casos, mas não nos 3,8% do regime de
portal adiado: quando o cliente mantém o portal de um `SELECT` aberto até o
commit, o `auto_explain` só dispara no `ExecutorEnd`, que acontece durante o
processamento do `COMMIT`, e o registro sai com o `query_id` do COMMIT. O
`Query Text`, esse o plano sempre carrega — daí `query_hash` = sha1 do SQL com
espaços colapsados (`hash_do_texto`). Não é a normalização do
`pg_stat_statements`: literais continuam onde estão. É só uma identidade estável
para agrupar execuções do mesmo texto.

**O que não dá para resolver fica marcado, não chutado.** Dos 7.381 planos
adiados do corpus, todos caem em `vxid` que cobre mais de uma query — nem o
`vxid` nem os `bind` daquele `vxid` dizem de qual query o plano é. Esses saem com
`query_id_origem = 'indefinido'`, e o índice que serve agregação por `query_id`
os exclui.

## Resolução do `query_id`

`SQL_LOTE` puxa o lote de planos e, por `LEFT JOIN LATERAL`, o **próximo**
registro `execute`/`statement` da mesma sessão (`session_id`, `line_num >`). O
lateral usa o índice único `(session_id, line_num, event_time)` e para no
primeiro — não varre a sessão inteira.

`resolver_query_id(qid_plano, qid_seguinte, cmd_seguinte)` decide:

| situação | `query_id_resolvido` | `query_id_origem` |
| --- | --- | --- |
| o execute seguinte é `COMMIT`/`ROLLBACK`/`BEGIN` | `NULL` | `indefinido` |
| há execute seguinte com id igual ao do plano | o do seguinte | `proprio` |
| há execute seguinte com id diferente | o do seguinte | `seguinte` |
| não há seguinte, mas o plano tem id | o do plano | `proprio` |
| nada disso | `NULL` | `indefinido` |

A ordem no log é `bind` → plano(s) → `execute`, e o execute seguinte carrega o id
certo — verificado em 53.905 pares do corpus, zero divergência. A exceção é o
`COMMIT`: aí o plano saiu durante o commit e o id que ele traz é o do commit.

## O laço de um lote

`Worker.processar_lote()`, por linha do lote:

1. `json.loads(message)` — o que não for objeto com `Plan` conta em `ilegiveis`
   e é pulado (o `auto_explain` às vezes escreve o plano dentro de uma lista de
   um elemento, tratado aqui também);
2. `achatar(topo, arvore)` percorre a árvore em pré-ordem. O `node_idx` é a
   posição nessa ordem, então `parent_idx` sempre aponta para uma linha que já
   existe — dá para reconstruir a árvore com um `WITH RECURSIVE` sem depender de
   nada externo. `texto()` normaliza os campos que o EXPLAIN escreve ora como
   lista (`Sort Key`) ora como string (`Filter`);
3. `ref_da_query()` faz o upsert na dimensão, com cache em memória
   `(banco, hash) -> id`: são 193 mil planos para poucas centenas de textos
   distintos. O `ON CONFLICT` usa
   `coalesce(query.query_id, EXCLUDED.query_id)` — nunca sobrescreve um id já
   conhecido com o nulo de um plano do regime adiado;
4. monta as tuplas de `plan_execution` (do nó de topo) e de `plan_node` (uma por
   nó);
5. `ensure_partition_de(...)` para cada dia visto no lote — as partições têm que
   existir antes do `COPY`;
6. `_copiar()` grava.

`_copiar()` faz `COPY` para uma tabela temporária `ON COMMIT DROP` e depois
`INSERT ... SELECT ... ON CONFLICT DO NOTHING`. É esse `DO NOTHING` que torna
`--reprocessar` seguro: reler o corpus inteiro não duplica nada nem exige
esvaziar as tabelas antes.

A marca d'água fica em `monitoring.worker_progress` (`last_event_id`), gravada
dentro da mesma transação do lote.

## A etapa de análise

As colunas incrementais de `monitoring.query_resumo` (contagem, durações) são
mantidas por trigger sobre `plan_execution` — é a única forma de o número não
poder divergir do fato. As colunas de análise vêm de `plan_node` (quais nós,
quais relações, quanto o Seq Scan descartou) e não são agregáveis
incrementalmente: `Worker.analisar()` chama `monitoring.analisar_resumo(refs)`
só para as queries tocadas nesta execução (`self._tocadas`), o que mantém o custo
proporcional ao que chegou e não ao tamanho do corpus.

`--sem-analise` pula essa etapa. É `query_resumo` que o `index_provider.py` lê
depois, então pular significa dossiê desatualizado.

## Relatórios

`--status` mostra planos no bruto × `plan_execution` × `plan_node` × queries
distintas × marca d'água, mais a distribuição de `query_id_origem` (é aqui que
se enxerga a fatia de `indefinido`).

`--achados` roda quatro consultas prontas sobre as tabelas derivadas:

- onde o planner mais erra a estimativa (nós com 1000+ linhas reais), com a razão
  das médias e o pior caso individual;
- `Seq Scan` que joga linha fora — candidato a índice, com exemplo de filtro;
- as queries mais caras no total, agrupadas **pela dimensão de texto** e não pelo
  `query_id`, que é o agrupamento válido para 100% dos planos;
- `Sort` que derramou para disco.

## Uso

```bash
./.venv/bin/python src/plan_worker.py                # processa o que chegou
./.venv/bin/python src/plan_worker.py --reprocessar  # volta ao começo do corpus
./.venv/bin/python src/plan_worker.py --status
./.venv/bin/python src/plan_worker.py --achados
./.venv/bin/python src/plan_worker.py --seguir
```

| flag | efeito |
| --- | --- |
| `--dsn` | conexão com o monitoramento (ou `BROKK_DSN`) |
| `--lote` | planos por transação (padrão 5000) |
| `--reprocessar` | zera a marca d'água e relê tudo |
| `--seguir` / `--intervalo` | fica processando a cada N segundos |
| `--sem-analise` | não atualiza as colunas de análise de `query_resumo` |
| `--silencioso` | só o resumo no fim |

`Ctrl-C` é tratado: a marca d'água do último lote fechado já está gravada.

## Ao editar

- `COLUNAS_EXECUCAO` e `COLUNAS_NO` são a ordem do `COPY`. `COLUNAS_NO[2:]` é
  usado para ler o dicionário montado em `achatar()` — mexer numa sem mexer na
  outra desalinha as colunas silenciosamente.
- Campo novo do EXPLAIN exige três edições: a chave em `achatar()`, o nome em
  `COLUNAS_NO` e a coluna no `04-worker-schema.sql`.
