# `src/` — os programas do BrokkPG

Um README por módulo, cada um explicando como aquele arquivo funciona por
dentro. O README da raiz do projeto descreve a bancada (docker compose,
instrumentação, corpus de logs); estes descrevem o código.

## O pipeline

```
Postgres observado (tpch:5432)
        │  jsonlog + auto_explain
        ▼
   logs/*.json
        │
        ├──────────────────────────────────────────┐
        ▼                                          ▼
 1. query_colector.py                      schema_estimator.py
    grava o evento bruto                   reconstrói o esquema só do log
        ▼                                  (fora do pipeline, sem banco)
 monitoring.pg_log_event
        │
        ▼
 2. plan_worker.py
    parseia o plano do auto_explain
        ▼
 monitoring.query / plan_execution / plan_node / query_resumo
        │
        ▼
 3. index_provider/
        ├─ clássico:  dossiê ──▶ Claude ──▶ CSV / Google Sheets
        └─ clusters:  embeddings ──▶ UMAP + HDBSCAN ──▶ Claude
                          ──▶ CREATE INDEX no tpch:5432 ──▶ ganho MEDIDO
```

| módulo | o que faz | leia |
| --- | --- | --- |
| `query_colector.py` | lê `logs/*.json` e grava o evento bruto no monitoramento | [README_query_colector.md](README_query_colector.md) |
| `plan_worker.py` | transforma os planos em `query` / `plan_execution` / `plan_node` | [README_plan_worker.md](README_plan_worker.md) |
| `index_provider/` | monta o dossiê, pede recomendações de índice e **mede** o resultado | [README_index_provider.md](README_index_provider.md) |
| `schema_estimator.py` | reconstrói o esquema a partir do log, sem conectar no banco | [README_schema_estimator.md](README_schema_estimator.md) |

A Etapa 3 é a única que é um pacote, e por um motivo: além do recomendador
clássico, ela carrega uma bancada que cria índice de verdade no banco
observado, reexecuta as queries e derruba o índice. Isso não cabia num
arquivo só.

## Ambiente

```bash
python3 -m venv .venv
./.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
./.venv/bin/pip install -r requirements.txt
```

`schema_estimator.py` roda com a biblioteca padrão. `query_colector.py` e
`plan_worker.py` precisam de `psycopg[binary]`.

O `index_provider/` divide as suas dependências por subcomando, e todas são
importadas dentro da função que as usa — `dossie` e `relatar` rodam sem nada
além do psycopg:

| subcomando | precisa de |
| --- | --- |
| `dossie`, `relatar`, `limpar`, `esquema` | só `psycopg` |
| `recomendar`, `candidatar` | `anthropic` |
| `--planilha` | `gspread`, `google-auth` |
| `vetorizar`, `agrupar` | `sentence-transformers`, `umap-learn`, `scikit-learn` |
| `medir` | só `psycopg` (mas fala com o `tpch:5432`) |

A linha do `torch` vem **antes** do `requirements.txt` de propósito: sem ela o
`sentence-transformers` puxa a build com CUDA (~2,5 GB) numa máquina sem GPU.

Rode de dentro da WSL — pelo caminho UNC do Windows os bind mounts relativos do
compose não resolvem, e os symlinks do `.venv` também não.
