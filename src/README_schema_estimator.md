# `schema_estimator.py` — o estimador de esquema

Lê todos os arquivos de log em `logs/` e reconstrói o esquema do banco a partir
do que a carga revela — tabelas, colunas, tipos, índices, chaves primárias,
chaves estrangeiras e cardinalidade — **sem abrir conexão com o banco**. Não
depende de nada além da biblioteca padrão.

É o único dos quatro módulos que não faz parte do pipeline de ingestão: ele lê o
mesmo corpus, mas de forma independente, e o produto é um `.sql` e um `.json` de
evidência (veja `esquema_estimado.sql` na raiz).

## Hierarquia de evidência

Da mais forte para a mais fraca:

1. **DDL capturado no log.** Com `log_min_duration_statement=0` os comandos do
   `initdb/` também entram no corpus, então um log que cubra a primeira subida do
   container traz o esquema literal (`CREATE TABLE`, `ALTER TABLE ... ADD PRIMARY
   KEY`, `CREATE INDEX`, ...).
2. **Checagens de FK dos triggers RI.** O Postgres dispara
   `SELECT 1 FROM ONLY "public"."x" WHERE "col" = $1 FOR KEY SHARE OF x` a cada
   escrita em tabela ligada por chave estrangeira. Elas provam que a FK existe e
   dizem de que lado a coluna está: coluna antes do `$n` é o lado referenciado
   (checagem no pai), `$n` antes da coluna é o lado que referencia.
3. **Os planos do `auto_explain`.** `Relation Name`/`Alias` dão as tabelas,
   `Index Name` + `Index Cond` dão os índices e suas colunas, as expressões
   (`Filter`, `Hash Cond`, `Sort Key`, ...) dão colunas, tipos — pelos casts que
   o próprio planner imprime — e as junções que viram FK candidata.
   `Plan Rows`/`Actual Rows` dão a cardinalidade.
4. **O texto SQL e os parâmetros vinculados.** A lista de colunas de um `INSERT`
   dá a ordem real das colunas; `Query Parameters` dá valores de verdade, que
   tipam o que o plano deixou sem cast.

Cada tipo de evidência tem um peso em `PESO`, que vira a "confiança" do
relatório:

| fonte | peso | exemplo |
| --- | --- | --- |
| `ddl` | 1.00 | `CREATE TABLE` literal |
| `cast` | 0.90 | `(col = 'x'::bpchar)` — anotação do próprio planner |
| `array` | 0.85 | `(col = ANY ('{...}'::name[]))` |
| `parametro` | 0.70 | `(col = $1)` com `$1 = '1996-10-09'` |
| `literal_sql` | 0.65 | `INSERT ... VALUES ('O', ...)` |
| `numero` | 0.60 | `(col >= 15)` |
| `juncao` | 0.55 | tipo herdado do outro lado de `(a.x = b.y)` |
| `aritmetica` | 0.50 | `('1'::numeric - col)` |
| `cast_saida` | 0.35 | `(col)::double precision` — dá a família, não o tipo |
| `like` | 0.30 | `(col ~~ '%x%')` |

## Estruturas

- **`Coluna`** — guarda `votos: Counter[(tipo, fonte)]`, exemplos de valor,
  tamanho mínimo e máximo observados, e o que veio do DDL (`tipo_ddl`,
  `tipo_serial`, `default_ddl`, `not_null`).
- **`Indice`** — nome, tabela, colunas na ordem em que apareceram, `unico`,
  `primaria`, `usos` e o conjunto de `fontes` (`plano`, `ddl`, `colunas da PK`).
- **`ChaveEstrangeira`** — origem, destino, confiança e a lista de evidências
  que a sustenta.
- **`Tabela`** — espécie (tabela / view / view materializada / cte / rotina),
  colunas, ordem vinda do DDL e a vinda de `INSERT`, índices, PK, FKs,
  cardinalidade por dois caminhos (`linhas_planner`, `linhas_lidas`), contadores
  de operação e de tipo de nó, e os dois contadores de trigger RI
  (`ri_referenciada`, `ri_referenciadora`).

`Coluna.resolver(usar_ddl)` é onde o tipo é decidido: com DDL, ele ganha e pronto
(confiança 1.0). Sem DDL, cada tipo fica com o **maior peso** que alguma fonte
lhe deu; o líder é o de maior peso, desempatado por número de ocorrências;
candidatos dentro de 0,2 de peso do líder disputam. Na família numérica vence o
tipo mais **geral** (`GENERALIDADE`): comparar uma coluna `numeric` com literal
inteiro é comum demais para deixar `integer` ganhar. Nas outras famílias
generalizar erra mais do que acerta — uma coluna `date` comparada com timestamp
(o planner promove sozinho) continua `date`.

## Fluxo

```
iter_registros(arquivo)        um dict por registro do jsonlog
      │
      ▼
Estimador.consumir_registro    filtra por --banco; separa plano de statement
      ├── consumir_plano       Query Text + Query Parameters + árvore
      └── consumir_sql         DDL, checagem RI, INSERT, UPDATE, FROM/JOIN
      ▼
Estimador.finalizar()          pendentes → junções → espécies → PKs → FKs
      ▼
gerar_relatorio / gerar_ddl / gerar_json
```

`iter_registros()` usa o mesmo truque do coletor — `raw_decode` sobre um buffer
deslizante — para ler tanto o jsonlog cru quanto o já identado, e ressincroniza
no próximo `"\n{"` quando encontra lixo.

### Travessia do plano

`consumir_plano()` primeiro manda o `Query Text` para `consumir_sql()`, depois
percorre a árvore. `_mapear_aliases()` monta `alias -> relação` antes da
travessia (apelido reaproveitado para relações diferentes vira `None`, inútil).
Se o plano toca uma relação só, ela vira `_relacao_unica`: coluna sem
qualificação num `Sort Key` ou `Group Key` só pode ser dela. Com duas ou mais, a
coluna fica pendente até a atribuição por nome único no fechamento.

`_percorrer()` visita cada nó: registra a relação, o tipo de nó, a cardinalidade,
as expressões de `CAMPOS_EXPRESSAO`, o índice de `Index Name`, as funções de
`Function Scan` e os nomes de `CTE Scan`. `Bitmap Index Scan` não repete a
relação, então ela é herdada do pai.

`_cardinalidade()` só confia em `Seq Scan` não paralelo com `Actual Loops = 1`:
`Actual Rows + Rows Removed by Filter` é quantas linhas a tabela tem. Sem
`Filter`, `Plan Rows` também vale (é a estimativa do planner sobre a tabela
inteira).

`_indice_do_plano()` marca como primário o índice cujo nome termina em `_pkey` e
como único o que termina em `_key` — a unicidade em si não aparece em plano
nenhum, só na convenção de nome que o Postgres usa. As colunas vêm dos lados
esquerdos do `Index Cond`.

### Análise de expressão

`analisar_expressao()` (com `lru_cache` de 8192 — o corpus repete muito) extrai
de uma string de expressão: as referências `(alias, coluna)`, os votos de tipo
com sua fonte, os pares `(coluna, $n)`, as junções `a.x = b.y` e os lados
esquerdos das comparações (que são o que vira coluna de índice). Antes de
procurar identificadores, `_mascarar()` apaga literais e nomes de tipo, que não
são colunas. `PALAVRAS_NAO_COLUNA` filtra o resto: palavras-chave de SQL, campos
de `EXTRACT`, e todo nome de tipo conhecido.

O caso `(col)::tipo` é tratado à parte: a coluna **não** é daquele tipo — é o
planner convertendo a saída — mas a família serve, e o voto entra com peso baixo
(`cast_saida`).

### O que vem do texto SQL

`consumir_sql()` limita-se a `amostras_por_texto` (3) execuções do mesmo texto:
depois disso ele já disse o que tinha para dizer. Por texto novo:

- `CREATE`/`ALTER` → `_consumir_ddl()`: `CREATE TABLE` (com `corpo_parenteses()`
  para achar o corpo e `RE_COLUNA_DDL`/`RE_MODIFICADOR` para cada coluna),
  `CREATE VIEW`, rotinas, `CREATE INDEX`, `ADD PRIMARY KEY`, `UNIQUE`,
  `FOREIGN KEY` e `REFERENCES` inline;
- `_consumir_checagem_ri()` → alimenta `ri_referenciada` / `ri_referenciadora` e
  descobre o nome do schema;
- `_consumir_insert()` → a ordem real das colunas e o tipo pelos valores;
- `_consumir_update()` → colunas escritas e tipo pelos valores;
- `RE_ALVO_ESCRITA` → conta insert/update/delete por tabela;
- `RE_FROM_JOIN` + `RE_CTE` → nomes citados no FROM que nunca aparecem como
  relação de plano (candidatos a view), descontadas as CTEs.

`_tipar_por_valor()` reconhece três formas: `$n` resolvido pelos parâmetros
vinculados, literal entre aspas e número cru. O tipo sai do **formato** do valor
(`RE_VAL_INTEIRO`, `RE_VAL_DECIMAL`, `RE_VAL_DATA`, `RE_VAL_TIMESTAMP`), e o
valor também vira exemplo e alimenta o tamanho mínimo/máximo da coluna.

### `finalizar()`

Cinco passos, nesta ordem:

1. **`_resolver_pendentes()`** — coluna sem dono só é aceita quando o nome existe
   em exatamente uma tabela conhecida; o resto é descartado e contado.
2. **`_propagar_tipos_por_juncao()`** — uma coluna comparada por igualdade com
   outra tem o tipo dela. Três rodadas, mínimo de 3 observações, e sempre a
   partir do tipo **inferido pela carga** (nunca do DDL), para que a conferência
   continue medindo o que a carga sozinha alcança. É o que fecha as chaves: num
   schema deste feitio a maior parte das colunas nunca aparece comparada com
   literal, só em junção.
3. **`_definir_especies()`** — quem aparece como relação em plano é "tabela ou
   view materializada"; quem só aparece no FROM é view (o planner expande a
   definição da view dentro da consulta, então ela some do plano — é assim que dá
   para separá-la de uma tabela); o resto é CTE, rotina ou relação.
4. **`_definir_pks()`** — DDL primeiro; senão, índice `*_pkey` visto em plano;
   senão, índice único vindo de DDL. Índice primário usado sem `Index Cond`
   (varredura completa do índice) não mostra coluna nenhuma, e nesse caso as
   colunas da PK são copiadas para ele.
5. **`_definir_fks()`** — as declaradas em DDL entram com confiança 1.0. As
   observadas são agrupadas por par de tabelas, e `_direcao()` decide quem
   referencia quem: (a) trigger RI é prova; (b) o lado que é chave única é o pai
   — com a ressalva de que unicidade vista só em plano pode ser de índice
   composto observado pela metade, e nesse caso o tamanho das tabelas desempata;
   (c) sem isso, quem tem mais linhas é quem referencia. A confiança sai 0.35
   (só junção), 0.60 (lado referenciado é chave única) ou 0.90 (checagem de
   trigger RI nos dois lados).

## Saídas

- **relatório no stdout** (padrão): corpus lido, contagem de registros, o que o
  DDL trouxe, as relações físicas com colunas, tipos, confiança, fonte, exemplos,
  índices e FKs; depois as outras relações citadas, as CTEs e rotinas; e, quando
  o corpus tem DDL, a **conferência**.
- **`--sql ARQ`** (`gerar_ddl`): o `CREATE TABLE` estimado, cada item com a
  evidência que o sustenta em comentário. Coluna sem tipo inferido sai comentada,
  e toda restrição que dependa dela sai comentada junto — senão o arquivo não
  roda.
- **`--json ARQ`** (`gerar_json`): a evidência completa, inclusive todos os votos
  por coluna e o tipo que a carga inferiria sem o DDL.

`conferir_ddl()` é a autoavaliação: para cada coluna com tipo declarado, compara
o tipo que a carga sozinha inferiria com o que o DDL diz, e classifica em acerto,
mesma família, errado ou sem evidência. É o que mede o que o estimador
conseguiria num corpus que não tivesse capturado o DDL.

## Uso

```bash
python3 src/schema_estimator.py                     # relatório no stdout
python3 src/schema_estimator.py --sql esquema.sql   # + DDL estimado
python3 src/schema_estimator.py --json evidencias.json
python3 src/schema_estimator.py --ignorar-ddl       # só o que a carga revela
```

| flag | efeito |
| --- | --- |
| `--logs` / `--padrao` | pasta e glob (padrão `./logs`, `*.json`) |
| `--sql` / `--json` | arquivos de saída |
| `--esquema` | nome do schema no DDL gerado (padrão: o visto no log) |
| `--banco` | considera só os registros deste `dbname` |
| `--ignorar-ddl` | descarta a evidência de DDL |
| `--incluir-sistema` | não filtra as relações de catálogo (`pg_*`) |
| `--limite` | lê no máximo N registros por arquivo (para testes) |
| `--sem-relatorio` / `--quieto` | silenciam o stdout e o progresso no stderr |

`--banco` importa quando o corpus cobre mais de um banco: o mesmo servidor serve
todos, e misturar dois esquemas com nomes de tabela iguais dá um Frankenstein. O
relatório avisa quando isso acontece.

`--ignorar-ddl` existe para medir o próprio estimador: joga fora a evidência mais
forte e mostra até onde a carga sozinha chega.

## Limites conhecidos

- Índice descoberto só por plano tem as colunas que apareceram em `Index Cond`,
  que podem ser só as iniciais de um índice composto. Por isso `_unicidade()`
  devolve também se a evidência veio do DDL — unicidade observada em plano é
  palpite, não fato.
- `RE_FROM_JOIN` descarta o `FROM` que não é cláusula (`EXTRACT(year FROM col)`,
  `TRIM(... FROM x)`) ao custo de perder o último item de uma subconsulta
  (`... FROM lineitem)`), que costuma aparecer em outro lugar do corpus.
- Corpo de função não é analisado: os statements internos chegam sozinhos no log.
- Tudo é minúsculo. Identificador com maiúscula entre aspas é desaspado e
  rebaixado.
