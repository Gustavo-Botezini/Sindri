#!/usr/bin/env bash
#
# Cria e carrega a usertable do YCSB no banco "ycsb" com o log desligado.
#
# Com auto_explain e log_min_duration_statement em 0, cada INSERT em lote do
# load (128 linhas x 10 campos de 100 bytes) iria para logs/ com os parâmetros
# e o plano: perto de 1 GB de log que não é carga nenhuma, só o preparo. O
# desligamento é por banco (ALTER DATABASE), então o tpch continua logando, e
# volta no trap mesmo se o load falhar no meio.
#
# Uso (de dentro da WSL):
#   ./ycsb-load.sh
#
# Recarregar apaga a usertable e começa de novo: é assim que se desfaz o que
# InsertRecord/DeleteRecord/UpdateRecord mudaram numa execução anterior.

set -euo pipefail

PROJETO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJETO"

msg() { printf '\033[1m==>\033[0m %s\n' "$*"; }
psql_tpch() { docker exec sindri_postgres psql -U sindri -d tpch -v ON_ERROR_STOP=1 -Atqc "$1"; }

if [ "$(psql_tpch "select count(*) from pg_database where datname = 'ycsb'")" = 0 ]; then
  # Volume criado antes de initdb/04-ycsb-database.sql existir.
  msg "Criando o banco ycsb"
  psql_tpch "create database ycsb owner sindri"
  docker exec sindri_postgres psql -U sindri -d ycsb -qc "create extension if not exists pg_stat_statements"
fi

religar() {
  msg "Religando o log no banco ycsb"
  psql_tpch "alter database ycsb reset log_min_duration_statement"
  psql_tpch "alter database ycsb reset auto_explain.log_min_duration"
}
trap religar EXIT

msg "Desligando o log no banco ycsb durante o load"
psql_tpch "alter database ycsb set log_min_duration_statement = -1"
psql_tpch "alter database ycsb set auto_explain.log_min_duration = -1"

msg "Criando e carregando a usertable"
HOST_UID="$(id -u)" HOST_GID="$(id -g)" \
  docker compose --profile bench run --rm benchbase \
  -b ycsb -c sindri-config/ycsb.xml \
  --create=true --load=true --execute=false

# Estatísticas frescas antes da primeira execução, senão os primeiros planos
# do log saem de estimativas de tabela vazia.
docker exec sindri_postgres psql -U sindri -d ycsb -qc "analyze usertable"
docker exec sindri_postgres psql -U sindri -d ycsb -Atc \
  "select 'usertable: ' || count(*) || ' linhas, ' || pg_size_pretty(pg_total_relation_size('usertable')) from usertable"
