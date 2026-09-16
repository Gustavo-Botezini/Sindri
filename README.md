# Sindri

Bancada de trabalho do BrokkPG: um Postgres com TPC-H carregado e
instrumentação de planos ligada, mais um gerador de carga para alimentar o
corpus de logs.

## Subir o banco

```bash
cd /home/colono/Sindri
docker compose up -d
```

A primeira subida executa `initdb/` (schema, `\copy` dos `.tbl` de
`tpch-data/`, índices baseline) e leva alguns minutos. As subidas seguintes
reaproveitam o volume `pgdata` e são imediatas.

Rode de dentro da WSL, não pelo caminho UNC do Windows: os bind mounts
relativos (`./logs`, `./tpch-data`) precisam resolver como caminho Linux.

## Instrumentação

O servidor sobe com `pg_stat_statements` e `auto_explain` pré-carregados. Com
`auto_explain.log_min_duration=0`, **toda** query executada gera um registro de
plano em `logs/postgresql-<timestamp>.json` — um JSON por linha
(`log_destination=jsonlog`).

O registro de plano é o que tem `message` começando com `duration:` e contendo
`plan:`; o corpo depois da primeira quebra de linha é o JSON do
`EXPLAIN (ANALYZE, BUFFERS, WAL, SETTINGS, FORMAT JSON)`, com os campos:

| campo | vem de |
| --- | --- |
| `Query Text` | SQL, com `$1`, `$2`... quando o cliente usa prepared statement |
| `Query Parameters` | valores realmente vinculados (`auto_explain.log_parameter_max_length=-1`) |
| `Plan` | árvore com `Actual Rows` x `Plan Rows`, buffers, tempo de I/O e WAL por nó |
| `Settings` | GUCs de planejamento fora do default (`{}` quando nenhum foi mexido) |

Cada linha do jsonlog carrega ainda `application_name`, que separa a carga do
BenchBase de qualquer outra coisa conectada no banco, e `query_id`
(`compute_query_id=on`) — este último com uma armadilha, descrita a seguir.

### O `query_id` do registro de plano não é confiável dentro de transação

Quando o cliente roda a query dentro de um bloco de transação explícito — que
é o caso do BenchBase, e de qualquer driver com autocommit desligado — o campo
`query_id` **do registro de plano** vem com o id do `COMMIT`, não o da query.

A causa é a ordem de execução: o `auto_explain` engancha no `ExecutorEnd`, e
dentro de uma transação explícita o portal só é destruído no commit. O plano
acaba sendo emitido durante o processamento do `COMMIT`, quando o `query_id`
reportado pelo backend já foi trocado.

O efeito medido num power run: 21 queries TPC-H distintas saíram todas com o
mesmo `query_id`, o do `COMMIT`. Em autocommit (por exemplo `psql` no protocolo
simples) o campo vem correto.

Para recuperar o id verdadeiro, use o **`vxid`**: o registro de plano tem o
mesmo `vxid` dos registros `parse`/`bind`/`execute` da própria query, e esses
carregam o `query_id` certo. O registro do `COMMIT` fica em outro `vxid`:

```
vxid    query_id               registro
10/3    835705010550979738     parse / bind / execute  <- id correto
10/3    6097083398544187049    plano                   <- id do COMMIT
10/0    6097083398544187049    COMMIT
```

Consequência prática para o coletor: **não desligue
`log_min_duration_statement`**. Ele é o que produz os registros `execute`, e
sem eles não há de onde tirar o `query_id` correto para juntar com
`pg_stat_statements`. O preço é volume: são 4 registros por query (`parse`,
`bind`, `execute` e o plano) quando o cliente usa prepared statement.

Vale notar também que o `Query Text` do plano e o `query` do
`pg_stat_statements` não batem como texto: o `pg_stat_statements` normaliza as
constantes literais para `$n` por conta própria, então a mesma query aparece
como `SUM(l_extendedprice) / 7.0` no plano e `SUM(l_extendedprice) / $3` no
agregado. Junção por texto não funciona; é pelo `query_id` recuperado via
`vxid`.

Sobre custo: `auto_explain.log_analyze` + `log_timing` instrumentam cada nó do
plano e inflam os tempos medidos. Para medir latência absoluta, desligue
`auto_explain.log_timing` — o plano continua saindo, sem os tempos por nó.

## Gerar carga com o BenchBase

[BenchBase](https://github.com/cmu-db/benchbase) (CMU-DB) roda o TPC-H por
JDBC. Não há integração a fazer com o `auto_explain`: qualquer query que chegue
ao banco já é registrada. Basta apontá-lo para cá.

A imagem é construída a partir do fonte dentro do Docker (não precisa de JDK
nem Maven na WSL); o clone do BenchBase acontece durante o build e não entra
no git deste projeto.

```bash
docker compose --profile bench build benchbase
```

**Power run** — 1 terminal, uma passada em ordem por Q1..Q22:

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) \
  docker compose --profile bench run --rm benchbase
```

**Throughput run** — 4 terminais escolhendo query por peso durante 180s
(ajuste `<time>` e `<terminals>` em `benchbase/config/tpch_throughput.xml`):

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) \
  docker compose --profile bench run --rm benchbase \
  -b tpch -c sindri-config/tpch_throughput.xml \
  --create=false --load=false --execute=true
```

Os sumários de cada execução (latência por query, throughput, CSV de samples)
caem em `benchbase/results/`. Os planos caem em `logs/`.

### Carga de escrita (INSERT / UPDATE / DELETE)

As 22 queries do TPC-H são só leitura, e o BenchBase não implementa as refresh
functions (RF1/RF2) da especificação — o diretório de procedures do TPC-H dele
tem só Q1..Q22. Para escrita, o caminho é o benchmark `templated`, que executa
SQL arbitrário com `?` e distribuições de valor, sem schema próprio e sem fase
de load, direto nas tabelas que já estão aqui.

```bash
docker compose --profile bench run --rm benchbase   -b templated -c sindri-config/tpch_write.xml   --create=false --load=false --execute=true
```

As instruções estão em `benchbase/config/templates_tpch_write.xml`. O desenho
segue duas regras, para respeitar as FKs e não estragar a base do dbgen:

- `INSERT`, `DELETE` e o `UPDATE` de coluna indexada agem só na **cauda
  sintética** de `orders`, acima de `o_orderkey` 6000000, faixa que o dbgen
  nunca ocupa. O filtro `AND o_orderkey > 6000000` está no SQL de propósito:
  mesmo mexendo nos ranges das distribuições, os dados originais ficam fora de
  alcance. Pedido sintético não tem `lineitem`, então o `DELETE` também nunca
  esbarra na FK.
- Os `UPDATE` que tocam a base original mexem só em `o_comment` e `l_comment`,
  colunas que nenhuma das 22 queries filtra ou agrega. O custo de escrita é
  real; o conjunto-resposta do TPC-H não muda.

Medido num teste de 30s a 200 req/s com 4 terminais:

| | resultado |
| --- | --- |
| requests | 6001, zero aborted, zero unexpected SQL errors |
| planos gerados em `logs/` | 10160 |
| pedidos originais | 1500000, todos intactos |
| cauda sintética | 1409 linhas (2306 inseridas, 897 removidas) |
| `lineitem` | contagem inalterada, 195 tuplas atualizadas |
| autovacuum | já disparou em `orders` e em `lineitem` |

Os planos vêm com nó de topo `ModifyTable` (`Operation` `Insert`/`Update`/
`Delete`) e contabilidade de WAL por nó. Aparecem também muitos `LockRows`:
são as checagens de FK disparadas por trigger, capturadas porque
`auto_explain.log_nested_statements` está ligado.

O `rate` está limitado a 200 de propósito. Sem limite, 4 terminais de `UPDATE`
produzem bloat mais rápido do que o autovacuum consegue recolher, e o corpus
vira retrato de banco em colapso em vez de banco sob carga — aumente só se for
justamente isso que você quer estudar.

Para **carga mista**, rode este config num terminal e `tpch_throughput.xml` em
outro ao mesmo tempo: leitura analítica concorrente com escrita, cada uma com
seu `application_name` no log.

Um `[ERROR] Procedure Invalid does not have the correct class type` na saída é
ruído interno do BenchBase (o placeholder `INVALID` da lista de transações), não
um problema do seu config — os workers são criados na linha seguinte.

### Consultas complementares (CTE, window, view, procedure)

As 22 queries do TPC-H cobrem 18 tipos de nó, incluindo bitmap scan,
index-only scan, os três tipos de join, paralelismo, `Memoize` e
`Incremental Sort`. Mas quatro nós comuns nunca apareciam: `WindowAgg`,
`Unique`, `SetOp` e `CTE Scan`.

`benchbase/config/templates_tpch_extra.xml` tem 12 consultas que fecham essa
lacuna, e `initdb/04-analytics-objects.sql` cria os objetos que elas usam:

| objeto | o que é |
| --- | --- |
| `v_order_revenue` | view: receita por pedido |
| `mv_nation_month_revenue` | view materializada: receita por nação e mês, 2000 linhas |
| `sp_snapshot_nation` | procedure: agregado pesado + INSERT + DELETE |
| `fn_customer_revenue` | função plpgsql chamada por linha |
| `bench_report` | tabela destino da procedure, mantida em 500 linhas |

```bash
docker compose --profile bench run --rm benchbase   -b templated -c sindri-config/tpch_extra.xml   --create=false --load=false --execute=true
```

Medido num teste de 60s: 128 requests, 444 planos, zero erro. Os nós que
passaram a existir no corpus:

| nó | ocorrências |
| --- | --- |
| `WindowAgg` | 82 |
| `Result` | 65 |
| `Function Scan` | 56 |
| `Subquery Scan` | 49 |
| `Unique` | 40 |
| `SetOp` | 22 |
| `Append` | 21 |
| `ModifyTable` | 20 |
| `ProjectSet` | 18 |
| `CTE Scan` | 18 |

O par mais útil do arquivo é `ConsultaView` contra `ConsultaMatview`: a mesma
pergunta de negócio, 1685 ms pela view (que o planner expande na junção
completa de `orders`, `customer` e `lineitem`) contra 4,5 ms pela materializada.

Duas armadilhas que resolvi escrevendo isso, e que valem para qualquer
consulta nova que você adicionar:

- O `MATERIALIZED` na CTE é obrigatório. Sem ele o planner faz inline desde o
  PG12 e o nó `CTE Scan` some do plano.
- A função é plpgsql, não SQL. Função SQL simples é inlined pelo planner e
  desaparece dentro do plano da query chamadora; em plpgsql ela vira statement
  aninhado próprio, visível no log graças ao `log_nested_statements`.

Um nó continua faltando: `Group`, o `GROUP BY` sobre entrada já ordenada. O
planner só o escolhe em situação bem específica e não consegui provocá-lo de
forma natural sobre este schema.

### Corrida longa nos 4 painéis

`run-bench.sh` abre o Windows Terminal dividido em 4 e dispara tudo de uma vez:
duas cargas de leitura analítica, uma de escrita e um monitor ao vivo.

```bash
./run-bench.sh          # 30 minutos (padrão)
./run-bench.sh 300      # 5 minutos
WRITE_RATE=50 ./run-bench.sh
DRY_RUN=1 ./run-bench.sh    # gera as configs e os scripts de painel sem abrir nada
```

```
┌─────────────┬─────────────┐
│ leitura1    │ leitura2    │   tpch_throughput, 4 sessões cada
├─────────────┼─────────────┤
│ monitor     │ escrita     │   templated, rate reduzido
└─────────────┴─────────────┘
```

O script sobe o Postgres se ele não estiver no ar, constrói a imagem do
BenchBase se faltar, e checa `wt.exe` antes de tentar abrir qualquer coisa.

O `<time>` e o `<rate>` moram dentro do XML — não existe flag de linha de
comando para eles. Por isso o script **copia** as configs versionadas para
`benchbase/config/generated/` com os valores trocados, em vez de editar os
originais. Cada painel escreve em `benchbase/results/{l1,l2,w}`, senão duas
execuções iniciadas no mesmo segundo colidiriam no nome do arquivo.

O `WRITE_RATE` cai para 20 por padrão. A 200 req/s a escrita sozinha geraria
cerca de 760 mil planos em 30 minutos, uns 3 GB de log, afogando os planos
analíticos — que são poucos porque cada query TPC-H leva segundos. A 20 req/s a
corrida de 30 minutos deve render algo em torno de 76 mil planos de escrita
para 8 mil de leitura, na casa de 400 a 500 MB.

O monitor mostra o tempo restante, o crescimento de `logs/`, as contadoras de
`pg_stat_user_tables` (inserções, updates, deletes, tuplas mortas, autovacuums)
e as sessões ativas por `application_name`. Ele encerra sozinho 45 segundos
depois do fim das cargas e imprime o total de planos gravados.

### Voltar a base ao estado original

A cauda sintética sai com um comando:

```sql
DELETE FROM orders WHERE o_orderkey > 6000000;
```

Os comentários sobrescritos não têm volta, porque o texto anterior era do
dbgen. Se quiser o dataset exatamente como veio, recarregue do zero — os `.tbl`
continuam em `tpch-data/`:

```bash
docker compose down -v && docker compose up -d
```

> **Nunca rode com `--create=true` ou `--load=true`.** O DDL do TPC-H do
> BenchBase começa com `DROP TABLE ... CASCADE` nas 8 tabelas: isso apagaria a
> base carregada pelo `initdb`. As configs em `benchbase/config/` já fixam
> `--create=false --load=false`.

O schema do BenchBase é idêntico ao de `initdb/01-tpch-schema.sql` (mesmos
nomes de tabela, coluna e tipos), por isso o `--execute` roda direto sobre os
dados do dbgen. A diferença é só de índices: o BenchBase criaria bem mais
índices que `initdb/03-baseline-indexes.sql`, e o baseline enxuto é
proposital — é o que deixa espaço para o BrokkPG ter o que recomendar.

## Reconstruir o esquema pelos logs

`src/schema_estimator.py` lê todos os arquivos de `logs/` e monta o esquema do
banco a partir do que a carga revela — sem abrir conexão com o Postgres.

```bash
python3 src/schema_estimator.py                    # relatório no stdout
python3 src/schema_estimator.py --sql esquema.sql  # + DDL estimado
python3 src/schema_estimator.py --json evid.json   # + evidência por coluna
python3 src/schema_estimator.py --ignorar-ddl      # só o que a carga revela
python3 src/schema_estimator.py --banco tpch       # ignora os outros bancos
```

Ele aceita o jsonlog cru e o já formatado (os dois convivem em `logs/`), e
percorre os 665 MB do corpus em cerca de 10 segundos.

O `--sql` sai executável: `CREATE TABLE` com tipos, `NOT NULL` e `DEFAULT`,
`ALTER TABLE ... ADD PRIMARY KEY`, `CREATE INDEX` com a contagem de uso de cada
um no corpus, e as FKs numa seção no fim (cada uma depende de duas tabelas, e
no meio do arquivo apontariam para tabela ainda não criada). Rodado num banco
vazio, o arquivo reconstrói as 8 tabelas do TPC-H com tipo, tamanho e
nulabilidade idênticos aos de `initdb/`, mais as 8 FKs e os 8 índices.

Use o `--banco` quando o corpus cobrir mais de um banco: o servidor é um só e
o log é comum a todos, então sem o filtro dois esquemas com nomes de tabela
iguais viram um só. O relatório avisa quando isso acontece.

As fontes, da mais forte para a mais fraca:

| fonte | o que dá |
| --- | --- |
| DDL no próprio log | tudo, literal — com `log_min_duration_statement=0` os comandos do `initdb/` também são registrados |
| checagem dos triggers RI | prova que a FK existe e de que lado está cada coluna (`SELECT 1 FROM ONLY "public"."x" ... FOR KEY SHARE`) |
| planos do `auto_explain` | tabelas (`Relation Name`), índices (`Index Name` + `Index Cond`), colunas e tipos (pelos casts que o planner imprime), junções e cardinalidade |
| SQL e parâmetros vinculados | ordem real das colunas (lista do `INSERT`) e valores que tipam o que o plano deixou sem cast |

Cada item do relatório vem com a evidência que o sustenta e uma confiança de 0
a 1; FK deduzida só de junção sai marcada como candidata (`fk?`), e o DDL
gerado deixa essas comentadas.

O `--ignorar-ddl` finge que o corpus não capturou o `initdb` e mostra até onde
a carga sozinha chega. O relatório normal fecha com a conferência entre os dois
caminhos: **das 66 colunas, a carga tipa 32 e acerta 30 (94%)** — as 34
restantes o corpus só projeta, nunca compara com literal nenhum, e são
invisíveis para qualquer inferência baseada em log.

## O banco de monitoramento

O destino dos logs é uma **segunda instância** de Postgres, `sindri_monitoring`,
na porta 5433. Sobe junto com o resto:

```bash
docker compose up -d
psql -h localhost -p 5433 -U brokk -d brokk     # senha brokk_dev_pwd
```

Ela é separada do banco observado por dois motivos. O primeiro é
realimentação: o `sindri_postgres` sobe com `log_min_duration_statement=0` e
`auto_explain.log_min_duration=0`, então gravar a ingestão lá dentro faria cada
`COPY` do coletor virar registro de log — com plano e tudo — que na passada
seguinte seria ingerido, gerando mais log. O corpus deixaria de ser o retrato da
carga e passaria a ser o do próprio coletor. O segundo é medição: a escrita da
ingestão disputaria buffers, WAL e autovacuum com o benchmark.

O schema está em `initdb-monitoring/`, aplicado na primeira subida:

```
monitoring
├── ingestion_file          controle por arquivo, com offset de leitura
├── pg_log_event            evento bruto, particionado por dia
├── pg_log_event_staging    UNLOGGED, destino do COPY
└── worker_progress         marca d'água do worker
```

### O contrato com o coletor

```
1. INSERT em monitoring.ingestion_file          -> devolve file_id
2. COPY monitoring.pg_log_event_staging FROM STDIN
3. SELECT * FROM monitoring.ingest_staging(file_id)
4. UPDATE do offset em monitoring.ingestion_file
```

Os passos 2 a 4 cabem numa transação. Nada é trigger: o coletor chama
explicitamente e o worker roda depois, por conta própria.

`ingest_staging` cria as partições do dia que faltarem, move o lote com
`ON CONFLICT DO NOTHING` e devolve quantas linhas entraram e quantas já
estavam lá.

### As decisões que o corpus ditou

**Offset, não status binário.** A rotação é por 50 MB, então há sempre um
arquivo sendo escrito enquanto o coletor lê. `bytes_ingested` guarda onde
terminou o último objeto JSON completo — que é justamente o número que
`json.JSONDecoder().raw_decode` devolve de graça, e que também resolve o fato
de metade dos arquivos em `logs/` estar identada em vez de um JSON por linha.

**`(session_id, line_num)` é chave natural e índice único.** `session_id` é o
`%c` do Postgres e `line_num` um contador por sessão: o par é único no servidor
inteiro. Isso torna re-ingerir um arquivo uma operação segura, e é o que o
`ON CONFLICT` usa.

Esse mesmo índice serve a correlação plano → query. A ordem real no log é
`bind → plano(s) → execute`, e é o `execute` **imediatamente seguinte** que
carrega o `query_id` certo:

```sql
SELECT record_type, query_id,
       lead(query_id) OVER (PARTITION BY session_id ORDER BY line_num) AS qid_real
  FROM monitoring.pg_log_event;
```

Correlacionar por `vxid` — como a seção sobre `query_id` mais acima sugere —
só funciona quando a transação tem uma query só. O `vxid` identifica a
transação inteira, e 95,6% dos pares `(session_id, vxid)` do corpus cobrem mais
de um `query_id`. Medido em 35.154 eventos ingeridos: 4.241 planos seguidos de
`execute`, 4.241 com `query_id` idêntico, zero divergência.

**`record_type` é coluna, não derivação.** 78% dos registros são
`parse`/`bind`/`execute` e 21,5% são plano. Sem separar, qualquer
`avg(duration_ms)` mistura o tempo de um `bind` (0,17 ms de média) com o de um
plano (132 ms) — o número que sai não significa nada.

**Não se guarda o evento inteiro em `jsonb`.** O `message` sozinho é metade dos
bytes de uma linha (medido: 50,2%), e ele já é coluna. Um `raw_event` completo
duplicaria justamente o que é grande. Só as chaves sem coluna própria vão para
`raw_extra`, e o corpus inteiro tem 19 chaves distintas.

**Índice parcial em `query_id <> 0`, não `IS NOT NULL`.** O Postgres escreve
`"query_id": 0` quando não há id — 25,8% dos registros — nunca ausente. Um
índice parcial com `IS NOT NULL` não excluiria uma linha sequer.

**Particionada por dia** porque o ciclo é rodar benchmark, medir e descartar:

```sql
SELECT * FROM monitoring.descartar_dia('2026-09-06');
```

Use essa, não `monitoring.drop_partition()`: a segunda só derruba
`pg_log_event` e não avisa `monitoring.query_resumo`, que ficaria contando
execuções de linhas que não existem mais.

### Descartar tudo e recomeçar

```bash
docker compose rm -sf monitoring && docker volume rm sindri_monitordata
docker compose up -d monitoring
```

## Acessar os bancos

Os dois estão publicados no host:

| | host | porta | banco | usuário | senha |
| --- | --- | --- | --- | --- | --- |
| monitoramento | localhost | **5433** | `brokk` | `brokk` | `brokk_dev_pwd` |
| observado (TPC-H) | localhost | 5432 | `tpch` | `sindri` | `sindri_dev_pwd` |

Pelo `psql`:

```bash
psql -h localhost -p 5433 -U brokk -d brokk
```

### pgAdmin 4 no navegador

```bash
docker compose --profile ui up -d pgadmin
```

http://localhost:5050 — sem tela de login, com as duas conexões já cadastradas
e **sem pedir senha**: elas vêm de um `.pgpass` preparado pelo serviço
`pgadmin-pass-init`.

Fica atrás de profile como o `benchbase`: é ferramenta, não parte do pipeline,
e um `docker compose up -d` normal não sobe.

Três detalhes que custam tempo se você for montar isso do zero:

- **Dentro do `servers.json` os hosts são `monitoring` e `postgres`, não
  `localhost`.** O pgAdmin roda na rede do compose, então alcança os bancos
  pelo nome do serviço na porta interna 5432 — o 5433 do host não existe para
  ele. É por isso que o erro de senha aparece com o IP interno (`172.19.0.3`).

- **Não use um domínio `.local` no `PGADMIN_DEFAULT_EMAIL`.** O pgAdmin valida
  o endereço contra a lista de TLDs de uso reservado, recusa-se a subir e entra
  em laço de reinício, com o erro escondido no `docker logs`.

- **O `.pgpass` precisa de um init container, não de um bind mount.** A libpq
  ignora arquivo de senha com leitura para grupo ou outros, e um bind mount
  vindo da WSL chega com o dono do host (uid 1000), não com o do pgAdmin
  (uid 5050). O `pgadmin-pass-init` escreve num volume nomeado e faz
  `chown 5050 && chmod 600` de dentro — mesmo padrão do `log-init`.

Duas senhas convivem aqui e é fácil trocá-las:
`PGADMIN_DEFAULT_PASSWORD: sindri` é a **do pgAdmin**; `brokk_dev_pwd` é a
**do Postgres**. Com o passfile no lugar, nenhuma das duas é digitada.

Do Windows, `localhost` resolve para a WSL nos dois casos (testado): pgAdmin em
5050 e Postgres em 5433.

## Alimentar o banco

`src/query_colector.py` lê `logs/` e alimenta o banco de monitoramento. É a
única parte do projeto com dependência externa (psycopg 3):

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

```bash
./.venv/bin/python src/query_colector.py              # ingere o que há de novo
./.venv/bin/python src/query_colector.py --seguir     # fica acompanhando
./.venv/bin/python src/query_colector.py --status     # só o estado, não ingere
./.venv/bin/python src/query_colector.py --reingerir  # zera os deslocamentos
```

Medido no corpus atual: **949 MB, 23 arquivos, 567.811 eventos em 47 s**
(~12 mil eventos/s), 467 MB no banco com os cinco índices. A segunda passada
leva 0,6 s e não insere nada.

O `--seguir` pode rodar junto com o `run-bench.sh`: ele relê a pasta a cada
intervalo e acompanha o arquivo que o Postgres ainda está escrevendo.

### Como ele não duplica e não perde

O deslocamento em bytes de cada arquivo fica em `ingestion_file.bytes_ingested`,
gravado junto com o lote na mesma transação. Interromper com Ctrl-C no meio de
um arquivo de 50 MB não perde nem repete nada: a execução seguinte retoma do
último lote que fechou.

O deslocamento é uma **dica**, não uma garantia. A garantia é o índice único
`(session_id, line_num, event_time)`. Se o deslocamento cair no meio de um
registro — arquivo truncado, byte inválido, o que for — o leitor ressincroniza
procurando o próximo objeto e o banco descarta o que já tiver via
`ON CONFLICT DO NOTHING`. Por isso `--reingerir` é seguro: relê os 949 MB e
insere zero linha.

### Duas coisas que o corpus obrigou a tratar

**O nome do arquivo não diz o dia dos eventos.** A rotação por idade cria o
arquivo à meia-noite, mas se o servidor estiver ocioso ele fica vazio até a
próxima escrita. No corpus atual, `postgresql-2026-09-07_000000.json` tem um
único evento, e ele é de **09-09**. Por isso a partição segue o `event_time` de
cada registro, nunca o nome do arquivo.

**O `application_name` tem coluna própria.** Parece detalhe, mas foi o bug que
mais custou: esquecê-lo na lista de chaves mapeadas fazia 84 mil linhas
ganharem um `raw_extra` redundante — exatamente a duplicação que o desenho
existe para evitar. No corpus inteiro, as únicas chaves que sobram para
`raw_extra` são `statement` (33 ocorrências) e `cursor_position` (4).

### O que dá para perguntar depois de ingerir

A separação por `record_type` é o que torna esta pergunta honesta — sem ela a
média misturaria o `bind` (0,17 ms) com o plano:

```sql
SELECT database_name, count(*) AS planos,
       round(avg(duration_ms)::numeric, 3) AS media_ms
  FROM monitoring.pg_log_event
 WHERE record_type = 'plan'
 GROUP BY 1 ORDER BY 2 DESC;
```

```
 database_name | planos | media_ms
---------------+--------+----------
 prova_esquema | 100107 |    0.217
 tpch          |  92399 |  272.235
```

## O worker de planos

`src/plan_worker.py` é a Etapa 2: lê os registros com `record_type = 'plan'`,
parseia o JSON do `auto_explain` que está no `message` e produz três tabelas
(`initdb-monitoring/04-worker-schema.sql`):

```
query            dimensão, uma linha por texto de query distinto
plan_execution   fato, uma linha por registro de plano
plan_node        uma linha por nó da árvore, com o pai
```

```bash
./.venv/bin/python src/plan_worker.py              # processa o que chegou
./.venv/bin/python src/plan_worker.py --achados    # o que o corpus revela
./.venv/bin/python src/plan_worker.py --status
./.venv/bin/python src/plan_worker.py --reprocessar
```

Medido no corpus: **193.087 planos → 458.696 nós e 167 queries distintas**,
a ~4.500 planos/s. `plan_execution` ocupa 78 MB e `plan_node` 152 MB, contra
478 MB do bruto. A segunda passada leva 0,35 s.

### A dimensão é chaveada pelo texto, não pelo query_id

O `query_id` do registro de plano está certo em 96,1% dos casos. Nos outros
3,8% não está, e a causa é mecânica: quando o cliente mantém o portal de um
`SELECT` aberto até o commit, o `auto_explain` só dispara no `ExecutorEnd`, que
acontece durante o processamento do `COMMIT` — e o registro sai com o
`query_id` do COMMIT.

O `Query Text`, esse o plano sempre carrega. Agrupar por hash do texto funciona
para 100% dos planos; o `query_id` fica como atributo, para juntar com
`pg_stat_statements` quando der.

`plan_execution.query_id_origem` diz de onde cada id veio:

| origem | planos | |
| --- | --- | --- |
| `proprio` | 185.529 (96,1%) | o registro já trazia o id certo |
| `indefinido` | 7.389 (3,8%) | o seguinte era um COMMIT |
| `seguinte` | 169 (0,1%) | veio do execute seguinte da sessão |

**O que não dá para resolver fica marcado, não chutado.** Testei o fallback que
a seção sobre `query_id` mais acima sugere — recuperar o id pelos `bind` do
mesmo `vxid`. Ele não funciona: dos 7.381 planos adiados, **todos** caem em
`vxid` que cobre mais de uma query, então não há como saber de qual delas o
plano é. O índice `ix_plan_execution_qid` exclui esses registros.

### Idempotência não para nas tabelas de fato

`--reprocessar` relê os 193.087 planos em 31 s e insere zero linha nova: o
`ON CONFLICT DO NOTHING` cuida dos fatos. Mas o contador `execucoes` da
dimensão não estava coberto por isso — ele era incrementado pelo tamanho do
lote, e um `--reprocessar` dobrava o número **sem duplicar um fato sequer**.
Um erro silencioso, porque as tabelas grandes continuavam certas.

A correção é contar o que o `INSERT` devolveu, não o que o lote trazia:

```sql
WITH inseridos AS (
    INSERT INTO monitoring.plan_execution (...)
    SELECT ... FROM tmp_execucao
    ON CONFLICT DO NOTHING
    RETURNING query_ref, event_time
)
UPDATE monitoring.query q SET execucoes = q.execucoes + a.n ...
```

Vale a regra geral: num pipeline reprocessável, todo contador denormalizado
tem que sair do `RETURNING`, não do lote de entrada.

### Contagem de linha é `double precision`, não `bigint`

O PG18 passou a emitir `Actual Rows` fracionário — é a média por loop. Um nó do
lado interno de um Nested Loop que devolve 1 linha a cada 3 execuções sai como
`0.33`. Arredondar jogaria fora justamente a resolução que torna o número útil
onde há muitos loops:

```
 plano                          | objeto               | estimado |    real | loops |      ms
--------------------------------+----------------------+----------+---------+-------+---------
 Aggregate                      |                      |        1 |       1 |     1 | 23023.1
    Hash Join                   |                      |     1960 |     512 |     1 | 23019.8
       Seq Scan                 | lineitem             |  6001215 | 6001215 |     1 |  2599.6
       Hash                     |                      |      196 |     187 |     1 |   298.8
          Gather                |                      |      196 |     187 |     1 |   298.5
             Seq Scan           | part                 |       82 |   62.33 |     3 |   128.3
       Aggregate                |                      |        1 |       1 |  5639 |     3.2
          Bitmap Heap Scan      | lineitem             |       32 |   31.23 |  5639 |     3.1
             Bitmap Index Scan  | idx_lineitem_partkey |       32 |   31.23 |  5639 |     0.1
```

`parent_idx` aponta para a posição do pai em pré-ordem dentro do mesmo
`event_id`, então a árvore volta com um `WITH RECURSIVE` sem depender de nada
externo — foi assim que a tabela acima foi gerada.

### Dois índices que o worker exigiu

Estão em `04-worker-schema.sql` porque são sobre a tabela bruta mas existem
para o worker. Sem eles, a leitura de um lote de 2.000 planos levava 273 ms;
com eles, **23 ms** — e custam 4 MB e 7 MB.

O primeiro (`id` parcial em `record_type = 'plan'`) evita ler 7.273 linhas por
filtro para achar 2.000 planos. O segundo (`session_id, line_num` parcial em
`execute`/`statement`) faz a busca do "próximo fim de statement da sessão"
parar na primeira entrada do índice.

### O que o corpus já revela

```
=== Seq Scan que joga linha fora: candidato a índice ===
  relation_name  ocorrencias  descartadas  ms      exemplo_de_filtro
  lineitem       2077         2840426      1966.7  ((l_shipdate >= $1) AND (l_shipdate < $2))
  orders         1703         696583       556.6   ((o_comment)::text !~~ ($1)::text)
  part           1909         102189       85.0    ((p_brand = ($1)::bpchar) AND (p_container = ...

=== Sort que derramou para disco ===
  sort_method     ocorrencias  kb_medio  kb_maximo
  external merge  1411         12162     105720
```

Os 2,84 milhões de linhas que o `Seq Scan` em `lineitem` descarta por
`l_shipdate` são o tipo de recomendação que o baseline enxuto de
`initdb/03-baseline-indexes.sql` existe para deixar em aberto. E os 1.411
`external merge` com média de 12 MB dizem que o `work_mem` do banco observado
está abaixo do que esta carga pede.

## O tabelão: `monitoring.query_resumo`

Uma linha por query, com tudo denormalizado — é onde a IA trabalha, e a
intenção é que se baste: sem join, sem window function, sem precisar entender o
modelo.

```sql
SELECT query_text, execucoes, duracao_media_ms, duracao_min_ms, duracao_max_ms,
       pior_fator_no, seq_scan_descartadas, sorts_em_disco, relacoes
  FROM monitoring.query_resumo
 WHERE database_name = 'tpch'
 ORDER BY duracao_total_ms DESC;
```

Ao contrário do resto do pipeline, esta tabela é alimentada por **trigger**.

### Por que trigger aqui, se em todo o resto eu evitei

Porque é a única forma de o resumo não *poder* divergir do fato, venha o
`INSERT` de onde vier — inclusive de um `psql` manual, ou de um segundo worker
que exista amanhã. E porque o custo que normalmente condena trigger em carga de
lote não se aplica: estas são de **statement** com transition table, disparam
uma vez por `INSERT` e não uma vez por linha.

Medido num backfill de 193.087 planos:

| | tempo |
| --- | --- |
| sem a trigger | 34,5 s |
| com a trigger | 34,1 s |

Dentro do ruído. Com trigger de linha seriam 193.087 disparos.

Há um bônus que não era esperado: com `ON CONFLICT DO NOTHING`, a transition
table contém **apenas as linhas que realmente entraram**. Verifiquei. Isso faz
a trigger ser imune por construção ao bug que me pegou no contador da dimensão
— um `--reprocessar` não move um número sequer aqui.

Foi por isso, aliás, que `monitoring.query` perdeu as colunas `execucoes`,
`primeiro_visto` e `ultimo_visto`: dois contadores para a mesma coisa é como um
deles fica errado sem ninguém perceber. A dimensão virou identidade pura e
`query_resumo` é o dono dos números.

### Duas classes de coluna, e a diferença importa

**Até `nos_total`** — mantidas por trigger, sempre em dia. Só entra aqui o que é
agregável de forma incremental: contagem, soma, mínimo e máximo. Mediana e
percentil **não** são, e por isso não existem: estariam sempre errados.

**De `analise_em` em diante** — vêm de `plan_node` (quais nós, quais relações,
quanto o Seq Scan descartou) e exigem varrer a árvore. O worker chama
`monitoring.analisar_resumo()` ao fim de cada execução, restrita às queries que
tocou. `--sem-analise` desliga.

### O buraco: `DROP` de partição não dispara trigger

Verificado: depois de um `DROP TABLE` cru numa partição, o resumo continuava
contando 200 execuções e um máximo de 999999 ms de linhas que não existiam
mais. Por isso:

```sql
SELECT * FROM monitoring.descartar_dia('2026-09-06');  -- use isto
SELECT count(*) FROM monitoring.conferir_resumo();     -- 0 = tudo certo
```

`descartar_dia()` derruba o dia nas três tabelas particionadas e reconstrói o
resumo. `conferir_resumo()` compara o resumo com `plan_execution` e devolve as
divergências — no teste ela detectou as 3 que o `DROP` cru causou, e zerou
depois da função.

`DELETE` e `TRUNCATE`, esses a trigger cobre. `DELETE` não decrementa: recalcula
por inteiro as queries afetadas, porque mínimo e máximo não voltam por
subtração — se a linha removida era a mais lenta, o novo máximo só se descobre
varrendo o resto.

### Uso de índice: `indices_usados` e `indices`

Duas colunas, calculadas juntas por `analisar_resumo()` — não podem divergir.
`indices_usados` é `text[]`, só os nomes, para filtro rápido
(`WHERE 'idx_x' = ANY (indices_usados)`). `indices` é `jsonb` com o detalhe:

```json
[{"indice": "idx_lineitem_partkey", "relacao": "lineitem",
  "tipos": ["Bitmap Index Scan"], "usos": 199,
  "linhas_media": 186720.4, "ms_total": 8.7,
  "cond": "(l_partkey = part.p_partkey)"}]
```

Um elemento por par **(índice, relação)**, porque o mesmo índice pode ser
varrido de mais de um jeito na mesma query. `[]` significa que a query não usou
índice nenhum — ausência medida, não informação faltando. No corpus, **42 das
117 queries do `tpch` não usam índice algum**.

O trabalho de verdade está no campo `relacao`, e ele existe por um detalhe do
EXPLAIN: **`Bitmap Index Scan` não informa a tabela**. Medido, 3.704 nós de
índice sem `Relation Name`, todos bitmap. Ela está no nó de cima — no
`Bitmap Heap Scan` (3.360 casos) ou, quando há um `BitmapAnd` no meio, no de
cima dele (344). Pai e avô cobrem 100%, e é por isso que
`monitoring.v_uso_de_indice` faz dois `LEFT JOIN` em vez de um `WITH RECURSIVE`.

Sem essa resolução, justamente os bitmap — que são os mais interessantes para
decidir se um índice vale a pena — apareceriam sem tabela.

A view também serve sozinha, um nó por linha:

```sql
SELECT i->>'indice' AS indice, i->>'relacao' AS relacao,
       count(DISTINCT r.query_ref) AS queries,
       round(sum((i->>'ms_total')::numeric)) AS ms_total
  FROM monitoring.query_resumo r, LATERAL jsonb_array_elements(r.indices) i
 WHERE r.database_name = 'tpch'
 GROUP BY 1, 2 ORDER BY ms_total DESC;
```

```
 indice                | relacao   | queries |  ms_total
-----------------------+-----------+---------+-----------
 idx_lineitem_orderkey | lineitem  |      12 |    735179
 partsupp_pkey         | partsupp  |       4 |     46776
 customer_pkey         | customer  |       3 |      7141
```

### A armadilha que ficou documentada no catálogo

`fator_estimativa` é do **nó de topo**, ou seja, do tamanho do resultado final.
Para query com `GROUP BY` o topo devolve poucas linhas e o número tende a zero
sem que haja nada errado. Quem acha plano ruim é `pior_fator_no`, que olha
todos os nós.

Como quem vai ler esta tabela é uma IA, isso e mais nove armadilhas estão em
`COMMENT ON COLUMN` (`initdb-monitoring/06-warehouse-doc.sql`) — é a única
documentação que ela alcança sozinha, com `\d+` ou lendo `pg_description`.

## O recomendador de índices

`src/index_provider.py` é a Etapa 3: pega o que o tabelão sabe, junta com o
catálogo real do banco observado e pede a Claude que proponha mudanças no
conjunto de índices.

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # ou preencha CHAVE_ANTHROPIC no topo
./.venv/bin/python src/index_provider.py --dossie          # só monta, não chama a API
./.venv/bin/python src/index_provider.py --csv rec.csv
./.venv/bin/python src/index_provider.py --planilha --compartilhar voce@gmail.com
```

A chave e o prompt ficam no topo do arquivo, como constantes `CHAVE_ANTHROPIC`
e `PROMPT_SISTEMA`.

### Por que ele leva números, e não o SQL

Um recomendador que só vê `CREATE INDEX ON lineitem (l_shipdate)` **chuta** o
tamanho. Este recebe:

```
lineitem_pkey           128.57 MB   22.5 B/linha   idx_scan=0
idx_lineitem_orderkey    74.28 MB   13.0 B/linha   idx_scan=111.224.522
lineitem.l_shipdate      avg_width=4  n_distinct=2511  correl=0.006
lineitem                 6.001.215 linhas   ins=6.001.215  upd=2.762
```

Com isso a estimativa de espaço é uma conta, não um palpite — e o esquema de
saída **obriga** a resposta a preencher `base_da_estimativa` com a conta que
fez. Se não der para estimar, a instrução é dizer isso ali em vez de inventar
um número.

O dossiê tem quatro partes, montadas de **dois bancos**:

| parte | fonte | o que carrega |
| --- | --- | --- |
| `workload` | `brokk:5433` | as queries mais caras, com os filtros de cada Seq Scan, quanto cada um descartou, junções, ordenações e índices já usados |
| `indices` | `tpch:5432` | definição, tamanho, `idx_scan` e se sustenta constraint |
| `tabelas` | `tpch:5432` | linhas, tamanho e atividade de escrita |
| `colunas` | `tpch:5432` | `pg_stats`: `avg_width`, `n_distinct`, correlação |

O campo `sustenta_constraint` existe para evitar a recomendação mais perigosa
que um modelo pode dar: remover um índice que existe por integridade e não por
desempenho. O `lineitem_pkey` do corpus é exatamente esse caso — 128 MB com
`idx_scan = 0`, e ainda assim não se remove.

### Tamanho e custo

As expressões de plano precisaram de teto por query (`EXPRESSOES_POR_QUERY`):
sem ele respondiam por mais de quatro quintos do dossiê. Com o corte, 25
queries dão **163 mil caracteres**, uns 41 mil tokens — na casa de US$ 0,20 por
análise com `claude-opus-5`.

### Google Sheets

Precisa de uma service account:

1. `console.cloud.google.com` → novo projeto
2. habilite **Google Sheets API** e **Google Drive API**
3. IAM → Contas de serviço → criar → Chaves → JSON
4. salve como `google-credentials.json` na raiz (o `.gitignore` já cobre)

O `--compartilhar` não é opcional na prática: a service account é dona do
arquivo e mais ninguém o enxerga, então sem ele a planilha existe e você não
consegue abrir.

Sem credencial, `--csv` faz o mesmo trabalho e não depende de nada.

### Sobre a chave no arquivo

`CHAVE_ANTHROPIC` está lá porque foi pedida ali, mas este arquivo é versionado.
Uma chave commitada é uma chave vazada — o GitHub varre pushes e a Anthropic
revoga o que aparece. As alternativas, que o código também aceita, são
`ANTHROPIC_API_KEY` no ambiente ou um `.env` na raiz; as duas ficam fora do git.

## Layout

| pasta | conteúdo |
| --- | --- |
| `initdb/` | scripts executados na primeira subida do container |
| `initdb-monitoring/` | schema do banco de monitoramento (porta 5433) |
| `pgadmin/` | conexões pré-cadastradas do pgAdmin (`--profile ui`) |
| `tpch-data/` | arquivos `.tbl` gerados pelo dbgen (SF=1) |
| `tpch-dbgen/` | fonte do gerador de dados oficial do TPC-H |
| `queries/` | as 22 queries TPC-H em SQL puro, para rodar via `psql` |
| `logs/` | jsonlog do Postgres com os planos do `auto_explain` |
| `benchbase/` | Dockerfile, configs (leitura e escrita) e resultados do gerador de carga |
| `src/` | código do BrokkPG (`query_colector.py`: ingestão; `plan_worker.py`: planos; `index_provider.py`: recomendação por IA; `schema_estimator.py`: esquema reconstruído pelos logs) |
