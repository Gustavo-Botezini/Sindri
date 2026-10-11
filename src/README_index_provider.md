# `index_provider/` — o recomendador de índices

Etapa 3 do pipeline. Pega o que o tabelão já sabe, junta com o catálogo real
do banco observado e propõe mudanças no conjunto de índices.

São **dois modos**, e a diferença entre eles é o que sai no fim.

| modo | subcomandos | saída |
| --- | --- | --- |
| clássico | `dossie`, `recomendar` | uma lista de recomendações, com espaço e ganho **estimados** |
| clusters | `vetorizar` → `agrupar` → `candidatar` → `medir` → `relatar` | o ganho **medido** de um índice real, query por query |

Mais `exportar` (o agrupamento em CSV/JSON), `esquema` e `limpar`.

O modo clássico é o `src/index_provider.py` de antes, redistribuído em
módulos sem mudança de comportamento. O modo de clusters fecha o ciclo que
ele deixa aberto: nenhuma recomendação dele é aplicada, nenhum ganho é
medido, e a saída é uma opinião com números de entrada bons e nenhum número
de saída.

```
monitoring.query_resumo
        │  texto das queries
        ▼
1. vetorizar   Qwen3-Embedding-0.6B                -> query_embedding
        ▼
2. agrupar     UMAP + HDBSCAN                      -> rodada_cluster, query_cluster
        ▼
3. candidatar  heurística propõe, Claude escolhe   -> experimento
        ▼
4. medir       CREATE INDEX, replay, DROP          -> experimento_medida
        ▼
5. relatar     experimento_veredito                -> CSV / Sheets
```

## A hipótese em teste, e ela pode ser falsa

O modo de clusters existe para responder uma pergunta, não para produzir uma
lista: **um índice derivado de uma query serve às outras queries do mesmo
cluster semântico?**

Por isso a bancada mede o efeito em *todas* as queries do cluster, inclusive
as que pioram, e por isso `monitoring.experimento_veredito` separa
`ganho_fora_da_origem_ms` do ganho total. A query de onde o candidato saiu vai
melhorar quase por construção; só o que sobra depois de tirá-la responde à
pergunta.

**O embedding é o particionador, não o critério.** Quem decide o índice
continua sendo o número medido em `plan_node` — quantas linhas aquele Seq Scan
descartou, quanto tempo custou. O vetor do texto só diz quais queries têm
forma parecida e portanto merecem ser testadas juntas.

## O corpus: 117 queries viram 27

`corpus.py` aplica três cortes, e cada um tira uma coisa diferente:

| corte | de | para | por quê |
| --- | --- | --- | --- |
| só leitura | 117 | 96 | a bancada reexecuta cada query; não se replica `INSERT`/`DELETE` numa base que é o objeto do experimento |
| `execucoes >= 5` | 96 | 69 | abaixo disso é acidente de execução, não carga |
| sem catálogo | 69 | 27 | 30 das 69 são introspecção do pgAdmin e do driver JDBC sobre `pg_class`, `pg_attribute` e afins |

O corte do catálogo é o que mais importa e o menos óbvio. São três famílias de
nove queries que variam só nos literais: sem tirá-las, elas formam três
clusters densos e inúteis que abafam a carga analítica de verdade — e ninguém
indexa `pg_catalog` (o próprio `PROMPT_SISTEMA` já proíbe recomendar isso).
`--incluir-catalogo` desfaz o corte.

As 27 que sobram são a carga real: as 22 do TPC-H, as consultas complementares
e as três sondagens de chave estrangeira que a carga de escrita gera.

## Normalização

O trabalho que inspirou este modo passou o texto integralmente, sem
normalização sintática. Aqui isso não serve: o corpus tem a mesma query em
duas grafias, minúscula (do benchmark `tpch`) e maiúscula (do `templated`).
Sem colapsar espaço e caixa, o HDBSCAN tem material para agrupar por estilo de
formatação em vez de por forma da query.

`espaco+caixa` é o padrão; `bruto` e `espaco` existem para rodar o experimento
nas três condições e comparar. O cache de vetores é chaveado por
`(query_ref, modelo, normalizacao)` justamente para que trocar de normalização
não reaproveite o vetor errado.

## Outras representações: só FROM e WHERE

O texto completo agrupa queries que *falam* parecido, e o SELECT é a maior
parte da fala: Q1, Q6, Q14 e Q17 caem juntas por causa de
`sum(l_extendedprice * (1 - l_discount))`, que nenhum índice toca. Dois modos
de `--normalizacao` trocam a representação, e não só a grafia. Os dois vêm de
`estrutura.py`, que usa o pglast (o parser do próprio Postgres) sobre o SQL
original:

| método | `--normalizacao` | `--modelo-embedding` | o que vira vetor |
| --- | --- | --- | --- |
| baseline | `espaco+caixa` | Qwen (padrão) | o SQL inteiro |
| A | `from-where` | Qwen (padrão) | só FROM, JOIN/ON e WHERE, recursivo nas tabelas derivadas |
| B | `estrutura` | `tfidf` | a assinatura de atributos, em TF-IDF binário |

A assinatura da B guarda o papel e, nos filtros, o operador:

```
FROM:lineitem  JOIN:lineitem.l_partkey  JOIN:part.p_partkey
FILTER:lineitem.l_quantity:<  FILTER:part.p_brand:=
```

`IN (subquery)` e `EXISTS` correlacionado entram como junção (são
semijunções); a autojunção `l1.l_orderkey = l2.l_orderkey` da Q21 também,
porque a decisão é por instância, não por nome de tabela. Colunas sem
qualificador são resolvidas pelo catálogo do banco observado, subindo pelos
escopos.

```bash
./.venv/bin/python -m src.index_provider vetorizar --normalizacao from-where
./.venv/bin/python -m src.index_provider vetorizar --normalizacao estrutura --modelo-embedding tfidf
./.venv/bin/python -m src.index_provider agrupar --normalizacao estrutura --modelo-embedding tfidf
```

A bancada não muda: ela reexecuta `Query.texto`, o SQL original, e nunca a
representação.

## Os parâmetros do UMAP e do HDBSCAN

Com 27 pontos, os defaults das bibliotecas não servem:

| parâmetro | flag | valor | por quê |
| --- | --- | --- | --- |
| `min_dist` | `--min-dist` | `0.0` | o padrão 0.1 existe para desenho, para os pontos não se empilharem. A projeção aqui alimenta um algoritmo de densidade — espalhar destrói o que ele vai procurar |
| `n_neighbors` | `--vizinhos` | `5` | o padrão 15 faria cada vizinhança cobrir mais da metade do corpus |
| `n_components` | `--dimensoes` | `5` | clusteriza em 5D; a projeção 2D é separada, mesma semente, e serve só ao gráfico |
| `min_cluster_size` | `--tamanho-minimo` | `3` | |
| `cluster_selection_method` | `--selecao` | `eom` | `eom` fica com o grupo mais estável da árvore; `leaf`, com as folhas. Com `eom`, `min_cluster_size` só muda o resultado quando passa do tamanho de um grupo já escolhido |
| `semente` | `--semente` | `42` | fixa, e gravada em `rodada_cluster.parametros` |

Para chegar perto da configuração do Expeditus, que não publica a sua mas
desenha a Figura 3 com a projeção 2D padrão: `--vizinhos 15 --dimensoes 2
--min-dist 0.1 --tamanho-minimo 2`.

HDBSCAN vem do `sklearn.cluster` (>= 1.3), não do pacote `hdbscan` avulso —
que exigiria compilar Cython no venv.

**27 pontos é pouco e o resultado é sensível aos parâmetros.** É por isso que
cada rodada guarda todos eles ao lado do resultado: sem isso um cluster é um
número que apareceu; com isso é um experimento que outra pessoa refaz.

### Olhar o agrupamento sem abrir o banco

`agrupar` grava em tabela, não em arquivo — é o que permite comparar duas
rodadas com parâmetros diferentes. Para conferir de relance:

```bash
./.venv/bin/python -m src.index_provider agrupar --listar          # as rodadas
./.venv/bin/python -m src.index_provider exportar --rodada 1 --csv clusters.csv
./.venv/bin/python -m src.index_provider exportar --rodada 1 --json clusters.json
```

O CSV tem uma linha por query, com a coluna `texto_embedado`: é literalmente a
string que virou vetor, já normalizada. É ela que responde "por que esta query
caiu neste cluster" — o SQL original, com as quebras de linha, não caberia numa
célula de planilha. O JSON leva junto os parâmetros da rodada, que é o que
torna o resultado reproduzível por outra pessoa.

### As figuras

```bash
./.venv/bin/python -m src.index_provider grafico --rodada 1 --projecoes
```

Duas saídas em PNG: um painel por cluster (mais um da visão geral) e, com
`--projecoes`, a mesma carga sob UMAP, PCA e t-SNE lado a lado — as Figuras 3
a 5 do artigo de referência.

Três decisões que a figura carrega:

**Painéis, não um gráfico só com as cores todas.** Num diagrama de dispersão
quaisquer dois pontos podem encostar, então *todos* os pares de cores precisam
ser distinguíveis, não só os vizinhos numa legenda. Rodado o validador de
paleta em modo `--pairs all`, nenhum conjunto de cinco cores passa: o pior par
cai em ΔE 12,9 de visão normal, abaixo do piso de 15. Com quatro ou mais
séries nessa forma, a saída é facetar. Cada painel usa então uma cor só, o que
passa nas seis checagens.

**Zoom por cluster.** Na escala do corpus inteiro um cluster é um borrão de
poucos pixels onde nenhum rótulo cabe. Cada painel enquadra o seu grupo; o
painel de visão geral responde onde ele fica.

**Rótulo em cada ponto.** As Figuras 3 a 5 do artigo não rotulam ponto nenhum
— dá para ver que há grupos, não quais queries estão em cada um. Aqui o rótulo
sai do casamento do texto da query com `queries/q*.sql`, por similaridade de
Jaccard sobre os identificadores (igualdade exata não casaria: no corpus os
literais viraram `$n`). Quem não casa fica com `#<query_ref>`.

Direto no banco, se preferir:

```sql
SELECT cluster_id, query_ref, probabilidade, x, y
  FROM monitoring.query_cluster WHERE rodada_id = 1
 ORDER BY cluster_id, query_ref;

SELECT jsonb_pretty(parametros) FROM monitoring.rodada_cluster WHERE id = 1;
```

### A conferência do agrupamento

`agrupar` termina procurando pares de vetores com cosseno ≥ 0.98 que caíram em
clusters diferentes. Não é circular: o HDBSCAN decide no espaço projetado pelo
UMAP, não no original, então um par quase idêntico separado significa que a
projeção rasgou o que estava junto — parâmetro errado, não estrutura.

Na rodada de referência o resultado bate com o que se esperaria de quem
conhece o TPC-H: 6 clusters, nenhum ruído, e o cluster 0 são exatamente as três
sondagens de FK (`SELECT 1 FROM ONLY "public"."lineitem" x WHERE ...`), que
diferem entre si só no nome da tabela.

## A heurística de candidatos

`candidatos.py` transforma `plan_node` em índices **concretos**, cada um com o
número que o justifica. Quatro famílias:

| família | o que procura |
| --- | --- |
| `seq_scan_filtro` | Seq Scan que descarta ≥ 1000 linhas por um filtro estável — o caso clássico, e o que `ix_plan_node_candidato_indice` existe para achar |
| `sort_em_disco` | Sort que derramou: a chave de ordenação é candidata |
| `hash_cond` | junção por hash sobre coluna sem índice |
| `index_filtro` | Index Scan que ainda descarta linha por filtro: candidato a coluna extra ou `INCLUDE` |

Dois cuidados que o corpus obrigou:

**As colunas vêm do catálogo, não do texto.** Regex sozinha em
`((l_shipdate >= '1994-01-01'::date) AND ...)` devolve `date` e `l_shipdate` no
mesmo saco. Cada identificador é cruzado com as colunas reais da relação, lidas
de `pg_attribute`. Quando o nó não carrega `relation_name` (Hash Join, Sort
acima de junção), a relação sai da coluna — e só quando ela é inequívoca.

**O candidato que já existe é descartado.** Medido: sem esse corte, os seis
candidatos do topo de quase todo cluster são os oito índices de FK de
`initdb/03-baseline-indexes.sql` sendo redescobertos pelos `Hash Cond` das
junções do TPC-H. Cobertura é por prefixo: um índice em `(a, b)` já atende uma
busca por `(a)`, o contrário não.

Depois do corte, o que sobra é o que de fato falta — `lineitem(l_shipdate)`,
`orders(o_orderdate)`, `part(p_brand, p_container)`,
`lineitem(l_shipmode, l_commitdate, l_receiptdate)`.

## O portão do DDL

A bancada executa DDL que veio do modelo contra o banco observado. Antes disso,
`validar_ddl()` reprova o que não for **uma única instrução `CREATE INDEX`**:
sem ponto e vírgula, sem comentário (`--` e `/*` são o jeito mais fácil de
esconder texto de uma regex), sem `CONCURRENTLY`. O nome é forçado ao prefixo
`brokk_exp_`.

Não é desconfiança do modelo — é que um `DROP TABLE` que chegue ali por
qualquer caminho custa o dataset inteiro, e o custo de conferir é uma regex. Um
DDL reprovado vira cluster sem experimento, e isso fica registrado.

## A bancada

```
checagens -> ANALYZE -> baseline -> CREATE INDEX -> ANALYZE -> replay -> DROP
```

**Não contaminar o próprio corpus.** O banco observado sobe com
`log_min_duration_statement=0` e `auto_explain.log_min_duration=0`. Cada replay
viraria registro de log e, na passada seguinte do coletor, entraria no corpus
como se fosse carga — o mesmo problema que `initdb-monitoring/01-schema.sql`
descreve para o banco de monitoramento. A sessão desliga os dois e se marca com
`application_name = 'brokk_bancada'`, que é a rede de segurança: se algo
escapar, dá para achar e descartar. Desligar o `auto_explain` tem um segundo
efeito, bem-vindo: sem `log_timing` por cima do nosso próprio `ANALYZE`, o
tempo medido é o da query e não o da instrumentação dobrada.

**Não medir durante carga.** `checar_ociosidade()` recusa rodar se houver
client backend ativo que não seja a bancada. Medir sob carga mede a carga.
`--forcar` ignora.

**Os parâmetros.** O `PREPARE` declara os tipos a partir do corpo da query, e
os valores entram no `EXECUTE` como literais de `psycopg.sql.Literal`. Passar
os valores como parâmetros do `EXECUTE` pelo protocolo estendido **não
funciona** — medido: as 27 queries falham com `could not determine data type of
parameter $1`, porque o Postgres não infere tipo em posição de argumento de
EXECUTE. E `plan_cache_mode = force_custom_plan`, senão a partir da sexta
execução o plano genérico entra e a medição muda de assunto.

**A primeira repetição é descartada** — ela paga o cache frio. Com 3
repetições sobram 2, e é por isso que `experimento_resultado` usa mediana e não
média.

**Timeout é piso, não erro.** Sem índice em `lineitem(l_partkey)`, a Q17 e a
Q20 levam horas e estouram qualquer `--timeout` razoável. Gravadas como erro,
elas sumiriam do resultado justamente no cluster em que o índice mais pesa.
Quem estoura é gravado com `tempo_ms` igual ao limite e `censurado = true`, e a
fase para de repetir ali. Em `experimento_resultado`:

| caso | `ganho_ms` |
| --- | --- |
| `antes_censurado` | piso: o ganho real é maior |
| `depois_censurado` | nulo: não se sabe quanto o índice ajudou |

`experimento_veredito` conta as duas situações em `antes_censuradas` e
`depois_censuradas`, e o `relatar` avisa quando os ganhos são "pelo menos".

**`DROP` em `try/finally`.** Um Ctrl+C no meio não pode deixar um índice de
300 MB para trás. O prefixo `brokk_exp_` e o subcomando `limpar` são a segunda
linha de defesa.

### A coluna que separa ganho de ruído

`experimento_medida.usou_indice` é `true` quando o plano do replay cita o
índice novo. Sem ela, qualquer variação de cache entre as duas fases viraria
"ganho". Uma query que ficou mais rápida e cujo plano não menciona o índice não
melhorou por causa dele.

## A matriz índice × query

`medir` só olha para dentro do cluster, e por isso não vê quem o índice
ajudaria fora dele. Na rodada 2, `lineitem(l_partkey)` ajudou Q17, Q19, Q8 e
Q9, e a Q20 depende da mesma coluna, mas as cinco estavam em três clusters, e
nenhum veredito mostrou isso.

`matriz` mede cada índice distinto proposto pelas rodadas pedidas contra o
corpus inteiro, numa sessão só:

1. **Linha de base única.** Cada query sem índice, uma vez. Todos os índices
   são comparados com ela, e a Q17 e a Q20 estouram o timeout uma vez, não uma
   vez por índice.
2. **EXPLAIN antes de medir.** Com o índice criado, um EXPLAIN sem ANALYZE
   diz se o plano o usa. Só quem usa é medido. Quem não usa tem ganho 0 em
   `matriz_resultado`: medir essa query só mediria cache.
3. **Um índice, uma medição.** O mesmo DDL proposto por dois métodos com nomes
   diferentes é medido uma vez.

`comparar` avalia qualquer rodada sobre a mesma matriz, sem voltar à bancada.
Para cada cluster e o índice que ele propôs:

| métrica | pergunta |
| --- | --- |
| cobertura | das queries que o índice beneficia no corpus, quantas estão no cluster? |
| precisão | das queries do cluster, quantas o índice beneficia? |
| média geral | ganho médio do cluster (0 para quem não usa o índice) |

"Beneficia" é usar o índice e cair pelo menos `--limiar` (padrão 10%). Como
todos os métodos são avaliados sobre as mesmas medidas, a diferença entre eles
é do agrupamento, não de duas corridas de bancada.

```bash
./.venv/bin/python -m src.index_provider matriz --rodadas 2,11,12
./.venv/bin/python -m src.index_provider comparar --rodadas 2,11,12 --json comparacao.json
```

## As tabelas

Em `src/index_provider/esquema.sql`, aplicado pelo próprio pacote em toda
execução, todo em `IF NOT EXISTS`. Não dá para pôr só em `initdb-monitoring/`:
aquilo roda uma vez, em banco vazio, e recriar o volume `monitordata` custaria
os planos já coletados. Uma cópia vive em
`initdb-monitoring/07-index-lab.sql` para volumes novos.

| tabela | papel |
| --- | --- |
| `query_embedding` | o vetor, chaveado por `(query_ref, modelo, normalizacao)` |
| `rodada_cluster` | uma execução de `agrupar`, com os parâmetros |
| `query_cluster` | em que cluster cada query caiu, com `x`/`y` para o gráfico |
| `experimento` | o índice candidato de um cluster, e os que foram recusados |
| `experimento_medida` | uma linha por (query, fase, repetição) |
| `experimento_resultado` | view: ganho por query, mediana das repetições |
| `experimento_veredito` | view: um índice por cluster, quantas queries ele ajudou |
| `matriz_sessao` | uma execução de `matriz`, com as rodadas de onde vieram os índices |
| `matriz_indice` | cada índice distinto da sessão, chaveado pelo DDL sem o nome |
| `matriz_medida` | linha de base (`indice_id` nulo), uso do índice (`repeticao = 0`) e medidas |
| `matriz_resultado` | view: cada índice contra cada query, com o ganho |

## Uso

```bash
cd ~/Sindri                  # de dentro da WSL

# modo clássico
export ANTHROPIC_API_KEY=sk-ant-...
./.venv/bin/python -m src.index_provider dossie --banco tpch
./.venv/bin/python -m src.index_provider recomendar --csv rec.csv
./.venv/bin/python -m src.index_provider recomendar --planilha --compartilhar voce@gmail.com

# modo de clusters
./.venv/bin/python -m src.index_provider vetorizar
./.venv/bin/python -m src.index_provider agrupar
./.venv/bin/python -m src.index_provider agrupar --listar
./.venv/bin/python -m src.index_provider exportar --rodada 1 --csv clusters.csv
./.venv/bin/python -m src.index_provider candidatar --rodada 1 --dossie   # sem gastar API
./.venv/bin/python -m src.index_provider candidatar --rodada 1
./.venv/bin/python -m src.index_provider medir --rodada 1 --cluster 3
./.venv/bin/python -m src.index_provider relatar --rodada 1 --csv clusters.csv

# se algo ficou para trás
./.venv/bin/python -m src.index_provider limpar
```

## Dependências

`psycopg[binary]` para tudo. `anthropic` para `recomendar` e `candidatar`,
`gspread` + `google-auth` para `--planilha`, `sentence-transformers` +
`umap-learn` + `scikit-learn` para `vetorizar` e `agrupar`. Todas importadas
dentro da função que as usa, não no topo do módulo: `dossie` e `relatar` rodam
sem nada disso instalado.

O `torch` tem que ser instalado **antes** do `requirements.txt`, do índice CPU
do PyTorch — senão o `sentence-transformers` puxa a build com CUDA (~2,5 GB)
numa máquina sem GPU:

```bash
./.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
./.venv/bin/pip install -r requirements.txt
```

O modelo de embedding baixa ~1,2 GB do HuggingFace na primeira execução e fica
em `~/.cache/huggingface`. Medido nesta bancada: 27 queries viram 27 vetores de
1024 dimensões em 170s numa CPU sem GPU.

## Ao editar

- **`%` em SQL com parâmetro vira placeholder do psycopg.** `SQL_EVIDENCIA` usa
  `position('Disk' in sort_method)` em vez de `ILIKE '%Disk%'` por isso, e
  `node_type LIKE 'Index%%'` está escapado. O comentário dentro de
  `SQL_EXPRESSOES` avisa a mesma coisa.
- **Não remova o `try/finally` de `Bancada.rodar()`.** Ele é o desenho inteiro:
  o índice sai, tenha o replay terminado ou não.
- **Nada de `fallbacks` de recusa nas chamadas.** O experimento grava qual
  modelo respondeu; um fallback silencioso misturaria duas procedências na
  mesma tabela de resultados. Uma recusa tem que virar erro visível.
- O DDL novo vai em `esquema.sql`, e a cópia em `initdb-monitoring/` precisa
  acompanhar.
