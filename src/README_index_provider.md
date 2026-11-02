# `index_provider/` — o recomendador de índices

Etapa 3 do pipeline. Pega o que o tabelão já sabe, junta com o catálogo real
do banco observado e propõe mudanças no conjunto de índices.

São **dois modos**, e a diferença entre eles é o que sai no fim.

| modo | subcomandos | saída |
| --- | --- | --- |
| clássico | `dossie`, `recomendar` | uma lista de recomendações, com espaço e ganho **estimados** |
| clusters | `vetorizar` → `agrupar` → `candidatar` → `medir` → `relatar` | o ganho **medido** de um índice real, query por query |
| otimização | `matriz --globais` → `otimizar` → `validar` | o **conjunto** de índices da carga inteira, previsto por um programa inteiro e depois medido |
| adaptativo | `matriz --globais` → `adaptar` (→ `--executar`, `--confirmar`) | as configurações **completas medidas** que valem o espaço, com o benefício marginal, a incerteza e o motivo de parar |

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
| `trigrama` | só com `matriz --trigramas`: `LIKE`/`ILIKE` com curinga no começo, ou regex (`~`, `~*`), sobre coluna de texto. Gera `USING gin (coluna gin_trgm_ops)` |

Sobre a família `trigrama`:

- **Ficam de fora** `NOT LIKE`, o `LIKE` ancorado (`'forest%'`, que um B-tree
  com `text_pattern_ops` resolve), padrão sem nenhum trecho de três
  caracteres sem curinga (o `pg_trgm` não extrai trigrama completo dele) e
  coluna dentro de função (`lower(field9) ~~ ...`).
- **A evidência é somada por (tabela, coluna).** A y19 do YCSB sorteia o
  padrão a cada execução, e cada padrão aparece no plano como um filtro
  diferente (`field9 ~~ '%4Ss%'`, `'%1Ff%'`...). Sem somar, a evidência se
  espalharia em dezenas de grupos pequenos e cairia do ranking.
- **A extensão é pré-requisito, e a bancada não a cria:** o portão do DDL só
  deixa passar `CREATE INDEX`. Sem `CREATE EXTENSION pg_trgm` no banco
  observado, `--trigramas` para com a instrução.
- **Sem o flag, nada muda:** a lista do TPC-H saiu idêntica (26 candidatos,
  mesma ordem). `matriz_sessao.trigramas` grava se a sessão ofereceu a
  família.

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

## Modo de otimização: seleção sem agrupamento

O modo de clusters decide um índice por cluster, e cada decisão é local. A
matriz mostrou o preço: `lineitem(l_partkey)` servia a cinco queries de três
clusters, e `orders(o_custkey)` entrou pela Q22 (+59%) levando junto a Q5
(−74%) e a Q8 (−90%). Este modo abandona a partição e escolhe o conjunto
inteiro de uma vez, para a carga inteira:

```
max   Σ_q w_q · Δ(q, S)  −  λ · MB(S)  −  μ · Escritas(S)
S ⊆ C
sujeito a  MB(S) ≤ orçamento
```

| símbolo | de onde vem |
| --- | --- |
| C | os índices de uma sessão da matriz: `--globais N` (heurística sobre o corpus inteiro, sem cluster), `--rodadas` (o que os clusters propuseram), ou os dois |
| w_q | execuções da query no corpus (`--peso execucoes`) ou 1 (`--peso uniforme`) |
| Δ(q, i) | ms poupados por execução, **medidos** pela matriz contra a linha de base única |
| MB | tamanho real do índice, medido no `CREATE INDEX` da matriz |
| λ | `--lambda`, ms de benefício que um MB precisa pagar (padrão 0) |
| μ | `--custo-escrita`, ms de manutenção por escrita na tabela do índice (padrão 0: não medido). As escritas saem de `query_resumo`, pela tabela-alvo do `INSERT`/`UPDATE`/`DELETE` |

```
monitoring.plan_node, corpus inteiro
        │
        ▼
1. matriz --globais N   heurística propõe, cada índice medido contra o corpus -> matriz_*
        ▼
2. otimizar             programa inteiro sobre a matriz                         -> otimizacao
        ▼
3. validar              o conjunto escolhido criado inteiro, carga remedida     -> otimizacao_medida
```

### De Δ(q, i) para Δ(q, S): duas hipóteses

A matriz mede um índice por vez; a função pede o efeito de um conjunto. O que
liga um ao outro é uma hipótese, e `--modelo` escolhe qual:

| modelo | Δ(q, S) | supõe |
| --- | --- | --- |
| `conservador` (padrão) | o melhor ganho entre os índices de S, **menos todas** as regressões que índices de S causam em q | que o planner pode usar justamente o índice que piora |
| `otimista` | o Δ do melhor índice de S que q usa | que o planner escolhe bem; regressão só conta se todo índice de S que q usa piora |

Nenhuma das duas é o que acontece, e é para isso que existe `validar`: ele
cria o conjunto escolhido inteiro, remede a carga e põe o Δ(q, S) medido ao
lado do previsto. Quanto o modelo erra, e qual das duas hipóteses erra menos,
é parte do resultado.

### Por que é exato

Com Δ(q, S) assim, o problema é um programa linear inteiro do tipo localização
de facilidades: `x_i` diz se o índice entra, `y_qi` qual índice atende a query.

```
y_qi ≤ x_i             só atende quem entrou
Σ_i y_qi ≤ 1           cada query fica com um índice, o melhor
Σ_i y_qi ≥ x_j         (otimista) se q usa j e j entrou, q é atendida por
                       algum índice do conjunto, mesmo que todos piorem
Σ_i MB_i · x_i ≤ orçamento
```

No conservador, só os ganhos viram `y`; cada regressão vira custo fixo do
índice que a causa. O `scipy.optimize.milp` (HiGHS) resolve dezenas de
índices × dezenas de queries em milissegundos, e o resultado é o ótimo do
modelo, não de uma busca gulosa. Conferido contra força bruta sobre os 2⁶
subconjuntos da matriz da sessão 1 do TPC-H: 76 casos (os dois modelos,
orçamentos, λ e pesos sorteados), nenhuma divergência.

A explosão combinatória da formulação geral some porque Δ(q, S) deixa de ser
medido na bancada a cada combinação: a bancada mede |C| índices, uma vez cada,
e a validação mede um conjunto. O preço é a hipótese de interação, e a
validação existe para cobrá-lo.

Três regras herdadas da matriz:

- **Ruído não é ganho.** |Δ| abaixo de `--limiar` (10%, o mesmo do
  `comparar`) vale 0.
- **Quem não usa o índice tem Δ = 0**, não a diferença entre duas medidas.
- **Pisos atravessam.** Com o "antes" estourando o timeout, o ganho previsto
  é piso; a saída marca as queries assim.

### Na mesma régua que os clusters

`otimizar --rodadas N` avalia o conjunto que a rodada N propôs (um índice por
cluster) no mesmo modelo e grava como otimização, com `parametros.rodada = N`.
Assim `validar` mede o conjunto dos clusters exatamente como mede o do solver:
criado inteiro, contra a mesma carga.

```bash
./.venv/bin/python -m src.index_provider matriz --globais 26 --rodadas 11
./.venv/bin/python -m src.index_provider otimizar --rodadas 11
./.venv/bin/python -m src.index_provider otimizar --modelo otimista --orcamento-mb 100,250,500
./.venv/bin/python -m src.index_provider otimizar --listar
./.venv/bin/python -m src.index_provider validar --otimizacao 1
./.venv/bin/python -m src.index_provider validar --otimizacao 1 --relatorio --json val1.json
```

Na validação, os índices do conjunto coexistem; o DDL é renomeado para
`brokk_exp_otim<N>_<k>` (`candidatos.renomear`, que passa de novo pelo
portão). A linha de base é medida de novo, na mesma sessão do "depois". Quem
não usa nenhum índice do conjunto não é remedido. O relatório traz, por
query, o previsto, o medido e quais índices o plano usou de fato contra o que
o modelo supôs; no fim, Σ peso × Δ previsto e medido, quantas queries caíram
na mesma classe (melhora / neutra / piora) e em quantas o índice que atende
foi o previsto.

## Modo adaptativo: até onde vale continuar

`otimizar` responde "qual o melhor conjunto que cabe em X MB?", e o X é
escolhido antes. `adaptar` responde outra pergunta: **até onde vale a pena
continuar adicionando, removendo ou trocando índices?** A resposta não vem do
modelo da matriz, que mede um índice por vez, e sim de configurações
**completas medidas**, comparadas com a incerteza que as sessões de medida de
fato têm. A exploração para quando o benefício adicional, mesmo no melhor
caso do intervalo, não paga mais.

```
matriz (índices um a um)          ──►  PROPOR    o que medir: fronteira prevista,
                                                 vizinhança (adição, remoção,
                                                 substituição, combinação)
validar (conjunto inteiro,        ──►  INFERIR   ruído com sessão e repetição
 várias sessões independentes)                   separadas, censura, bootstrap
                                       DECIDIR   superioridade para crescer, não
                                                 inferioridade para encolher,
                                                 nova sessão se inconclusivo, parada
                                       RELATAR   Pareto, intervalos, regressões,
                                                 limitações, confirmação à parte
```

Nenhum resultado de índice individual é usado como estimativa de uma
configuração. A matriz tem dois papéis e só esses: **priorizar** o que medir e,
como cada índice sozinho é ele mesmo uma configuração completa, ser
**evidência sobre a configuração {i}**.

### Por que repetir dentro da sessão não basta

Antes de qualquer fórmula, a medida. Nas 8 linhas de base do TPC-H com PGTune
(a da matriz e as 7 validações), a mediana de cada bloco varia entre sessões,
em escala log, **2,2× mais** do que as repetições variam dentro da sessão; no
YCSB, 1,8×. Na Q9, 8,8×. Um intervalo feito só com as repetições seria
otimista nessa proporção, e é por isso que a mesma configuração de 44 MB deu
−203 ms e +533 ms em duas validações.

### O contrato dos dados

`esquema.sql`, seção 9, garante e `evidencia.py` entrega para `estatistica.py`:

| termo | o que é |
| --- | --- |
| sessão de medida | uma corrida da bancada: a linha de base da matriz (`m<s>`) ou uma validação (`v<otimização>.<k>`) |
| configuração | o conjunto de `matriz_indice.id` criados além dos existentes. Duas otimizações com o mesmo conjunto são a mesma configuração (as 4 e 8 do PGTune viram uma, com duas sessões) |
| bloco | uma fase de uma sessão: (sessão, configuração, fase). Nunca é medida de outra configuração |
| repetição | uma execução no bloco; a 1 é aquecimento. **Não** é sessão independente |
| censura | `censurado = true`: o tempo real é ≥ `tempo_ms`, nunca igual |
| finalidade | `selecao` (decide), `confirmacao` (mede à parte o que foi decidido), `legado` (validação anterior a este registro, sem finalidade declarada) |
| ambiente | impressão digital da sessão, bruta e com hash do que precisa ser igual |

`validar` agora acumula validações (`--nova-validacao`) em vez de apagar a
anterior (`--refazer` continua existindo e apagando), grava a finalidade, a
ordem dos blocos e o ambiente, e `otimizacao_resultado` tem uma linha por
validação. As validações feitas antes da mudança viram a validação 1, com
finalidade `legado`, origem `migracao` e a limitação escrita: finalidade e
ambiente não foram registrados.

### O modelo de ruído

```
log T[q,b,r] = μ[q,c] + γ[s(b)] + α[q,b] + ε[q,b,r]
```

| termo | o que é | como é estimado |
| --- | --- | --- |
| μ[q,c] | o desempenho da query q na configuração c | média dos centros dos blocos (mediana do log das repetições úteis) |
| γ[s] | deslocamento comum da sessão inteira | median polish sobre as linhas de base, com ≥ 8 queries por bloco; centrado na sessão típica |
| α[q,b] | o que a sessão acrescenta para esta query | variância entre blocos da mesma (query, configuração), descontada a parte das repetições |
| ε[q,b,r] | entre repetições do mesmo bloco | MAD em torno da mediana do bloco |

A variância de um centro de bloco é σ²_α + κ·σ²_ε/r: mais repetições só
encolhem a segunda parcela. Três cuidados que os dados pediram:

- **Poucas sessões, muitas queries.** Com 2 a 8 blocos por query, cada
  variância sozinha é mal estimada. Ela é moderada por Bayes empírico (Smyth,
  2004, o `limma`): cada query fica entre a sua e a típica da carga, pesadas
  pelos graus de liberdade, e o peso da carga (d0) é estimado dos próprios
  dados. No TPC-H, d0 = 1,2: as queries diferem muito e cada uma fica com
  quase só a sua.
- **γ e o pareamento dentro da sessão.** Nas linhas de base todas as queries
  rodam e γ é estimável. Nos blocos de configuração só as afetadas rodam. A
  pergunta "o deslocamento da sessão chega ao bloco de configuração ao lado?"
  foi respondida com os dados, e a resposta é "pela metade": regredindo o
  desvio de cada bloco de configuração contra o γ da linha de base da mesma
  validação, a inclinação é **β = 0,57** (115 pontos). Não é detalhe: no
  mesmo dia e com as mesmas GUCs, as 7 validações das 18h14 às 19h29 (UTC)
  rodaram a linha de base entre −6% e +1% da sessão típica, e as 4 das 20h45
  às 21h32, entre +13% e +18% (a matriz, das 17h07 às 18h, +13%). Sem
  pareamento, as configurações medidas nas sessões lentas apareciam com três
  "regressões" (Q5, Q10, Q21) que eram a hora, não o índice. Agora o bloco de configuração de uma validação é ajustado por
  β̂·γ, β é estimado com erro padrão e sorteado no bootstrap. Os blocos de
  índice da matriz não são ajustados (a linha de base dela é a primeira coisa
  medida, horas antes deles) e recebem γ como efeito aleatório comum às
  queries da sessão, com a variância medida nas linhas de base.
- **Plano mais instável que a query.** O modelo supõe a mesma variância da
  query em toda configuração. A Q2 deu −31% e +3% nas duas sessões do
  conjunto de 44 MB, o que o τ da linha de base dela não explica. Cada grupo
  com ≥ 2 blocos usa o maior entre o τ da query e o seu próprio desvio, e o
  relatório nomeia as queries em que as sessões discordam (no TPC-H: Q2, Q7,
  Q8, Q16 e Q19, exatamente as "cinco regressões que sumiram na segunda
  validação" que este README registrou à mão).

**Censura.** Bloco com timeout diz T ≥ limite. A média do grupo sai da
verossimilhança censurada à direita (Tobit em log): blocos medidos pela
densidade, censurados pela cauda. Com todos censurados, não há limite
superior identificável, e toda comparação que dependa dele fica com o
intervalo **aberto** daquele lado. Q17 e Q20 sem índice são assim: o
benefício de tirá-las do timeout é "≥", e não pode ser rejeitado por ganho
insuficiente; entre duas configurações que as terminam, a linha de base
censurada cancela e o intervalo volta a ser fechado.

**Bootstrap paramétrico.** Sorteia-se, por query, uma variância da posterior
moderada (que carrega a incerteza de estimá-la com poucos blocos) e, com ela,
a média de cada grupo; por sessão, o γ comum. A mesma amostra de uma query
serve a todas as configurações, então a covariância entre B(S₁) e B(S₂) vem
de graça, e uma query que não usa índice de nenhuma das duas cancela
exatamente. Semeado por (query, configuração): mesma evidência, mesmos
números, em qualquer ordem. Cobertura medida em dados sintéticos (300
experimentos por regime): 0,92 a 0,95 para o intervalo de 90%.

### Benefício, benefício marginal e eficiência

```
B(S)  = Σ_q w_q (T[q,∅] − T[q,S])          ms sobre a janela do corpus (w = execuções)
ΔB    = B(S_novo) − B(S_atual) = Σ_q w_q (T[q,atual] − T[q,novo])
ΔS    = MB(S_novo) − MB(S_atual)
EM    = ΔB / ΔS                             informativa; não decide nada sozinha
```

T é a mediana (geométrica) do tempo por execução; o relatório mostra o ganho
por execução e o ponderado lado a lado. Query que não usa índice de nenhuma
das duas configurações contribui exatamente 0. Query que usou o índice numa
sessão e não noutra é uma mistura (com frequência p roda no plano novo) e
fica marcada como plano instável.

### A margem de relevância

"Ganho relevante" precisa de um número, e ele é declarado **antes** da
comparação: depende dos parâmetros e da configuração de referência, nunca do
resultado da própria comparação.

```
margem(A) = max( ρ · C(A),  σ_C(A) )        (ou --margem-s, fixa)
C(A)      = Σ_q w_q T[q,A]                  o que a carga de leitura gasta hoje
σ_C(A)    = √Σ_q (w_q T[q,A] τ_q)²          quanto C(A) varia entre sessões sem mudar nada
```

- **ρ = 1%** (`--relevancia`): um ganho menor que 1% do que a carga gasta na
  configuração atual não justifica mudar a configuração. É relativo à carga
  atual, e não à linha de base: no TPC-H com PGTune, 1% da linha de base é
  75,6 s (Q17 e Q20 entram com o piso de 300 s cada), e depois de tirá-las do
  timeout essa margem seria inalcançável para qualquer melhoria seguinte.
- **σ_C**: um ganho menor que a flutuação natural da carga entre sessões não
  se distingue, na operação, de um dia bom. Termos que são só piso ficam
  fora (a variação de um tempo que não foi medido não é conhecida).
- **Espaço e manutenção** entram como custo do movimento, na unidade do
  benefício: `λ` ms por MB (`--lambda`, padrão 0) e o custo de manutenção
  **só** se medido (`custo_manutencao`) ou declarado como hipótese
  (`--custo-entrada`). Com λ = 0 o espaço é lexicográfico: a configuração
  maior só substitui a menor se provar ganho relevante.

### As decisões, cada uma com o seu teste

| pergunta | teste | regra (intervalo bilateral de 90%) | usado em |
| --- | --- | --- | --- |
| b é melhor que a por mais que a margem? | superioridade | aceita se li > m; **ganho insuficiente** se ls < m; senão inconclusiva | crescer (ΔS ≥ 0) |
| b perde no máximo a margem? | não inferioridade | aceita se li > −m; perda relevante se ls < −m; senão inconclusiva | remover, trocar por menos espaço |
| a diferença está dentro de ±m? | equivalência (TOST) | −m < li e ls < m | só no relatório |
| a query mudou? | diferença, bilateral | melhora se li > 0, piora se ls < 0 | regressões por query |

Inconclusiva não é rejeição nem empate: pede nova sessão de quem tem menos,
até `--max-validacoes` (3); esgotado, o par fica **não ordenável** e a
configuração atual se mantém — o ônus da prova é de quem pede mais espaço.
Sem significância nunca vira "equivalente": equivalência exige o intervalo
inteiro dentro da margem.

**Aceitar pede duas sessões.** Uma configuração só é aceita com pelo menos
duas sessões independentes, uma delas validação do conjunto inteiro
(`--min-sessoes` no código, 2). Uma sessão só não separa efeito de um plano
que o planner escolheu só naquela sessão — a matriz e as validações já
discordaram assim na y15 do YCSB e na Q3 do TPC-H. Rejeitar com uma sessão é
o lado conservador e continua permitido.

### A caminhada

Parte da linha de base. A cada passo, as candidatas vêm de cinco fontes e são
examinadas nesta ordem de prioridade:

| ordem | grupo | o que entra |
| --- | --- | --- |
| 1 | correção | remoção com motivo **medido**: índice no plano de uma query que piorou, ou índice que nenhuma query usou nas validações |
| 2 | parcimônia | remoção ou troca por menos espaço com perda prevista menor que a margem, se economizar ≥ 10% do espaço atual (`--economia-minima`: 11 min de bancada para economizar 1 MB de 44 não se pagam) |
| 3 | crescimento | ponto da fronteira prevista, adição de um índice, substituição (mesma tabela, query em comum: o simples × o composto) e **configurações já medidas** por `otimizar` ou pelas rodadas de clusters. As que já têm sessões para decidir vêm primeiro (a evidência já está paga); depois, a maior eficiência prevista |
| 4 | exploração | até `--exploracao` (2) combinações de pares que a mesma query usa e que o modelo do máximo **não** favorece: o planner pode escolher outro plano quando os índices coexistem, e o modelo aditivo não vê isso |

A fronteira prevista (varredura de orçamento sobre o MILP de `otimizar`) só
ordena: um ponto fora dela entra pelas outras fontes.

**Parada**, registrada com o motivo:

1. `--paciencia` (2) crescimentos seguidos **sem ganho provado**: rejeitados
   (mesmo no limite superior do intervalo, o benefício adicional não passou da
   margem) ou não ordenáveis (as sessões se esgotaram sem decidir);
2. nenhuma candidata restante: o modelo não prevê ganho acima da margem e a
   exploração se esgotou;
3. a decisão pede uma medição: `adaptar` sem `--executar` para aí e diz qual
   (estado `aguardando_medicao`); com `--executar`, mede e recomeça, até
   `--max-medicoes` (6) por execução.

As paradas 1 e 2 só valem depois da **resolução de incerteza** (abaixo), que
pode mudar a atual ou pedir mais uma medição.

O não ordenável conta para a paciência desde as adaptações 13 e 14 do TPC-H
com PGTune. Antes, só a rejeição contava, e a caminhada passou por quatro
variantes do mesmo crescimento: a atual (248,4 MB) mais {i1,i18,i19}
(+357 MB), cada uma com um índice minúsculo diferente (i25, i23, i7 + i25,
i31). Cada variante é uma configuração nova da fronteira prevista, com
sessões próprias. Foram 10 sessões nessa região, e todas deram o mesmo
quadro: ganho provável de 2 a 5 s, sem provar que passa da margem de 4,8 s.
Só o limite de medições por execução parava a busca.

A caminhada é função pura da evidência: a cada medição nova ela recomeça da
linha de base e refaz o caminho. Decisões antigas podem mudar com a evidência
nova, e é para isso que existem; o histórico fica em `adaptacao_passo`.

### Resolução de incerteza antes de parar

"Não ordenável" não quer dizer "dispensável". No TPC-H com PGTune, o conjunto
de 292,8 MB de `otimizar` ficou a ΔB +1,8 s [−3,7; +8,3] da recomendação
(248,4 MB) com **uma** sessão. A previsão da matriz não o favorecia, ele não
entrava em nenhum grupo da caminhada, e ela parou sem compará-lo. Medido de
novo na comparação de métodos, era o melhor: +475 ms por passada [+117; +858].

Antes de parar, a caminhada compara com a atual **toda configuração completa
já medida** (fora as do próprio caminho, já decididas entre si) e remede a que
cumpre as três condições:

1. já foi medida, e a comparação com a atual não decidiu;
2. a diferença de espaço (e de manutenção, quando medida) pode ser paga por
   um ganho relevante: crescer passa pela superioridade com o custo
   descontado; encolher só vale a validação se economizar `economia_minima`
   (10%) do espaço;
3. a incerteza ainda a deixa competitiva: **UCB(ΔB) − custo ≥ δ** (no
   encolhimento, ≥ −δ). Com UCB(ΔB) − custo < δ ela é descartada: nem o melhor
   caso do intervalo paga a margem.

"Não ordenável" (o intervalo contém o zero) e "inconclusiva" (o intervalo
cruza a margem) são coisas diferentes, e só a segunda é remedida. No mesmo
TPC-H: o baseline do HDBSCAN (358 MB) fica a −7,1 s [−14,6; +1,1] da atual.
Não é ordenável, mas UCB < δ, e é descartado sem gastar validação.

| ação | quando |
| --- | --- |
| aceitar | a comparação já decide, com sessões para aceitar (≥ 2, uma delas validação): a atual muda e a exploração continua dela |
| remedir | inconclusiva e competitiva: nova sessão de quem tem menos, até `--max-validacoes` (3) por configuração e `--resolucao` (3) no experimento |
| descartada | rejeitada pelo teste, ou compacta demais para pagar uma validação |
| sem orçamento | competitiva e sem decisão, mas os orçamentos acabaram: fica "não ordenável" e vai para o DBA |
| não resolve | o intervalo é aberto dos dois lados (as duas no timeout em alguma query): outra sessão não muda isso |

A remedição segue a mesma regra de eficiência do crescimento, com a
probabilidade de vencer no lugar do ganho previsto: **P(vencer) por MB
alterado**, com 1 MB de piso. P(vencer) vem das amostras do bootstrap e só
ordena. Pela preferência por menos espaço, resolver primeiro a alternativa
mais perto da atual pode dispensar as maiores (se ela for aceita, as outras
passam a ser comparadas com ela); o contrário não acontece. Com P(vencer)
sozinha, o conjunto de 795 MB (30%, +547 MB) passaria à frente do de 293 MB
(25%, +44 MB).

A comparação é sempre entre configurações completas. Cada uma é medida contra
a linha de base da sua própria sessão, e a diferença entre as duas carrega o
deslocamento de sessão pareado (β) e o resto como variância. Nunca são dois
intervalos lado a lado. Duas configurações nunca estão na mesma sessão (ver
os limites).

A validação pedida grava `movimento = 'resolucao'` em `otimizacao_validacao`.
É o que conta o orçamento, e o comando sugerido para medir à mão já traz
`--movimento resolucao`.

### Multiplicidade

Cada decisão é um teste unilateral de 5% (intervalo bilateral de 90%), sem
correção para o número de comparações. O procedimento é sequencial: a cada
validação, as mesmas comparações são refeitas com mais evidência. O relatório
(`multiplicidade` no JSON) documenta:

- quantas configurações foram medidas: conjuntos validados e índices só na
  matriz;
- quantas comparações foram feitas: na caminhada e na resolução de incerteza.
  A da resolução conta uma comparação por configuração medida fora do
  caminho, contra a atual, mesmo que a última análise tenha parado antes de
  chegar lá (pedindo medida). Sem isso, a família de Bonferroni dependia de
  onde a análise parou: no TPC-H, caiu de 49 para 6 comparações de uma
  execução para a outra, com mais evidência;
- quantos testes por query o relatório mostra, e quantas marcas +/− se
  esperariam por acaso se nada tivesse mudado (10% de cada);
- quantas sessões independentes há, quantas validações de seleção e de que
  movimento veio cada uma, e quantas sessões tem cada conjunto;
- a unidade independente: a sessão de medida. As repetições de um bloco não
  são independentes, e as queries de uma sessão compartilham o γ dela;
- como a seleção afeta os intervalos: eles são condicionais a cada
  comparação, e o ganho da recomendada estimado na seleção é otimista por
  construção;
- a **sensibilidade a Bonferroni**: cada passo aceito do caminho, refeito com
  a confiança corrigida pelo número total de comparações, sobrevive ou não. É
  sensibilidade, não regra: Bonferroni é conservador com comparações
  correlacionadas e mesmo assim não cobre as análises intermediárias. O
  limite é um quantil extremo, por isso sai de um amostrador à parte com 50
  amostras na cauda;
- a confirmação: pendente na seleção. Na confirmação, a **reprodução** mostra
  que fração do ganho estimado na seleção a medida independente encontrou, e
  se essa estimativa cabe no intervalo da confirmação.

### Custo de manutenção: o que se sabe e o que não

A bancada só reexecuta leitura. `manutencao.py` separa três coisas:

1. **Exposição estática** (o que o SQL permite deduzir, pelo pglast): INSERT
   mantém todo índice da tabela (**certa**); UPDATE que atribui coluna de que
   algum índice depende — chave, expressão, `INCLUDE`, predicado, inclusive
   dos índices que já existem — não pode ser HOT se o valor mudar
   (**provável**); UPDATE que não toca coluna indexada é elegível a HOT e só
   mantém os índices se não couber na página (**possível**: depende de
   fillfactor e espaço livre, não do SQL). DELETE não insere entrada; as
   mortas são removidas pelo VACUUM, uma passada por índice, e pela limpeza
   oportunista — esse custo existe e não é estimado aqui, é contado à parte.
2. **Comportamento observado**: `n_tup_hot_upd / n_tup_upd` da tabela,
   gravado no ambiente de cada sessão, situa a exposição "possível" entre os
   extremos. É observação, não previsão.
3. **Custo medido**: só o que vier de `monitoring.custo_manutencao`, gravado
   por um experimento A/B de escrita (a mesma carga com e sem o índice:
   latência, throughput, WAL, fração HOT). Sem isso, o relatório dá o **ponto
   de equilíbrio** — quanto cada entrada de índice pode custar antes de o
   movimento deixar de se pagar — e marca a configuração como "só a leitura
   avaliada". Nenhuma configuração é apresentada como ótima com escrita na
   carga e manutenção não medida.

### O ambiente

`ambiente.py` grava, por sessão de medida: versão e build do Postgres,
extensões e versões, GUCs efetivas da sessão (todas as fora do padrão) e o
subconjunto que muda plano e tempo, banco, tamanho das tabelas e os índices
que **já existem** (a linha de base é "sem os índices do experimento", e isso
só é a mesma coisa com os mesmos índices existentes), CPU, memória, sistema e
kernel da máquina da bancada, limites do container (`docker inspect`, quando
responde), quem mais estava conectado no começo e no fim, e o aquecimento
(repetições, descartada, ordem dos blocos). Valores brutos para auditar; um
hash do subconjunto comparável para conferir rápido. Sessão com ambiente
registrado diferente do de referência é **excluída** da análise, com a lista
do que mudou; sessão sem registro (anterior a ele) entra e é marcada.

A ordem dos blocos alterna entre as validações do mesmo conjunto (linha de
base primeiro, depois conjunto primeiro), para a deriva temporal da sessão não
cair sempre do mesmo lado. Alternar dentro da validação, query a query,
pediria recriar índices de centenas de MB a cada query.

### Seleção e confirmação

Escolher a melhor entre várias estimativas ruidosas superestima a escolhida
(a "maldição do otimizador" que a validação do TPC-H mostrou: 1.751 ms
previstos, 695 entregues). Por isso:

- a decisão lê só `selecao` e `legado`;
- `adaptar --confirmar N --executar` mede de novo, em sessões novas com
  finalidade `confirmacao`, o último passo aceito da adaptação N (a
  recomendada e a anterior a ela no caminho), e testa esse passo com a
  **margem e o teste declarados na seleção**. As médias vêm só da
  confirmação; as variâncias, de toda a evidência;
- confirmada: a recomendação se sustenta fora dos dados que a escolheram.
  Não confirmada: `adaptacao.confirmada = false`, a exploração se reabre, as
  sessões de confirmação passam a ser evidência de seleção (já foram usadas
  para decidir) e a próxima recomendação pede outra confirmação.

### Uso

```bash
# o modelo de ruído da sessão: σ_α, σ_ε, τ moderado e o corte da matriz por query
./.venv/bin/python -m src.index_provider adaptar --sessao 1 --ruido

# decide com o que já foi medido e diz a próxima medição (não toca na bancada)
./.venv/bin/python -m src.index_provider adaptar --sessao 1 --json adaptar.json

# mede o que a decisão pede, até parar ou até 6 validações
./.venv/bin/python -m src.index_provider adaptar --sessao 1 --executar --max-medicoes 6

# a confirmação independente da adaptação 7
./.venv/bin/python -m src.index_provider adaptar --sessao 1 --confirmar 7 --executar

./.venv/bin/python -m src.index_provider adaptar --listar
# a medição que `adaptar` pede, feita à mão (o comando sugerido já traz o movimento)
./.venv/bin/python -m src.index_provider validar --otimizacao 11 --nova-validacao --ordem conjunto_primeiro --movimento resolucao
./.venv/bin/python -m src.index_provider validar --otimizacao 11 --relatorio --validacao 1
```

No YCSB, como antes: `--banco ycsb`, `--consultas queries/ycsb` e
`BROKK_DSN_OBSERVADO` apontando o `ycsb`.

| parâmetro | padrão | o que é |
| --- | --- | --- |
| `--confianca` | 0,90 | nível do intervalo bilateral; cada limite é unilateral a 95% |
| `--relevancia` | 0,01 | ρ da margem |
| `--margem-s` | — | margem absoluta, no lugar de ρ, declarada por quem decide |
| `--lambda` | 0 | ms de benefício que um MB precisa pagar |
| `--custo-entrada` | — | hipótese de ms por entrada de índice (o relatório diz que é hipótese) |
| `--paciencia` | 2 | crescimentos seguidos sem ganho provado (rejeitados ou não ordenáveis) para parar |
| `--max-validacoes` | 3 | sessões de seleção por configuração antes de "não ordenável" |
| `--max-medicoes` | 6 | validações por execução de `--executar` |
| `--exploracao` | 2 | combinações sem ganho previsto testadas antes de parar |
| `--resolucao` | 3 | validações, no experimento, para remedir antes de parar alternativas já medidas inconclusivas e competitivas |
| `--modelo` | conservador | o modelo de interação que só **prioriza** candidatas |

Os padrões foram fixados antes de olhar os resultados que este README
apresenta, e não foram ajustados por eles. A exceção é declarada: a
resolução de incerteza foi criada **depois** da comparação de métodos, que
mostrou o conjunto de 293 MB à frente. A ordem de remedição (P(vencer) por
MB) foi escolhida depois de ver, na evidência de **seleção** do TPC-H, que
P(vencer) sozinha mediria antes o de 795 MB. Por isso as sessões daquela
comparação (confirmação da adaptação 8) não servem de confirmação para o que a
regra nova recomendar: isso pede sessões novas.

Duas outras exceções foram feitas depois de ver a evidência de **seleção** das
adaptações 13 e 14, e nunca a de confirmação. O não ordenável passou a contar
para a paciência, e a família de Bonferroni passou a contar as configurações
medidas mesmo quando a resolução ainda não foi feita (ver Parada e
Multiplicidade).

### O que sai para o DBA

Uma tabela de configurações medidas (espaço, benefício com intervalo, quantas
queries melhoram e pioram com evidência, sessões, situação), a recomendada
com o DDL, o caminho até ela com ΔB, ΔMB e eficiência de cada passo, as
limitações (piso, plano instável, sessões que discordam, índice sem uso, uma
sessão só, manutenção não medida com o ponto de equilíbrio) e, entre as
configurações da fronteira de Pareto, **o que a evidência permite afirmar**
de cada par: "b melhor", "a melhor", "equivalentes" (TOST) ou "não
ordenável" — nunca uma ordem forçada. A situação de cada configuração:

| situação | quando |
| --- | --- |
| recomendada | a configuração em que a caminhada parou |
| ganho relevante sobre a recomendada, fora do caminho | maior, e prova mais que a margem: a caminhada não a examinou (vale examinar) |
| mais espaço, ganho abaixo da margem; avaliar custo | maior, ganho com evidência mas pequeno |
| mais espaço sem ganho relevante | maior, e nem o limite superior passa da margem |
| alternativa compacta: não inferior | menor, e perde menos que a margem |
| alternativa compacta, com perda relevante | menor, para quem tem pouco espaço |
| dominada por X | X usa menos espaço e não perde mais que a margem para ela |
| não ordenável | a evidência não decide |

Sufixos: "avaliar regressões" (alguma query piora com evidência) e "só a
leitura avaliada" (há escrita nas tabelas e a manutenção não foi medida).

Depois da tabela vêm a **resolução de incerteza** e a **multiplicidade**. A
resolução lista cada alternativa medida contra a atual: ΔB com intervalo,
ΔMB, sessões, P(vencer), a ação e o motivo. Os índices sozinhos da matriz que
foram descartados só aparecem no JSON. A multiplicidade traz as contagens, a
unidade e a sensibilidade a Bonferroni (seção anterior).

### Testes

```bash
./.venv/bin/python -m unittest discover -s tests -t . -v
BROKK_TESTE_PG=postgresql://brokk:brokk_dev_pwd@localhost:5433/postgres \
    ./.venv/bin/python -m unittest tests.test_integracao_pg -v
```

| arquivo | o que cobre |
| --- | --- |
| `tests/test_estatistica.py` | benefício ponderado e por execução, marginal, separação σ_α/σ_ε, "repetição não vale sessão", γ, moderação, cobertura do intervalo, reprodutibilidade, censura (piso, cancelamento, Tobit, inidentificável), regressões, sessões que discordam, plano instável, os quatro testes de decisão, indistinguíveis, Pareto com tamanhos diferentes, medições insuficientes |
| `tests/test_manutencao.py` | parser das escritas, dependências do índice (chave, INCLUDE, expressão, GIN, predicado), classes de exposição, HOT observado, ponto de equilíbrio, custo parcial que não vira número |
| `tests/test_adaptativa.py` | a caminhada contra uma bancada sintética (`tests/simulado.py`): ganho grande em poucas queries com piso, ganhos pequenos espalhados (aceitos só com ruído baixo), leitura melhor com escrita pior, indistinguíveis, espaço sem benefício com parada por paciência, índice que ajuda sozinho e atrapalha em conjunto, medição insuficiente, remedição e limite de medições; a resolução de incerteza (a alternativa medida uma vez que a previsão não favorece é remedida e aceita, a sem ganho é descartada sem remedir, o orçamento esgotado deixa o par não ordenável, a compacta com pouca economia não é remedida), as variantes quase iguais (o não ordenável conta para a paciência e a caminhada não pede a quinta variante), a contagem de multiplicidade com Bonferroni (estável quando a análise para antes da resolução) e a reprodução na confirmação |
| `tests/test_integracao_pg.py` | num banco descartável: os scripts de `initdb-monitoring/`, migração idempotente com dados, a view por validação, o carregador de evidência, a persistência da adaptação |

### Limites conhecidos

- **A margem é uma convenção declarada**, não uma lei: ρ = 1% e a flutuação
  entre sessões são justificáveis, não únicos. Quem tem SLA usa `--margem-s`.
- **As decisões sequenciais não controlam o erro global.** Cada teste tem o
  seu nível; remedir até decidir é olhar os dados mais de uma vez. O
  relatório conta as comparações e mostra a sensibilidade a Bonferroni, sem
  corrigir as decisões. A confirmação independente é a defesa contra o
  otimismo que isso gera, e é ela que deve ser apresentada como resultado.
- **Duas configurações nunca estão na mesma sessão.** A diferença entre A e B
  passa pela linha de base de cada sessão. Com β ≈ 0,4 (TPC-H), a linha de
  base remove só parte do deslocamento da sessão, e o resto entra como
  variância das duas. Uma validação "duelo" (linha de base, A e B na mesma
  sessão) parearia direto. Ela pede outro protocolo na bancada e um termo de
  sessão compartilhado no modelo, e não existe ainda.
- **Poucas sessões por conjunto.** Com 2 sessões por conjunto, a média de
  cada um é imprecisa. A variância entre sessões não depende disso: ela é
  estimada em conjunto (todas as queries e configurações, moderada), com a
  hipótese de que é a mesma para toda configuração, fora a proteção do grupo
  instável.
- **O modelo de variância** supõe o mesmo τ por query em toda configuração,
  com a proteção do grupo instável; não modela correlação entre queries além
  do γ comum da sessão.
- **γ da matriz**: os blocos de índice da matriz não são ajustados pelo γ da
  linha de base dela; se a matriz inteira rodou deslocada, os índices sozinhos
  carregam esse deslocamento como ruído.
- **Um β para a carga inteira**: o pareamento é o mesmo para toda query e
  configuração; queries cujo bloco de configuração sente mais (ou menos) a
  sessão do que a média ficam com esse erro no ruído delas.
- **Deriva dentro da sessão** que não seja proporcional ao γ da linha de base
  não é modelada; a alternância da ordem dos blocos a distribui entre os dois
  lados, sem removê-la.
- **Mistura de planos** usa a fração observada p como fixa, sem a incerteza
  de estimá-la com 2 ou 3 sessões.
- **Custo de manutenção** não é medido pela bancada; a tabela
  `custo_manutencao` está pronta para o experimento A/B, que ainda não existe.
- **A exploração por combinação** é limitada a pares; sinergias de três ou
  mais índices só aparecem se a fronteira prevista as propuser.
- **Medir custa**: no TPC-H, ~11,5 min por validação, e aceitar pede duas
  sessões. Quando a matriz e as validações discordam muito, a caminhada mede
  bastante antes de parar (no cenário sintético adversarial, 15 validações
  para 3 índices).

## Resultados: baseline e método A

TPC-H SF1, 22 queries (uma por template), PostgreSQL 18, Qwen3-Embedding-0.6B,
UMAP (`n_neighbors=5`, `n_components=5`) e HDBSCAN (`min_samples=2`,
`min_cluster_size=3`), semente 42. Baseline é a rodada 2 (SQL inteiro) e o
método A a rodada 11 (`from-where`). Os dois foram avaliados sobre a mesma
matriz (`figuras/tpch-puro/comparacao_metodos/`, limiar de 10%), então a
diferença é do agrupamento. O método B ficou atrás nos dois critérios e não
entra aqui.

**Estabilidade** (21 sementes, ARI entre sementes; 1 = partição idêntica):

| método | ARI | clusters mais comuns |
| --- | --- | --- |
| baseline | 0,77 | 4 clusters (14 de 21 sementes) |
| A | 0,83 | 3 clusters (15 de 21 sementes) |

**Qualidade dos clusters** (matriz índice × query):

| | baseline | A |
| --- | --- | --- |
| clusters | 4 | 3 |
| cobertura média | 0,51 | 0,50 |
| precisão média | 0,35 | 0,33 |
| média geral de ganho | 18,6% | 29,2% |
| queries beneficiadas fora do cluster do índice | 9 | 4 |

**Ganho por query** (índice do cluster em que a query caiu; 0% = o plano não
usou o índice):

| query | baseline | A |
| --- | --- | --- |
| Q17 | 99,6% (piso) | 99,6% (piso) |
| Q20 | 99,97% (piso) | 99,97% (piso) |
| Q19 | 90,5% | 92,9% |
| Q22 | 59,4% | 0% |
| Q8 | 41,5% | 43,9% |
| Q9 | 12,8% | −3,2% |
| Q10 | 0% | 7,3% |
| Q4 | 0% | −5,7% |
| Q5 | −74,1% | −1,4% |
| demais (Q1, Q2, Q3, Q6, Q7, Q11, Q12, Q13, Q14, Q16, Q18, Q21) | 0% | 0% |

Leitura:

- **Os dois dependem do mesmo índice.** `lineitem(l_partkey)` (com variações de
  `INCLUDE`) responde por Q17, Q19, Q20 e Q8. A diferença está em quem acerta
  o cluster: o A é mais estável e deixa menos queries beneficiadas fora do
  cluster, mas perde a Q22, que só o baseline atende (`orders(o_custkey)`).
- **A média maior do A é efeito de cluster pequeno**, não de mais queries
  beneficiadas: 4 queries acima do limiar, contra 6 do baseline.
- **Regressões.** O baseline tem uma (Q5, −74%, pelo `orders(o_custkey)`). O A
  tem três, todas pequenas (Q4, Q5, Q9, até −6%).
- **Q17 e Q20 são piso.** Estouraram o timeout de 300 s sem índice; o ganho
  real é maior. Nenhum método as agrupou na mesma partição em 21 sementes.

Limites desta amostra:

- O corpus tem 22 queries, uma por template; um cluster de 3 já é 14% dele.
- Cada query tem 4 repetições por fase, descartada a primeira (3 úteis). Q17 e
  Q20 têm uma só medida "antes" (o timeout).
- A matriz é de uma sessão só. A rodada 2 também foi medida pelo `medir`, que
  dá valores um pouco diferentes (Q5 −22,8%, Q19 79,9%, Q22 53,8%), efeito de
  cache e carga entre execuções.
- A query de referência 26 não foi medida em nenhuma das duas rodadas.

## Resultados: YCSB, baseline e método A

Banco `ycsb` (usertable, 1M linhas, só a PK em `ycsb_key`), carga de 30 min
do `ycsb-bench.sh`: as 22 consultas de `templates_ycsb_mixed.xml` em paralelo
com os 10 comandos de escrita, mais uma corrida do YCSB original. Corpus de
24 queries de leitura (as 22 mistas e o ReadRecord/ScanRecord do YCSB; o
`SELECT ... FOR UPDATE` fica fora pelo corte de escrita). Mesmo modelo de
embedding, UMAP e HDBSCAN do TPC-H, semente 42. Baseline é a rodada 1, método
A a rodada 2. A bancada apontou para o `ycsb` com
`BROKK_DSN_OBSERVADO=postgresql://sindri:sindri_dev_pwd@localhost:5432/ycsb`.

Diferente do TPC-H, isto é **`medir`, não `matriz`**: cada método foi medido
só dentro dos próprios clusters, e as linhas de base não são as mesmas. A
comparação justa ainda não foi feita.

| | baseline | A |
| --- | --- | --- |
| clusters | 4 | 2 (um sem candidato) |
| experimentos | 4 | 1 |
| queries que usaram o índice e melhoraram | 5 (y06, y17, y15, y07, y08 +2%) | 3 (y15, y22, y07) |
| pioras atribuíveis ao índice | 3 (y14 −52%, y11 −46%, y22 −8%) | 0 |
| média geral (critério do `comparar`) | 9,9% | 8,1% |
| ganho atribuível somado | 1.411 ms | 412 ms |
| ganho fora da query de origem | −107 ms (cluster 2), −7 ms (cluster 3), 0 nos outros | +24 ms |

**Os índices** (mediana, primeira repetição descartada):

| índice | tamanho | efeito medido |
| --- | --- | --- |
| `(left(field2, 3))` | 7 MB | y06 738 → 0,0 ms |
| `(left(field7, 1))` | 7 MB | y17 373 → 24 ms |
| `(ycsb_key) INCLUDE (field1)` | 140 MB | y15 681 → 244 ms; y22 +8% |
| `(ycsb_key) INCLUDE (field1, field2)` (A) | 250 MB | y15 629 → 241 ms; y22 87 → 63 ms |
| `(ycsb_key) INCLUDE (field3..field6)` | 486 MB | y14 195 → 297 ms, y11 11 → 15 ms: piorou |

Leitura:

- **A hipótese não se sustentou aqui também.** Cada índice ajudou basicamente
  a query de onde saiu; o único ganho fora da origem foi a y22 no método A.
- **O baseline achou as duas maiores otimizações** (y06 e y17, índices de
  expressão de 7 MB); o A deixou as duas sem experimento, porque com uma
  tabela só o FROM/WHERE varia pouco e ele agrupou em 12 + 12.
- **O pior experimento foi escolha do modelo**: o índice de cobertura de 486 MB
  saiu de um `index_filtro` de 34 ms e piorou duas queries.
- **O que ficou sem índice:** a y19 (`LIKE '%abc%'`, ~318 ms) precisaria de
  GIN com `pg_trgm`, que a heurística não propõe (só gera B-tree), o prompt
  não autoriza e a extensão não está instalada; as agregações sobre a tabela
  inteira (y12, y13, y16, y18, y20, 0,5 a 1,5 s) leem tudo e nenhum índice
  ajuda.
- **Ruído:** as OLAP variaram até ~10% sem tocar o índice (y12 −8%, y16 +9%).

Limites desta amostra: 24 queries, 5 repetições por fase (4 úteis), sem
estabilidade entre sementes e sem `matriz`. O arquivo completo (dump do
`brokk`, logs, resultados do BenchBase, figuras) está em
`arquivo/2026-10-25_ycsb/`.

## Resultados: TPC-H, otimização × método A

Mesmo corpus e mesma configuração do experimento de 24/10: TPC-H SF1, 21
queries replicáveis (a Q15 fica fora pela view `revenue0`), Postgres **com os
padrões**, sem o PGTune (o servidor foi recriado com o `command` do HEAD só
para esta rodada e voltou ao PGTune depois). O trabalho rodou numa cópia do
`brokk_tpch_padrao`, o `brokk_tpch_otimizacao`, e o método A é a rodada 11 de
lá.

- **Matriz, sessão 2:** 29 índices (os 26 que a heurística propõe para o
  corpus inteiro e os 3 da rodada 11) × 21 queries, 5 repetições, timeout de
  300 s, em 49,5 min.
- **Validação:** cada conjunto criado inteiro e medido contra uma linha de
  base nova, ~12 min por conjunto.
- **Pesos:** as 21 queries têm 6 execuções e o corpus não tem escrita, então
  `--peso` e `--custo-escrita` não mudam nada aqui.

| conjunto | índices | MB | beneficiadas (≥10%) | regressões (≤−10%) | ganho fora de Q17/Q20, previsto → medido (ms por passada) |
| --- | --- | --- | --- | --- | --- |
| método A como conjunto (otimização 2) | 3 | 526,3 | 4 | 2: Q3 −61%, Q10 −15% | 702 → 520 |
| otimização, orçamento = A (6) | 8 | 497,3 | 7 | 3: Q2 −40%, Q21 −22%, Q5 −10% | 1.751 → 695 |
| otimização, orçamento 50 MB (3) | 3 | 44,7 | 8 | 0 | 1.058 → 1.552 |
| otimista, sem orçamento (8) | 11 | 962,2 | 9 | 4: Q3 −40%, Q2 −28%, Q21 −21%, Q12 −17% | 1.933 → 878 |

λ = 1 ms/MB chega ao mesmo conjunto do orçamento = A. Sem orçamento e com
λ = 0, o conservador leva 10 índices (900 MB): qualquer ganho acima do
limiar paga, inclusive os 7 ms que `(l_partkey) INCLUDE (...)` ganha sobre o
índice composto na Q17 e na Q19.

**Ganho medido por query** (A sessão 1 = o primeiro experimento, cada query
com o índice do próprio cluster; as outras colunas são conjuntos criados
inteiros nesta rodada; Q1, Q7, Q13 e Q18 não usam índice nenhum em
coluna nenhuma):

| query | A, sessão 1 | A como conjunto | otim. = A | otim. 50 MB | otimista |
| --- | --- | --- | --- | --- | --- |
| Q17 | 99,6% (piso) | 100,0% (piso) | 100,0% (piso) | 99,6% (piso) | 100,0% (piso) |
| Q20 | 100,0% (piso) | 100,0% (piso) | 100,0% (piso) | 99,4% (piso) | 100,0% (piso) |
| Q19 | 92,9% | 92,9% | 91,3% | 92,7% | 90,2% |
| Q8 | 43,9% | 46,9% | 30,7% | 42,2% | 30,0% |
| Q6 | 0 | 0 | 37,4% | 0 | 34,3% |
| Q14 | 0 | 0 | 35,6% | 0 | 33,8% |
| Q11 | 0 | 0 | 54,9% | 17,6% | 53,5% |
| Q9 | −3,2% | 6,4% | 0,6% | 28,1% | 13,3% |
| Q16 | 0 | 0 | −5,5% | 18,0% | −1,1% |
| Q21 | 0 | 0 | −21,8% | 13,7% | −20,7% |
| Q10 | 7,3% | −15,1% | −9,6% | 0 | 29,5% |
| Q3 | 0 | −60,6% | 0 | 0 | −40,2% |
| Q2 | 0 | 0 | −40,0% | 4,5% | −27,5% |
| Q12 | 0 | 0 | −1,0% | 0 | −17,1% |
| Q22 | 0 | 0 | 9,4% | 0 | −5,4% |
| Q4 | −5,7% | 9,5% | −1,1% | 0 | −3,5% |
| Q5 | −1,4% | 2,7% | −10,5% | 0 | −3,5% |

Leitura:

- **O que é robusto, todos acertam.** Q17 e Q20 (≥300 s → 51 ms a 1,7 s),
  Q19 (~570 → 40–55 ms) e Q8 (30–47%). O conjunto de 50 MB faz isso com
  `lineitem(l_partkey)` (43 MB) no lugar das duas variantes com `INCLUDE` do
  método A (455 MB): 8% do espaço. Q17 e Q20 ficam em 1,2–1,7 s em vez de
  60–80 ms, o que não aparece em total nenhum: na carga original as duas
  levaram em média ~4 h.
- **A regressão que o agrupamento não viu.** O índice de cobertura
  `orders(o_orderdate) INCLUDE (...)`, escolhido por um cluster do método A,
  piora a Q3: −30% na matriz, −61% no conjunto do A, −40% no otimista. Na
  sessão 1 a Q3 não usava esse índice; o plano mudou entre sessões. O
  conservador viu a regressão na matriz e deixou o índice de fora. O otimista,
  que supõe que o planner escolhe bem, trouxe o índice de volta e pagou com a
  Q3.
- **Q6 e Q14 são ganho que só a seleção global achou.** O índice
  `lineitem(l_shipdate, l_discount, l_quantity)` (181 MB) veio da heurística
  sobre o corpus inteiro e não de cluster nenhum. Na matriz e nas duas
  validações que o incluíram, Q6 e Q14 melhoraram 29–42% (no otimista, a Q14
  usou o vizinho `(l_shipdate, l_partkey, l_suppkey)`).
- **Ganho pequeno é ruído.** A mesma query sem índice variou 16% (mediana)
  entre as cinco linhas de base desta rodada, chegando a 46% (Q16), 43% (Q11)
  e 40% (Q9). O limiar de 10% fica abaixo disso, e o solver compra índice
  para ruído: Q12, Q4, Q16 e Q21, previstas entre +110 e +210 ms, mediram
  entre −179 e +110 ms. É a maldição do otimizador: escolher pelo máximo de
  estimativas ruidosas superestima o ganho. O conjunto do orçamento do A
  previa 1.751 ms fora de Q17/Q20 e entregou 695. O inverso também acontece:
  os +605 ms da Q9 no conjunto de 50 MB vêm de uma linha de base alta naquela
  sessão (2.156 ms, contra 1.543–1.822 nas outras), não do índice.
- **A hipótese de interação segurou; a de magnitude, não.** Em 41 de 43
  queries o plano usou o índice que o modelo previa. O erro está no tamanho
  de Δ, não em qual índice atende. A única interação provável é a Q2:
  `partsupp(ps_suppkey, ps_partkey)` e `part(p_type, p_size)` são neutros
  sozinhos na matriz e juntos dão −40% e −28%, coisa que o modelo do máximo
  não vê por construção. Entre os dois modelos, o conservador errou menos:
  classe igual (melhora / neutra / piora) em 20 de 21 (50 MB) e 14 de 21
  (orçamento = A), contra 12 de 21 do otimista.

Limites desta amostra:

- 21 queries, uma por template; Q17 e Q20 são piso dos dois lados da
  previsão.
- A matriz mede cada índice uma vez. Com o ruído medido acima, o limiar
  deveria ser por query (algo como a amplitude da linha de base dela), ou a
  matriz deveria repetir sessões; nenhum dos dois foi feito.
- `ANALYZE` é amostral: o mesmo índice deu +14% e −22% na Q21 em validações
  diferentes, o que é compatível com plano trocando por estatística. Não foi
  confirmado.
- Na mesma VM rodava outro projeto (containers ociosos, mas no ar).

Tudo está em `figuras/tpch-puro/otimizacao/`: os JSON de `otimizar` e de
cada `validar`, `comparacao_metodoA.json`, `matriz_sessao2.log` e
`validar.log`.

## Resultados: YCSB, otimização com trigramas

Mesmo banco `ycsb` (usertable, 1M linhas, só a PK) e mesma configuração
padrão do experimento de 25/10. O `brokk.dump` daquele arquivo foi
restaurado em `brokk_ycsb_otimizacao`, e o baseline é a rodada 1 de lá e o
método A a rodada 2. A diferença desta rodada: `matriz --trigramas`, com
`CREATE EXTENSION pg_trgm` no `ycsb`.

```bash
export BROKK_DSN_OBSERVADO=postgresql://sindri:sindri_dev_pwd@localhost:5432/ycsb
./.venv/bin/python -m src.index_provider --banco ycsb matriz --globais 60 --trigramas --rodadas 1,2
./.venv/bin/python -m src.index_provider otimizar --sessao 1 --rodadas 1,2 --consultas queries/ycsb
```

- **Matriz:** 11 índices (6 da heurística, um deles o GIN de trigrama, e os
  5 das rodadas) × 24 queries, em 1,6 min.
- **Os 4 B-tree simples da heurística** (`field2`, `field3`, `field7`,
  `field9`) não foram usados por query nenhuma: os filtros são
  `left(field2, 3) = ...` e `LIKE '%...%'`, e a heurística não gera índice de
  expressão. Os índices de expressão vêm das rodadas.
- **Os dois modelos** (conservador e otimista) escolheram o mesmo conjunto.

| conjunto | índices | MB | melhoram | pioram | Σ execuções × Δ (s) |
| --- | --- | --- | --- | --- | --- |
| baseline 25/10 (`medir`, por cluster) | 4 | 608 | 3 (y06, y17, y15) | 2 (y14 −52%, y11 −46%) | 340 |
| método A 25/10 (`medir`, por cluster) | 1 | 238 | 2 (y15, y22) | 0 | 119 |
| baseline remedido como conjunto | 4 | 608 | 2 (y06, y17) | 0 | 216 |
| método A remedido como conjunto | 1 | 238 | 0 | 0 | 0 |
| **otimização** | 3 | **276** | **3** (y06, y17, y19) | **0** | **313** |

"Melhora" e "piora" pedem ±10% **e** pelo menos 1 ms. Sem o piso em ms, as
queries de 0,1 ms entram por ruído (o ReadRecord do YCSB deu +55% num
conjunto e −31% no outro, com o mesmo tipo de índice).

O conjunto escolhido:

| índice | MB | origem | efeito medido |
| --- | --- | --- | --- |
| `usertable (left(field2, 3))` | 6,8 | rodada 1 | y06 685 → 0,1 ms |
| `usertable (left(field7, 1))` | 6,7 | rodada 1 | y17 346 → 23 ms |
| `usertable USING gin (field9 gin_trgm_ops)` | 262,6 | heurística, trigrama | y19 244 → 2,2 ms |

Leitura:

- **O trigrama resolveu a query que ficou sem índice em 25/10.** A y19
  (`LIKE '%' || chr(…) || '%'`) caiu de 244 para 2,2 ms. Ponderada pelas 343
  execuções, ela é 83 s dos 313 s do conjunto.
- **Na mesma sessão, a otimização ganha do baseline em benefício e em
  espaço:** 313 s contra 216 s, com 276 MB contra 608 MB. Os dois índices
  de cobertura do baseline (594 MB) não deram ganho acima de 1 ms em query
  nenhuma, e o otimizador os deixou de fora. Sem o trigrama, o otimizador
  teria o mesmo benefício do baseline remedido com 13,5 MB, 45× menos
  espaço.
- **Previsão boa:** classe igual em 24 de 24 queries e o índice previsto em
  3 de 3. O medido (313 s) ficou acima do previsto (259 s) porque as linhas
  de base de y06 e y17 vieram mais altas nesta sessão (685 contra 491 ms).
- **O que se perdeu desde 25/10:** a y15 melhorava 64% com
  `(ycsb_key) INCLUDE (field1)`, e nesta sessão o planner não usou o índice
  nela, nem na matriz nem nas validações. Por isso o baseline de 25/10 soma
  340 s, mais que qualquer conjunto de agora. Não foi investigado por que o
  plano mudou.
- **O custo de escrita ficou de fora, e no YCSB ele pesa.** O corpus tem
  186.221 escritas em `usertable`, e todo índice novo as paga, o GIN mais que
  todos. Com `--custo-escrita` em 0, o modelo não as vê. O ponto de
  equilíbrio do GIN é ~0,44 ms por escrita (83 s / 186.221): se a manutenção
  dele custar mais que isso em média, o trigrama não se paga nesta carga.
  Isso precisa de uma corrida de escrita com e sem o índice, que a bancada
  não faz (ela só reexecuta leitura).

Limites: 24 queries; uma sessão de matriz; o plano da y15 mudou entre
sessões. Os arquivos estão em `figuras/ycsb/otimizacao/`.

## Resultados: TPC-H com PGTune, otimização × baseline e método A

O corpus do TPC-H coletado **com o PGTune** (power run de
25–26/10; 21 queries replicáveis, 12 execuções cada) e o experimento de
clusters medido sobre ele em 30/10: baseline é a rodada 1 (SQL inteiro, 4
clusters, 358 MB) e método A a rodada 2 (`from-where`, 3 clusters, 294 MB).
O `brokk` foi copiado para `brokk_tpch_pgtune` (`CREATE DATABASE ... TEMPLATE
brokk`) e tudo rodou na cópia, com o servidor no PGTune. Com `--trigramas`,
o que pediu `CREATE EXTENSION pg_trgm` no `tpch`.

```bash
export BROKK_DSN=postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk_tpch_pgtune
./.venv/bin/python -m src.index_provider matriz --globais 28 --trigramas --rodadas 1,2
./.venv/bin/python -m src.index_provider otimizar --sessao 1 --rodadas 1,2
./.venv/bin/python -m src.index_provider otimizar --sessao 1 --orcamento-mb 50,294.2,358.1
./.venv/bin/python -m src.index_provider otimizar --sessao 1 --modelo otimista
```

- **Matriz, sessão 1:** 32 índices (os 28 da heurística, 3 deles de
  trigrama, e 4 que só as rodadas propuseram) × 21 queries, em 52,5 min.
  Linha de base de 662 s por passada, quase toda em Q17 e Q20, que estouram
  os 300 s também com o PGTune.
- **Trigramas:** `part(p_type)` (Q2), `part(p_name)` (Q9) e
  `supplier(s_comment)` (Q16). Na matriz, só o último ganhou (+22% na Q16);
  o de `p_type` piorou a Q2 em 10% e o de `p_name` foi neutro na Q9.
- **Validação:** sete conjuntos, ~11,5 min cada. O de 50 MB foi validado
  duas vezes (otimizações 4 e 8, mesmo conjunto) para medir o ruído entre
  sessões.

| conjunto | índices | MB | melhoram | pioram | Q17 + Q20 depois (ms) | fora de Q17/Q20, previsto → medido (ms por passada) |
| --- | --- | --- | --- | --- | --- | --- |
| baseline 30/10 (`medir`, por cluster) | 4 | 358 | 4 | 1: Q4 −56% | — | — → 314 |
| método A 30/10 (`medir`, por cluster) | 3 | 294 | 5 | 3: Q10 −42%, Q5 −32%, Q4 −18% | — | — → 950 |
| baseline como conjunto (otim. 2) | 4 | 358,1 | 5 | 3: Q3 −46%, Q7 −22%, Q5 −13% | 116 | 433 → 451 |
| método A como conjunto (otim. 3) | 3 | 294,2 | 5 | 5: Q22 −53%, Q3 −34%, Q7 −30%, Q10 −13%, Q5 −10% | 1.060 | 696 → 206 |
| **otimização, 50 MB (otim. 4)** | 3 | **44,3** | 4 | 5: Q7 −45%, Q16 −40%, Q2 −31%, Q9 −16%, Q21 −12% | 1.422 | 862 → −203 |
| **o mesmo, outra sessão (otim. 8)** | 3 | **44,3** | 6 | **0** | 1.256 | 862 → 533 |
| otimização, 294 MB = A (otim. 5) | 6 | 292,8 | 8 | 2: Q6 −35%, Q21 −23% | 115 | 1.185 → 516 |
| otimização, sem limite (otim. 1) | 10 | 795,1 | 7 | 4: Q21 −32%, Q10 −21%, Q22 −17%, Q16 −13% | 136 | 1.528 → 426 |
| otimista, sem limite (otim. 7) | 12 | 982,5 | 8 | 6: Q3 −112%, Q5 −90%, Q7 −40%, … | 137 | 1.600 → −100 |

"Melhora" e "piora" pedem ±10% e pelo menos 1 ms, como no YCSB. O orçamento
de 358 MB deu um conjunto de 336 MB que é o de 294 MB mais
`lineitem(l_partkey)`; não foi validado à parte.

O conjunto de 50 MB:

| índice | MB | origem | atende |
| --- | --- | --- | --- |
| `lineitem (l_partkey)` | 43,0 | rodadas 1 e 2 e heurística | Q17, Q20, Q19, Q8 |
| `supplier (s_nationkey)` | 0,1 | heurística | Q7, Q2, Q11 |
| `supplier USING gin (s_comment gin_trgm_ops)` | 1,2 | heurística, trigrama | Q16 |

**Ganho medido por query** (as colunas de 30/10 com o índice do cluster de
cada query; — = a query caiu no ruído do HDBSCAN e ficou sem índice; Q1,
Q13, Q15 e Q18 ficam fora, nenhum conjunto as atende):

| query | baseline 30/10 | A 30/10 | baseline conj. | A conj. | 50 MB (4) | 50 MB (8) | 294 MB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q17 | 99,6% (piso) | 99,5% (piso) | 100,0% (piso) | 99,7% (piso) | 99,6% (piso) | 99,6% (piso) | 100,0% (piso) |
| Q20 | 100,0% (piso) | 100,0% (piso) | 100,0% (piso) | 100,0% (piso) | 100,0% (piso) | 100,0% (piso) | 100,0% (piso) |
| Q19 | 89,5% | 86,3% | 91,1% | 89,5% | 83,7% | 89,5% | 88,9% |
| Q8 | — | 47,5% | 44,7% | 33,1% | 18,6% | 42,5% | 48,4% |
| Q14 | 0 | 0 | 0 | 0 | 0 | 0 | 41,4% |
| Q11 | 0 | 0 | 0 | 0 | 0,8% | 14,6% | 28,9% |
| Q9 | — | 40,5% | 9,9% | 10,8% | −16,1% | 2,8% | 14,6% |
| Q7 | — | 0 | −22,5% | −29,8% | −45,2% | 18,4% | 10,8% |
| Q22 | 53,6% | 0 | 54,5% | −52,9% | 0 | 0 | −7,2% |
| Q16 | 0 | 0 | 0 | 0 | −39,8% | 6,9% | 3,3% |
| Q2 | 0 | 0 | 0 | 0 | −31,1% | 3,1% | −9,9% |
| Q21 | 0 | 0 | 0 | 0 | −12,0% | 0,8% | −23,2% |
| Q6 | 0 | 0 | 0 | 0 | 0 | 0 | −35,4% |
| Q3 | 0 | 0 | −45,9% | −34,4% | 0 | 0 | 0 |
| Q10 | 0 | −42,4% | 3,8% | −13,2% | 0 | 0 | −3,2% |
| Q5 | — | −32,4% | −13,1% | −10,3% | 0 | 0 | 0,6% |
| Q4 | −55,7% | −17,6% | −1,0% | 1,3% | 0 | 0 | 3,3% |

Leitura:

- **O que é robusto, todos acertam, de novo.** Q17 e Q20 (≥300 s → 44 ms a
  1,3 s) e Q19 (~320–370 → 33–53 ms) são mais de 99% do ganho de qualquer
  conjunto, e o de 50 MB os pega com 44 MB, contra 358 MB do baseline e 294
  MB do A. O preço do orçamento pequeno está na coluna "Q17 + Q20 depois":
  só `l_partkey` deixa as duas em ~1,3 s, o índice de cobertura de 232 MB
  (`(l_partkey, l_suppkey, l_shipdate) INCLUDE (l_quantity)`) em ~0,1 s.
  São ~1,2 s numa passada que, com índice, leva ~60 s.
- **O mesmo conjunto, duas sessões: −203 e +533 ms.** As cinco regressões da
  primeira validação do conjunto de 50 MB sumiram na segunda (Q7 −45% →
  +18%, Q16 −40% → +7%, Q2 −31% → +3%), com os mesmos índices no plano.
  A diferença entre as duas (736 ms por passada) é maior que a distância
  entre quaisquer dois conjuntos validados nesta rodada. Com uma validação
  por conjunto, esse ranking não se separa do ruído.
- **O ruído é maior que na configuração padrão.** A mesma query sem índice
  variou 32% (mediana) entre as oito linhas de base desta rodada, chegando a
  80% (Q9), 79% (Q22) e 59% (Q8); sem o PGTune eram 16% em cinco linhas de
  base (com as quatro primeiras daqui, 27%). Os 4 workers por Gather e o
  `shared_buffers` de 1,9 GB disputando a VM com o banco de monitoramento
  são candidatos; não foi isolado.
- **No mesmo espaço do A, a otimização ganha.** O conjunto de 294 MB (6
  índices) tem 8 melhoras e 2 pioras contra 5 e 5 do A como conjunto, deixa
  Q17 + Q20 em 115 ms contra 1.060 e acha a Q14 (+41%), que nenhum cluster
  atende. Uma das pioras foi escolhida de olhos abertos: o modelo previa
  −45 ms na Q6 com `lineitem(l_shipdate)` e aceitou pelos +105 da Q14.
- **A regressão do índice de cobertura de `orders` voltou.**
  `orders(o_orderdate) INCLUDE (...)`, que os dois métodos de cluster
  escolhem, piora a Q3 em 34–46%, como sem o PGTune. O conservador não o
  pôs em orçamento nenhum.
- **O otimista é o pior de novo:** Q3 −112% e Q5 −90%, as duas usando
  `orders(o_custkey)`. Na matriz esse índice já piorava a Q3 (−80%) e a Q5
  (−58%), e o conservador o deixou de fora.
- **O trigrama não se mostrou.** O GIN de `s_comment` entrou no conjunto de
  50 MB pelos +22% da Q16 na matriz e mediu −40% e +7% nas duas validações:
  ruído. Ocupa 1,2 MB e não atrapalhou de forma consistente. No TPC-H os
  `LIKE` com curinga caem em `part` e `supplier`, tabelas pequenas cujo seq
  scan já é barato; no YCSB o trigrama valia porque a `usertable` tem 1M
  linhas.
- **Q21 é a regressão que parece real.** A linha de base dela é das mais
  estáveis (7% de amplitude), e com `supplier(s_nationkey)` ela piorou
  23–32% em três conjuntos, 12% num e nada no outro, com o mesmo índice no
  plano. A matriz tinha medido −1%.

Limites: 21 queries, uma por template; uma sessão de matriz; uma validação
por conjunto, fora o de 50 MB; Q17 e Q20 são piso. Os arquivos estão em
`figuras/tpch-pgtune/otimizacao/`: `matriz.log`, os JSON de `otimizar` e de
cada `validar`, `validar.log` (otimizações 4, 2, 3), `validar2.log` (8, 5, 1,
7) e `comparacao.json`.

## Resultados: seleção adaptativa

Tudo rodou em **cópias** dos bancos de experimento (`brokk_tpch_pgtune_adapt`
e `brokk_ycsb_adapt`, `CREATE DATABASE ... TEMPLATE`), para a migração não
tocar nos originais. Os padrões de `adaptar` não foram mudados entre as
execuções. Arquivos em `figuras/tpch-pgtune/adaptativa/` e
`figuras/ycsb/adaptativa/`.

### TPC-H com PGTune

A mesma sessão de matriz (32 índices × 21 queries) e as 7 validações de 1/11,
agora `legado`. O servidor estava no PGTune (as GUCs batem com as do
experimento); a matriz e as validações antigas não têm ambiente registrado, e
as novas, todas o mesmo (`67c9b7ec26546e7a`).

```bash
export D=postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk_tpch_pgtune_adapt
./.venv/bin/python -m src.index_provider --dsn $D adaptar --sessao 1 --ruido
./.venv/bin/python -m src.index_provider --dsn $D adaptar --sessao 1                     # offline
./.venv/bin/python -m src.index_provider --dsn $D adaptar --sessao 1 --executar --max-medicoes 4   # 2×
./.venv/bin/python -m src.index_provider --dsn $D adaptar --sessao 1 --confirmar 8 --executar
```

**Offline, só com o que já existia**, a caminhada aceitou o conjunto de 44 MB
de `otimizar` (as otimizações 4 e 8 viram uma configuração com duas sessões)
e pediu a próxima medição. Já ali o modelo nomeou, sozinho, as queries em que
as duas sessões do conjunto de 44 MB discordam: Q2, Q7, Q8, Q16 e Q19.

**Com a bancada**, 8 validações de seleção em duas execuções (45,7 e 46,7
min), mais a confirmação. O caminho final, sobre as 16 sessões:

| passo | movimento | ΔB (s) [IC 90%] | margem (s) | ΔMB | decisão |
| --- | --- | --- | --- | --- | --- |
| ∅ → {i1,i14,i31} | já medida (`otimizar` ≤ 50 MB) | ≥ +8.628,6 [+8.621,3; +∞) | 87,9 | +44,3 | superioridade: aceita |
| {i1,i14,i31} → {i2,i14,i17,i21} | fronteira prevista, medida 2× | +24,7 [+16,1; +32,7] | 6,0 | +204,1 | superioridade: aceita |
| → {i2,i3,i4,i14,i17,i21} | combinação (exploração) | −2,5 [−8,4; +1,8] | 4,9 | +83,0 | ganho insuficiente |
| → {i2,i3,i5,i14,i17,i21} | combinação (exploração) | −4,5 [−10,0; −0,1] | 4,9 | +83,0 | ganho insuficiente |
| parada | | | | | 2 crescimentos seguidos rejeitados |

A recomendada, 248,4 MB:

| índice | MB |
| --- | --- |
| `lineitem (l_partkey, l_suppkey, l_shipdate) INCLUDE (l_quantity)` | 232,1 |
| `supplier (s_nationkey)` | 0,1 |
| `orders (o_orderdate)` | 10,4 |
| `customer (c_acctbal, c_phone)` | 5,8 |

Melhoram com evidência Q8 (+43%), Q9 (+26%), Q11 (+16%), Q19 (+91%), Q17 e
Q20 (piso: saem do timeout); **nenhuma piora** com evidência. Os métodos na
mesma régua (benefício contra a linha de base, sessões de seleção):

| conjunto | MB | sessões | + | − | situação |
| --- | --- | --- | --- | --- | --- |
| clusters, rodada 1 (baseline HDBSCAN) | 358,1 | 1 | 5 | 1 | dominada pela recomendada |
| clusters, rodada 2 (método A) | 294,2 | 1 | 4 | 3 | dominada pela recomendada |
| `otimizar` ≤ 50 MB | 44,3 | 2 | 4 | 1 | compacta, com perda relevante (−24,7 s) |
| `otimizar` ≤ 294 MB | 292,8 | 1 | 5 | 1 | não ordenável contra a recomendada (+1,8 s [−3,7; +8,3]) |
| `otimizar` sem orçamento | 795,1 | 1 | 8 | 1 | não ordenável contra a recomendada |
| otimista sem orçamento | 982,5 | 1 | 8 | 3 | dominada |
| **adaptar** | **248,4** | 2 | 6 | 0 | recomendada |

Leitura:

- **O ganho grande todo mundo pega; o resto é pequeno e ruidoso.** Tirar Q17
  e Q20 do timeout é > 99% do benefício de qualquer conjunto. O passo de 44
  para 248 MB vale +24,7 s sobre a janela do corpus (12 execuções por query,
  ~2 s por passada); a maior parte vem do composto em `l_partkey`, que deixa
  Q17 + Q20 em ~0,1 s em vez de ~1,3 s por passada (~14 s na janela), e o
  resto de Q8, Q9 e Q11. É pouco, mas o intervalo inteiro está
  acima da margem (6,0 s = a flutuação natural da carga entre sessões nessa
  configuração).
- **Parar é uma decisão com número.** Depois dos 248 MB, nenhuma candidata
  prevista passava da margem, e as duas explorações medidas ficaram abaixo
  dela mesmo no limite superior do intervalo. Os conjuntos maiores que já
  existiam (293 e 795 MB) não se distinguem da recomendada com uma sessão,
  e o relatório diz "não ordenável", não "pior".
- **O pareamento mudou a resposta.** Sem ele, o conjunto de 248 MB aparecia
  com três regressões (Q5, Q10, Q21), que eram as sessões das 20h45 rodando
  ~15% mais lentas que as das 18h, não o índice.
- **As regressões que o README atribuía a `orders`** reapareceram
  medidas: as quatro combinações com `orders (o_custkey)` ou
  `orders (o_orderdate) INCLUDE (...)` tiveram 3 ou 4 queries piorando com
  evidência, e todas saíram dominadas.

**A confirmação independente** mediu de novo, em sessões com finalidade
`confirmacao` que a seleção não lê, o passo decisivo {i1,i14,i31} →
{i2,i14,i17,i21}, com a margem e o teste da seleção (superioridade, 6,0 s):

| | ΔB (s) [IC 90%] | sessões | veredito |
| --- | --- | --- | --- |
| seleção | +24,7 [+16,1; +32,7] | as que escolheram | aceita |
| confirmação, 1ª rodada | +11,8 [+5,8; +18,2] | v4.2, v12.3 | inconclusiva |
| confirmação, 2ª rodada | **+8,1 [+3,1; +12,9]** | v4.2, v4.3, v12.3, v12.4 | inconclusiva |

- **O ganho existe**: em quatro sessões independentes, o intervalo do passo
  exclui zero.
- **Se ele é relevante, os dados não decidem**: o intervalo cruza a margem
  declarada. A confirmação não "falhou" (`confirmada` fica nula, e a
  exploração não se reabre), e também não sustenta a recomendação como foi
  escolhida.
- **A seleção superestimou o passo em ~3×.** É a maldição do otimizador,
  agora medida: o passo foi aceito justamente porque as sessões de seleção o
  favoreceram. Sem a confirmação à parte, o número apresentado seria +24,7 s.

Para o DBA, com a evidência que existe: o conjunto de **44 MB**
(`lineitem (l_partkey)`, `supplier (s_nationkey)` e o GIN de `s_comment`)
captura praticamente todo o benefício (tirar Q17 e Q20 do timeout). Os
**204 MB a mais** do conjunto de 248 MB compram ~8 s na janela do corpus
(~0,7 s por passada, intervalo de 0,3 a 1,1 s), sem regressão com evidência
— ganho real, relevância não demonstrada. Entre os dois, a decisão é de
quanto vale o espaço para quem opera; declarado como λ (`--lambda`), ela sai
do próprio teste. Para fechar a questão estatisticamente, mais sessões de
confirmação (`adaptar --confirmar 8 --executar --repetir-confirmacao`).

**Os métodos em condições equivalentes.** Depois da seleção, os conjuntos de
cada método foram medidos de novo na mesma janela (23h26–00h38 UTC), 2
sessões cada, finalidade `confirmacao` (a seleção não lê), ordem dos blocos
alternada e sequência invertida na segunda rodada
(`validar --nova-validacao --finalidade confirmacao --confirma-adaptacao 8`;
a tabela sai de `adaptar --confirmar 8`). Por passada (÷ 12 execuções),
contra a linha de base das mesmas sessões; + e − são queries cujo intervalo
exclui zero:

| conjunto | índices (MB) | MB | Q17+Q20 depois (ms) | fora de Q17/Q20 (ms) [IC 90%] | + | − | contra a adaptativa (ms) [IC 90%] |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HDBSCAN, rodada 1 | l_partkey 43,0 · composto 232,1 · o_custkey 12,0 · o_orderdate INCLUDE 71,0 | 358,1 | 120 | +267 [+17; +483] | 5 | 3 | −167 [−525; +217], não ordenável; dominada pela adaptativa |
| HDBSCAN, método A | l_partkey 43,0 · (l_partkey, l_suppkey, l_shipdate) 180,3 · o_orderdate INCLUDE 71,0 | 294,2 | 1.068 | +150 [−92; +358] | 4 | 5 | −1.233 [−1.608; −842]; dominada pela de 44 MB |
| `otimizar` ≤ 50 MB | l_partkey 43,0 · s_nationkey 0,1 · GIN s_comment 1,2 | 44,3 | 1.127 | +767 [+483; +1.000] | 4 | 0 | −675 [−1.050; −292] |
| `otimizar` ≤ 294 MB | composto 232,1 · l_shipdate 40,7 · o_orderdate 10,4 · c_acctbal,c_phone 5,8 · part(p_size,…) 3,8 · s_nationkey 0,1 | 292,8 | 111 | +900 [+625; +1.142] | 6 | 1 | **+475 [+117; +858]** |
| `adaptar` | composto 232,1 · o_orderdate 10,4 · c_acctbal,c_phone 5,8 · s_nationkey 0,1 | 248,4 | 113 | +425 [+83; +708] | 4 | 0 | — |

O composto é `lineitem (l_partkey, l_suppkey, l_shipdate) INCLUDE
(l_quantity)`, 232,1 MB: 189 MB a mais que `lineitem (l_partkey)` para levar
Q17 + Q20 de ~1,1 s a ~0,11 s por passada.

- **A recomendação adaptativa não é a melhor medida.** O conjunto de 293 MB
  da versão anterior ganha dela por +475 ms por passada, com o intervalo
  excluindo zero; a diferença não passa da margem com evidência (410 ms por
  passada = 4,9 s na janela). Ele acrescenta `lineitem (l_shipdate)` (40,7
  MB) e `part (p_size, p_brand, p_container)` (3,8 MB): Q14 +55% e Q2 +43%,
  ao preço de Q6 −17%. Na seleção, com uma sessão, ele contra a adaptativa
  dava +1,8 s [−3,7; +8,3] na janela, o modelo não previa ganho acima da
  margem, e a caminhada não o remediu. É um limite real da regra de parada:
  alternativa já medida e "não ordenável" perto da margem não é revisitada.
- **O HDBSCAN fica atrás.** O método A é o pior (5 regressões: Q3, Q4, Q7,
  Q10, Q22) e é dominado pelo conjunto de 44 MB; o baseline usa 110 MB a
  mais que a adaptativa sem ganho distinguível, com Q3 −49% e Q7 −50%.
- **A instabilidade de uma validação só, medida.** O conjunto de 44 MB deu,
  fora de Q17/Q20, −203 e +533 ms nas validações de 1/11 e +767 [+483;
  +1.000] agora; o método A como conjunto, 206 antes e +150 [−92; +358]
  agora.

Dois erros da implementação, achados por estas execuções e corrigidos depois
delas (as medições que custaram ficam no banco, identificadas):

- a 4ª validação da primeira execução mediu `{i14,i31}`, isto é, tirar
  `lineitem (l_partkey)` para corrigir uma piora da Q9 — o que perde Q17 e
  Q20. A remoção por regressão agora só é proposta se a perda prevista for
  menor que a regressão que ela corrige;
- a segunda execução mediu 4 combinações com orçamento de exploração 2: a
  contagem era por caminhada, e a caminhada recomeça a cada medição. Agora o
  orçamento é do experimento (teste em `test_adaptativa.py`).

**Com a resolução de incerteza (adaptações 13 a 16).** As regras novas
foram desenhadas depois da comparação de métodos acima. Por isso a seleção
foi refeita na mesma bancada e no mesmo ambiente (`67c9b7ec26546e7a`), e a
confirmação veio em sessões novas. Foram três execuções de `adaptar
--executar`, com 13 validações de seleção (3 delas de resolução), e depois
duas rodadas de `--confirmar 16`, com 4 sessões. As duas primeiras execuções
pararam no limite de 6 medições, passando de variante em variante de
+357 MB, e expuseram o defeito da paciência e o da família de Bonferroni
(ver Parada e Multiplicidade). A terceira, já corrigida, concluiu com uma
medição. Contra a recomendada, na evidência final de seleção (29 sessões;
margem 4,8 s):

| alternativa | MB | ΔB (s) [IC 90%] | sessões | resolução |
| --- | --- | --- | --- | --- |
| `otimizar` ≤ 294 MB | 292,8 | −0,1 [−3,4; +2,9] | 2 | descartada (UCB < δ) |
| atual + {i1,i18,i19,i25} | 609,6 | +4,2 [+1,5; +7,0] | 3 | não ordenável, sem orçamento |
| atual + {i1,i7,i18,i19,i25} | 614,9 | +4,7 [+2,0; +7,3] | 3 | não ordenável, sem orçamento |
| atual + {i1,i18,i19,i31} | 607,0 | +2,3 [−3,6; +6,7] | 2 | não ordenável, sem orçamento |
| atual + {i1,i18,i19,i23} | 607,5 | −1,3 [−5,9; +2,7] | 2 | rejeitada |
| `otimizar` sem orçamento | 795,1 | −1,8 [−6,9; +2,0] | 2 | descartada (UCB < δ) |

A recomendada não mudou: {i2,i14,i17,i21}, 248,4 MB. A pergunta que ficou
aberta é o núcleo {i1,i18,i19}: `lineitem (l_partkey)`, `(l_shipmode,
l_commitdate, l_receiptdate)` e `(l_shipdate, l_discount, l_quantity)`, que
somam 357 MB. Em três variantes ele deu de +2 a +5 s na janela, sem provar
que passa da margem. É uma decisão de custo para o DBA (`--lambda`), não da
estatística.

O conjunto de 293 MB **não se reproduziu**. Nas sessões de seleção, com
uma remedição de resolução, ficou em −0,1 s [−3,4; +2,9]. Nas sessões da
comparação de métodos, tinha ficado em +475 ms por passada (+5,7 s na
janela) [+1,4; +10,3]. São fontes separadas, que a seleção nunca misturou, e
elas discordam. Só uma comparação na mesma sessão (o duelo, ver Limites)
fecharia essa diferença.

A confirmação da adaptação 16 mediu o passo decisivo de novo, com a margem
da seleção (5,5 s):

| | ΔB (s) [IC 90%] | sessões | veredito |
| --- | --- | --- | --- |
| seleção | +24,4 [+19,8; +30,5] | as que escolheram | aceita |
| confirmação, 1ª rodada | +10,7 [+5,8; +16,1] | v4.4, v12.6 | confirmada |
| confirmação, 2ª rodada | **+9,4 [+6,0; +13,1]** | v4.4, v4.5, v12.6, v12.7 | **confirmada** |

- **O passo se confirma**, mas com folga pequena: o limite inferior (6,0 s)
  passa da margem (5,5 s) por 0,5 s. A confirmação da adaptação 8, em outras
  4 sessões, deu +8,1 [+3,1; +12,9], com margem de 6,0 s, e ficou
  inconclusiva. As duas confirmações, separadas, concordam em ~8 a 9 s.
- **A seleção superestima o passo** em cerca de 2,6×: a confirmação mediu
  38% do ganho estimado, e a estimativa da seleção fica fora do intervalo da
  confirmação. O número a apresentar é o da confirmação.
- **O pareamento β ainda se move**: 0,73 ± 0,28 com 142 pontos, 0,34 ± 0,13
  com 261. Toda diferença entre duas configurações passa por ele, e a decisão
  sobre o conjunto de 607,5 MB mudou de inconclusiva para rejeitada em parte
  por isso.

### YCSB

Só offline. O YCSB foi medido com os padrões do Postgres, e o servidor está
hoje no PGTune: medir agora misturaria ambientes, que é o que este modo
existe para impedir.

```bash
export BROKK_DSN_OBSERVADO=postgresql://sindri:sindri_dev_pwd@localhost:5432/ycsb
./.venv/bin/python -m src.index_provider --dsn postgresql://brokk:brokk_dev_pwd@localhost:5433/brokk_ycsb_adapt \
    --banco ycsb adaptar --sessao 1 --consultas queries/ycsb
```

| conjunto | MB | benefício (s) [IC 90%] | sessões | situação |
| --- | --- | --- | --- | --- |
| clusters, rodada 1 (baseline) | 607,6 | +205,3 [+188,1; +224,2] | 1 | dominada por {i1,i2,i7}; `(ycsb_key) INCLUDE (field3..6)` (461 MB) sem uso |
| clusters, rodada 2 (método A) | 237,6 | +0,0 [−0,2; +0,2] | 2 | mais espaço sem ganho relevante |
| `otimizar` (com o GIN) | 276,1 | +283,0 [+264,8; +303,1] | 1 | ganho relevante sobre a atual |

A caminhada começa pelo `left(field2, 3)` de 6,8 MB (melhor eficiência
prevista: +116,7 s [+103,9; +131,3] contra margem de 61,5 s) e pede uma
segunda sessão antes de aceitá-lo. Para continuar, com o servidor de volta
aos padrões em que o YCSB foi medido:

```bash
./.venv/bin/python -m src.index_provider --dsn ...brokk_ycsb_adapt --banco ycsb \
    adaptar --sessao 1 --executar --consultas queries/ycsb
```

**Manutenção.** O conjunto de 276 MB fica exposto a 183 mil (INSERT +
UPDATE que atribui coluna indexada) a 418 mil (+ UPDATE elegível a HOT)
entradas de índice nas escritas do corpus, com 140 mil DELETE à parte. O
ponto de equilíbrio: a configuração deixa de se pagar se cada entrada custar
mais que **0,68 ms** (exposição máxima) a **1,54 ms** (mínima). É outra
pergunta que a conta à mão de antes (0,44 ms por escrita: o ganho só do GIN
dividido por todas as 186 mil escritas, inclusive DELETE e UPDATE que não
tocam `field9`); a do GIN sozinho pediria medir o conjunto sem ele. Sem
medida A/B de escrita, a configuração sai como "só a leitura avaliada".

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
| `otimizacao` | um problema resolvido por `otimizar` (ou o conjunto de uma rodada), com parâmetros, conjunto e previsão por query |
| `otimizacao_medida` | a validação: linha de base, índices usados (`repeticao = 0`) e medidas com o conjunto inteiro criado; `validacao` diz qual |
| `otimizacao_resultado` | view: o conjunto contra cada query, uma linha por validação, com o ganho medido |
| `otimizacao_validacao` | cada validação: finalidade, origem, ordem dos blocos, ambiente e a adaptação que a pediu ou que ela confirma |
| `adaptacao` | uma execução de `adaptar`: parâmetros, medições rodadas e lidas, motivo de parar, recomendada, confirmação |
| `adaptacao_passo` | cada passo da exploração: movimento, teste, ΔB com intervalo, margem, decisão |
| `custo_manutencao` | custo de manutenção medido por experimento A/B de escrita (vazia até existir o experimento) |
| `matriz_sessao.ambiente` | a impressão digital da sessão da matriz |

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

# modo de otimização
./.venv/bin/python -m src.index_provider matriz --globais 26
./.venv/bin/python -m src.index_provider otimizar --rodadas 11
./.venv/bin/python -m src.index_provider validar --otimizacao 1

# se algo ficou para trás
./.venv/bin/python -m src.index_provider limpar
```

## Dependências

`adaptar` não acrescenta dependência: usa `numpy` e `scipy` (o `milp` de
`otimizar` e `scipy.special` para a moderação e a censura) e o `pglast`, que
já estavam. Os testes usam só `unittest`, da biblioteca padrão.

`psycopg[binary]` para tudo. `anthropic` para `recomendar` e `candidatar`,
`gspread` + `google-auth` para `--planilha`, `sentence-transformers` +
`umap-learn` + `scikit-learn` para `vetorizar` e `agrupar`, `scipy` (>= 1.9,
pelo `milp`; já vem com o `scikit-learn`) para `otimizar`. Todas importadas
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
