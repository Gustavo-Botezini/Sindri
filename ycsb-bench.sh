#!/usr/bin/env bash
#
# Carga mista sobre o banco ycsb: lê, insere, atualiza e apaga ao mesmo tempo.
# Dispara em paralelo, com a mesma duração:
#
#   leitura  ycsb_mixed.xml  22 consultas OLTP e OLAP          (templated)
#   escrita  ycsb_write.xml  INSERT, UPDATE e DELETE           (templated)
#   core     ycsb.xml        as 6 transações do YCSB original  (só com CORE=1)
#
# Uso (de dentro da WSL):
#   ./ycsb-bench.sh                 # 30 minutos (padrão)
#   ./ycsb-bench.sh 300             # 5 minutos
#   RECARREGAR=1 ./ycsb-bench.sh    # recria a usertable antes (ycsb-load.sh)
#   CORE=1 ./ycsb-bench.sh          # inclui o YCSB original como terceira carga
#   WRITE_RATE=50 ./ycsb-bench.sh   # rate da escrita (padrão 100 req/s)
#   DRY_RUN=1 ./ycsb-bench.sh       # só mostra o que seria executado
#
# Os planos caem em logs/ (auto_explain); os sumários do BenchBase em
# benchbase/results/ycsb/{leitura,escrita,core}.

set -euo pipefail

DURACAO="${1:-1800}"
WRITE_RATE="${WRITE_RATE:-100}"
CORE="${CORE:-0}"
RECARREGAR="${RECARREGAR:-0}"
DRY_RUN="${DRY_RUN:-0}"

PROJETO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERADOS="$PROJETO/benchbase/config/generated"
RESULTADOS="$PROJETO/benchbase/results/ycsb"

msg() { printf '\033[1m==>\033[0m %s\n' "$*"; }
erro() { printf '\033[31merro:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checagens
[ -n "${WSL_DISTRO_NAME:-}" ] || erro "rode de dentro da WSL (os bind mounts relativos precisam resolver como caminho Linux)"
command -v docker >/dev/null || erro "docker não encontrado no PATH da WSL"
cd "$PROJETO"

if ! docker exec sindri_postgres pg_isready -U sindri -d ycsb -q 2>/dev/null; then
  msg "Postgres não está aceitando conexões; subindo com docker compose"
  [ "$DRY_RUN" = 1 ] || docker compose up -d postgres
  for _ in $(seq 1 60); do
    docker exec sindri_postgres pg_isready -U sindri -q 2>/dev/null && break
    sleep 2
  done
fi

if [ "$RECARREGAR" = 1 ]; then
  msg "Recarregando a usertable"
  [ "$DRY_RUN" = 1 ] || ./ycsb-load.sh
fi

linhas="$(docker exec sindri_postgres psql -U sindri -d ycsb -Atc \
  "select count(*) from usertable" 2>/dev/null || echo 0)"
[ "$linhas" -gt 0 ] 2>/dev/null || erro "usertable vazia ou inexistente; rode ./ycsb-load.sh antes"

# Restos de um ALTER DATABASE ... SET que não foi desfeito deixariam a carga
# inteira sem plano no log, sem erro nenhum para avisar.
desligado="$(docker exec sindri_postgres psql -U sindri -d tpch -Atc \
  "select count(*) from pg_db_role_setting s join pg_database d on d.oid = s.setdatabase
    where d.datname = 'ycsb' and array_to_string(s.setconfig, ',') like '%min_duration%'")"
[ "$desligado" = 0 ] || erro "o log está desligado no banco ycsb (ALTER DATABASE ycsb RESET ALL para religar)"

# ------------------------------------------------------- configs com o tempo
# <time> e <rate> moram dentro do XML; as versionadas são copiadas com os
# valores trocados, como no run-bench.sh.
mkdir -p "$GERADOS"
printf '%s\n' '*' '!.gitignore' > "$GERADOS/.gitignore"

sed "s|<time>[0-9]*</time>|<time>${DURACAO}</time>|" \
  benchbase/config/ycsb_mixed.xml > "$GERADOS/ycsb-leitura.xml"
sed -e "s|<time>[0-9]*</time>|<time>${DURACAO}</time>|" \
    -e "s|<rate>[0-9]*</rate>|<rate>${WRITE_RATE}</rate>|" \
  benchbase/config/ycsb_write.xml > "$GERADOS/ycsb-escrita.xml"
sed "s|<time>[0-9]*</time>|<time>${DURACAO}</time>|" \
  benchbase/config/ycsb.xml > "$GERADOS/ycsb-core.xml"

# ------------------------------------------------------------------- cargas
# carga NOME BENCHMARK CONFIG: um docker compose run por carga, cada uma com a
# própria pasta de resultados (senão duas iniciadas no mesmo segundo
# colidiriam no nome do arquivo) e com a saída prefixada pelo nome.
carga() {
  mkdir -p "$RESULTADOS/$1"
  local cmd=(docker compose --profile bench run --rm -q benchbase
             -b "$2" -c "sindri-config/generated/$3"
             --create=false --load=false --execute=true
             -d "results/ycsb/$1")
  if [ "$DRY_RUN" = 1 ]; then
    echo "[$1] ${cmd[*]}"
    return 0
  fi
  HOST_UID="$(id -u)" HOST_GID="$(id -g)" "${cmd[@]}" 2>&1 \
    | grep --line-buffered -E 'Rate limited|ERROR|Exception' \
    | grep --line-buffered -v 'Procedure Invalid' \
    | sed -u "s/^/[$1] /"
  return "${PIPESTATUS[0]}"
}

msg "Duração ${DURACAO}s | leitura sem limite | escrita ${WRITE_RATE} req/s$([ "$CORE" = 1 ] && echo ' | core 200 req/s')"
msg "usertable com $linhas linhas; planos em logs/, sumários em benchbase/results/ycsb/"

pids=()
carga leitura templated ycsb-leitura.xml & pids+=($!)
carga escrita templated ycsb-escrita.xml & pids+=($!)
if [ "$CORE" = 1 ]; then
  carga core ycsb ycsb-core.xml & pids+=($!)
fi

falhas=0
for p in "${pids[@]}"; do
  wait "$p" || falhas=$((falhas + 1))
done
[ "$DRY_RUN" = 1 ] && exit 0

# ------------------------------------------------------------------- resumo
python3 - "$RESULTADOS" <<'PY'
import collections, csv, glob, json, os, sys
for pasta in sorted(glob.glob(os.path.join(sys.argv[1], '*'))):
    sumarios = sorted(glob.glob(os.path.join(pasta, '*.summary.json')), key=os.path.getmtime)
    if not sumarios:
        continue
    s = json.load(open(sumarios[-1]))
    lat = s['Latency Distribution']
    print(f"{os.path.basename(pasta):8} {s['Measured Requests']:>8} requests  "
          f"{s['Throughput (requests/second)']:7.1f} req/s  "
          f"mediana {lat['Median Latency (microseconds)'] / 1000:.1f} ms  "
          f"p99 {lat['99th Percentile Latency (microseconds)'] / 1000:.1f} ms")
    tipos = collections.Counter(r['Transaction Name']
                                for r in csv.DictReader(open(sumarios[-1].replace('.summary.json', '.raw.csv'))))
    print('         ' + '  '.join(f'{k}={v}' for k, v in sorted(tipos.items())))
PY
docker exec sindri_postgres psql -U sindri -d ycsb -Atc \
  "select 'usertable: ' || count(*) || ' linhas (' || count(*) filter (where ycsb_key >= 5000000) || ' na cauda sintética)' from usertable"
du -sh logs | sed 's/^/logs\/: /'

[ "$falhas" = 0 ] || erro "$falhas carga(s) terminaram com erro; veja as linhas acima"
