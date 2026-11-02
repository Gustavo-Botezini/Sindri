#!/usr/bin/env bash
#
# N passadas do power run do TPC-H (tpch_power.xml: 1 terminal, Q1..Q22 em
# ordem, uma vez cada), uma depois da outra. É a carga do corpus "tpch-puro":
# só as 22 queries, sem escrita e sem a carga complementar.
#
# Uma passada só não basta para o index_provider, que exige pelo menos 5
# execuções por query no corpus (MINIMO_EXECUCOES em corpus.py). O padrão é 6.
#
# Uso (de dentro da WSL):
#   ./tpch-power.sh          # 6 passadas
#   ./tpch-power.sh 3        # 3 passadas
#   DRY_RUN=1 ./tpch-power.sh
#
# Cada passada leva de 12 a 33 min com a configuração padrão do Postgres,
# quase tudo em Q20 e Q17 (sem índice em lineitem(l_partkey)).

set -euo pipefail

PASSADAS="${1:-6}"
DRY_RUN="${DRY_RUN:-0}"
PROJETO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

msg() { printf '\033[1m==>\033[0m %s\n' "$*"; }
erro() { printf '\033[31merro:\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "${WSL_DISTRO_NAME:-}" ] || erro "rode de dentro da WSL (os bind mounts relativos precisam resolver como caminho Linux)"
command -v docker >/dev/null || erro "docker não encontrado no PATH da WSL"
cd "$PROJETO"

docker exec sindri_postgres pg_isready -U sindri -d tpch -q 2>/dev/null \
  || erro "Postgres fora do ar: docker compose up -d postgres"

psql_tpch() { docker exec sindri_postgres psql -U sindri -d tpch -Atqc "$1"; }

# Mesmo motivo do ycsb-bench.sh: um ALTER DATABASE ... SET esquecido deixaria a
# carga inteira sem plano no log, sem erro nenhum para avisar.
[ "$(psql_tpch "select count(*) from pg_db_role_setting s join pg_database d on d.oid = s.setdatabase
                 where d.datname = 'tpch' and array_to_string(s.setconfig, ',') like '%min_duration%'")" = 0 ] \
  || erro "o log está desligado no banco tpch (ALTER DATABASE tpch RESET ALL para religar)"

cauda="$(psql_tpch "select count(*) from orders where o_orderkey > 6000000")"
[ "$cauda" = 0 ] || erro "orders tem $cauda linhas na cauda sintética da carga de escrita; veja 'Voltar a base ao estado original' no README"

docker ps --format '{{.Names}}' | grep -q benchbase \
  && erro "já há BenchBase rodando; o power run mede uma query por vez"

INICIO_UTC="$(date -u '+%Y-%m-%d %H:%M:%S')"
msg "$PASSADAS passada(s) do power run; planos em logs/, sumários em benchbase/results/power/"
msg "configuração: shared_buffers=$(psql_tpch 'show shared_buffers') work_mem=$(psql_tpch 'show work_mem') random_page_cost=$(psql_tpch 'show random_page_cost')"

mkdir -p benchbase/results/power
for i in $(seq 1 "$PASSADAS"); do
  cmd=(docker compose --profile bench run --rm -q benchbase
       -b tpch -c sindri-config/tpch_power.xml
       --create=false --load=false --execute=true -d results/power)
  if [ "$DRY_RUN" = 1 ]; then
    echo "[$i/$PASSADAS] ${cmd[*]}"
    continue
  fi
  inicio=$(date +%s)
  msg "passada $i/$PASSADAS começou às $(date +%H:%M:%S)"
  HOST_UID="$(id -u)" HOST_GID="$(id -g)" "${cmd[@]}" 2>&1 \
    | grep --line-buffered -E 'ERROR|Exception' | grep --line-buffered -v 'Procedure Invalid' || true
  msg "passada $i/$PASSADAS terminou em $(( ($(date +%s) - inicio) / 60 )) min"
done
[ "$DRY_RUN" = 1 ] && exit 0

# ------------------------------------------------------------------- resumo
# Os tempos saem dos planos do auto_explain, não dos CSVs do BenchBase: a
# latência dele corrompe acima de ~2147 s (2^31 µs) — a Q20 de 4050 s aparece
# como 441 s ou negativa, e a soma da passada chega a dar tempo negativo.
python3 - "$INICIO_UTC" <<'PY'
import collections, glob, json, statistics, sys
inicio = sys.argv[1]
tempos = collections.defaultdict(list)
for f in sorted(glob.glob('logs/*.json')):
    for linha in open(f, 'rb'):
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        m = r.get('message') or ''
        if (r.get('application_name') != 'benchbase-tpch-power'
                or r.get('timestamp', '') < inicio or 'plan:' not in m[:60]):
            continue
        texto = json.loads(m.split('\n', 1)[1]).get('Query Text', '')
        chave = ' '.join(texto.split())[:60]
        tempos[chave].append(float(m.split()[1]) / 1000)
print(f"{len(tempos)} queries distintas, {sum(map(len, tempos.values()))} planos")
for chave, ts in sorted(tempos.items(), key=lambda x: -statistics.median(x[1]))[:5]:
    print(f"  {len(ts):3}x  mediana {statistics.median(ts):8.1f} s  máx {max(ts):8.1f} s  {chave}")
PY
du -sh logs | sed 's/^/logs\/: /'
