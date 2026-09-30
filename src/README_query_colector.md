# `query_colector.py` — o coletor

Etapa 1 do pipeline. Lê os arquivos de jsonlog que o Postgres observado escreve
em `logs/` e grava **o evento bruto** no banco de monitoramento (`brokk`, porta
5433). Ele não interpreta plano nenhum: separa o que é `plan`, `bind`, `parse`,
`execute` ou `statement`, guarda o payload inteiro e termina. Quem lê o JSON do
`auto_explain` é o `plan_worker.py`, depois.

```
logs/postgresql-*.json  ──▶  query_colector.py  ──▶  monitoring.pg_log_event
```

## O contrato com o banco

As funções estão em `initdb-monitoring/02-functions.sql`. Por lote, nesta ordem:

1. `INSERT` em `monitoring.ingestion_file` → devolve `file_id` e o deslocamento
   já ingerido (`registrar_arquivo`, `SQL_UPSERT`);
2. `COPY monitoring.pg_log_event_staging FROM STDIN` com as tuplas do lote;
3. `SELECT * FROM monitoring.ingest_staging(file_id)` → move da staging para a
   tabela final e devolve `(loaded, skipped)`;
4. `UPDATE` do deslocamento em `monitoring.ingestion_file` (`SQL_FECHAR`).

Os passos 2 a 4 vão dentro de `with self.conn.transaction()` em `_gravar()`. Se
o processo morrer no meio de um arquivo, a próxima execução retoma do último
lote que fechou — não do começo.

## Como a leitura funciona

`LeitorJsonlog.lotes()` é o coração do arquivo. Três problemas do corpus
explicam o formato dele:

**Os dois formatos de log convivem.** Metade dos arquivos em `logs/` é jsonlog
cru (um objeto por linha), metade está identada em dezenas de linhas.
`json.JSONDecoder.raw_decode` sobre um buffer deslizante lê os dois sem precisar
saber a diferença — ele consome um objeto a partir de uma posição e devolve onde
parou. Não há `splitlines()` em lugar nenhum.

**O arquivo corrente ainda cresce.** A rotação é por 50 MB, então sempre há um
arquivo aberto sendo escrito enquanto o coletor lê. Por isso o controle é
`bytes_ingested` — um deslocamento em bytes que vai para o `seek()` da próxima
passada — e não um status de "já li este arquivo". O arquivo é aberto em binário
e decodificado com `codecs.getincrementaldecoder("utf-8")`, para que um
caractere multibyte partido no limite do bloco de 1 MB não desalinhe a contagem.

**O deslocamento é uma dica, não um dogma.** A garantia de não duplicar vem do
índice único `(session_id, line_num, event_time)` no destino — a chave natural
do log do Postgres. Quando o `raw_decode` falha e o buffer já é grande demais
para ser um registro partido (`LIMITE_REGISTRO`, 8 MB; o maior `message` do
corpus tem 45 KB), o leitor conta um `registros_ilegiveis`, procura o próximo
`"\n{"` e continua dali. Um deslocamento gravado no meio de um registro se
recupera sozinho em vez de travar o arquivo para sempre.

No fim do arquivo o espaço em branco que sobra (o `\n` final) também é somado ao
deslocamento — sem isso `bytes_ingested` nunca alcança `file_size_bytes` e o
arquivo parece eternamente parcial.

## Classificação do `message`

`classificar()` separa `(record_type, duration_ms, payload)`. O prefixo
`duration: X ms  ` aparece em 99,7% dos registros do corpus, com cinco
continuações:

| forma no log | `record_type` | payload guardado |
| --- | --- | --- |
| `duration: 132 ms  plan:\n{...}` | `plan` | o JSON do plano |
| `duration: 0.1 ms  statement: SELECT ...` | `statement` | o SQL |
| `duration: 0.2 ms  execute S_1: SELECT ...` | `execute` | `S_1: SELECT ...` |
| `duration: 0.17 ms  bind S_1: ...` | `bind` | idem |
| `duration: 0.3 ms  parse S_1: ...` | `parse` | idem |
| qualquer outra | `other` | o resto, sem jogar nada fora |

Separar isso na ingestão é o que evita que um `avg(duration_ms)` adiante misture
o tempo de um `bind` (0,17 ms de média) com o de um plano (132 ms) — 78% dos
registros são parse/bind/execute e só 21,5% são plano. O nome do portal fica
dentro do payload; não há coluna para ele, e o worker separa no primeiro `": "`.

## De evento JSON para linha da staging

`linha_da_staging()` monta a tupla do `COPY` na ordem exata de
`COLUNAS_STAGING` — as duas têm que ser editadas juntas.

- `momento()` converte `"2026-09-06 21:13:40.482 UTC"` em `datetime` com fuso.
  Qualquer sufixo que não seja UTC/GMT é contado em `fuso_inesperado` e tratado
  como UTC: errar o fuso em silêncio deslocaria as partições diárias.
- Registro sem `timestamp`, `session_id` ou `line_num` é descartado
  (`sem_chave_natural`): sem os três não há como deduplicar, e é melhor contar
  do que inserir.
- Toda chave do jsonlog fora de `CHAVES_MAPEADAS` vai para `raw_extra` (jsonb).
  Na prática fica quase sempre nulo — o corpus tem 19 chaves distintas e todas
  estão mapeadas.

## Estado por arquivo

`monitoring.ingestion_file` guarda uma linha por `file_path`. O caminho gravado
usa `--prefixo` (padrão `/var/log/postgresql`), que é como o container do banco
de monitoramento enxerga a pasta `logs/` — assim o registro significa a mesma
coisa dos dois lados, independentemente de onde o coletor rode.

`registrar_arquivo()` zera o deslocamento em dois casos:

- `head_fingerprint` mudou — sha256 dos primeiros 64 KB. Hash do arquivo inteiro
  não serviria, porque ele cresce; o começo não muda. Detecta nome reaproveitado
  para conteúdo diferente.
- `bytes_ingested > file_size_bytes` — o arquivo encolheu (truncado por fora, ou
  `log_truncate_on_rotation`).

`Coletor.rodada()` trata o **último arquivo em ordem alfabética** como o que
ainda está aberto: o Postgres nomeia por timestamp, então todos os outros já
rotacionaram e podem ser fechados com `status = 'completed'`.

Erro durante um arquivo faz `rollback`, grava `status = 'failed'` com a mensagem
em `error_message` e segue para o próximo — uma passada não morre inteira por
causa de um arquivo ruim.

## Uso

```bash
./.venv/bin/python src/query_colector.py                 # ingere o que há de novo
./.venv/bin/python src/query_colector.py --seguir        # fica acompanhando
./.venv/bin/python src/query_colector.py --status        # só mostra o estado
./.venv/bin/python src/query_colector.py --reingerir     # zera os deslocamentos
./.venv/bin/python src/query_colector.py --limite 5000   # para testes
```

| flag | efeito |
| --- | --- |
| `--logs` / `--padrao` | pasta e glob dos arquivos (padrão `./logs`, `*.json`) |
| `--dsn` | conexão com o monitoramento (ou a variável `BROKK_DSN`) |
| `--prefixo` | caminho da pasta como o container do banco a enxerga |
| `--lote` | eventos por transação (padrão 20000) |
| `--seguir` / `--intervalo` | relê a pasta a cada N segundos |
| `--reingerir` | zera `bytes_ingested` de todos os arquivos e relê tudo |
| `--status` | imprime o estado da ingestão e sai |
| `--silencioso` | não imprime linha por arquivo |

`--reingerir` é seguro: o índice único descarta o que já está no banco, e os
repetidos aparecem na coluna "repetidos" do relatório.

`Ctrl-C` é tratado — o deslocamento do último lote fechado já está gravado.

## Dependências e armadilhas

- `psycopg` 3 (`pip install 'psycopg[binary]'`). O escaping do `COPY` fica com o
  driver de propósito: o payload de um plano é JSON com barras invertidas e
  quebras de linha dentro, e montar isso à mão é onde esse tipo de coletor
  costuma quebrar.
- Rode de dentro da WSL. Pelo caminho UNC do Windows os bind mounts relativos do
  compose não resolvem.
- Se `--padrao` pegar o `.log` em texto puro, cada linha vira
  `registros_ilegiveis` — `"2026-09-06 ..."` decodifica como o número `2026`, que
  não é `dict`, e é descartado sem quebrar nada.
