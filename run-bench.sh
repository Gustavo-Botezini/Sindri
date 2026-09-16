#!/usr/bin/env bash
#
# Abre 4 painéis no Windows Terminal com a carga do BenchBase contra o Postgres
# do projeto: 2 de leitura analítica (TPC-H), 1 de escrita (INSERT/UPDATE/
# DELETE via benchmark "templated") e 1 de monitoramento ao vivo.
#
# Uso:
#   ./run-bench.sh              # 30 minutos (padrão)
#   ./run-bench.sh 300          # 5 minutos
#   WRITE_RATE=50 ./run-bench.sh
#   DRY_RUN=1 ./run-bench.sh    # só mostra o que seria executado
#
# Rode de dentro da WSL. O script precisa de docker (na WSL) e de wt.exe
# (Windows Terminal, alcançado pelo interop).

set -euo pipefail

DURACAO="${1:-1800}"
# rate baixo de propósito: a 200 req/s a escrita sozinha geraria ~760 mil
# planos em 30min (uns 3 GB de log) e afogaria os planos analíticos, que são
# poucos porque cada query TPC-H leva segundos.
WRITE_RATE="${WRITE_RATE:-20}"
DRY_RUN="${DRY_RUN:-0}"

PROJETO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERADOS="$PROJETO/benchbase/config/generated"
PAINEIS="/tmp/sindri-bench"
DISTRO="${WSL_DISTRO_NAME:-Ubuntu}"
IMAGEM="sindri/benchbase-postgres:latest"

msg() { printf '\033[1m==>\033[0m %s\n' "$*"; }
erro() { printf '\033[31merro:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checagens
[ -n "${WSL_DISTRO_NAME:-}" ] || erro "rode de dentro da WSL (os bind mounts relativos precisam resolver como caminho Linux)"
command -v docker >/dev/null || erro "docker não encontrado no PATH da WSL"

WT="$(command -v wt.exe 2>/dev/null || true)"
if [ -z "$WT" ]; then
  for c in /mnt/c/Users/*/AppData/Local/Microsoft/WindowsApps/wt.exe; do
    [ -e "$c" ] && WT="$c" && break
  done
fi
[ -n "$WT" ] || erro "wt.exe (Windows Terminal) não encontrado"

if ! docker exec sindri_postgres pg_isready -U sindri -d tpch -q 2>/dev/null; then
  msg "Postgres não está aceitando conexões; subindo com docker compose"
  ( cd "$PROJETO" && docker compose up -d )
  for _ in $(seq 1 60); do
    docker exec sindri_postgres pg_isready -U sindri -d tpch -q 2>/dev/null && break
    sleep 2
  done
  docker exec sindri_postgres pg_isready -U sindri -d tpch -q 2>/dev/null \
    || erro "Postgres não ficou pronto. Veja: docker compose logs postgres"
fi

if ! docker image inspect "$IMAGEM" >/dev/null 2>&1; then
  msg "Imagem do BenchBase ausente; construindo (demora alguns minutos na primeira vez)"
  ( cd "$PROJETO" && docker compose --profile bench build benchbase )
fi

# ------------------------------------------------------- configs com o tempo
# O <time> e o <rate> moram dentro do XML, não há flag de linha de comando
# para eles. Por isso as configs versionadas são copiadas com os valores
# trocados, em vez de editadas no lugar.
mkdir -p "$GERADOS" "$PAINEIS"
printf '%s\n' '*' '!.gitignore' > "$GERADOS/.gitignore"

sed "s|<time>[0-9]*</time>|<time>${DURACAO}</time>|" \
  "$PROJETO/benchbase/config/tpch_throughput.xml" > "$GERADOS/leitura.xml"

sed "s|<time>[0-9]*</time>|<time>${DURACAO}</time>|" "$PROJETO/benchbase/config/tpch_extra.xml" > "$GERADOS/extra.xml"

sed -e "s|<time>[0-9]*</time>|<time>${DURACAO}</time>|" \
    -e "s|<rate>[0-9]*</rate>|<rate>${WRITE_RATE}</rate>|" \
  "$PROJETO/benchbase/config/tpch_write.xml" > "$GERADOS/escrita.xml"

mkdir -p "$PROJETO/benchbase/results/l1" \
         "$PROJETO/benchbase/results/l2" \
         "$PROJETO/benchbase/results/w"

# --------------------------------------------------------- scripts de painel
# Cada painel roda um script próprio em vez de um comando embutido na linha do
# wt.exe: aspas aninhadas entre bash, wt.exe e wsl.exe são a forma mais fácil
# de quebrar isso silenciosamente.

carga() { # $1 = nome, $2 = -b, $3 = config, $4 = subpasta de resultados
  cat > "$PAINEIS/$1.sh" <<PANE_EOF
#!/usr/bin/env bash
cd "$PROJETO"
printf '\033]0;sindri: $1\007'
echo "== $1 :: $2 :: ${DURACAO}s =="
echo
docker compose --profile bench run --rm benchbase \
  -b $2 -c sindri-config/generated/$3 -d results/$4 \
  --create=false --load=false --execute=true
echo
echo "== $1 terminou (saida \$?) =="
read -r -p "Enter para fechar este painel..."
PANE_EOF
  chmod +x "$PAINEIS/$1.sh"
}

carga leitura1 tpch      leitura.xml l1
# O painel 2 roda a carga complementar (CTE, window, DISTINCT, UNION,
# EXCEPT, view, view materializada, procedure, funcao) em vez de repetir
# as mesmas 22 queries do painel 1 - duas instancias identicas geravam
# plano duplicado. Para voltar ao TPC-H puro nos dois, troque por:
#   carga leitura2 tpch leitura.xml l2
carga leitura2 templated extra.xml   l2
carga escrita  templated escrita.xml w

cat > "$PAINEIS/monitor.sql" <<'SQL_EOF'
SELECT relname AS tabela, n_tup_ins AS ins, n_tup_upd AS upd, n_tup_del AS del,
       n_dead_tup AS mortas, autovacuum_count AS vacuums
FROM pg_stat_user_tables WHERE relname IN ('orders','lineitem') ORDER BY relname;
SELECT application_name AS carga,
       count(*) FILTER (WHERE state = 'active') AS ativas,
       count(*) AS sessoes
FROM pg_stat_activity
WHERE application_name <> '' AND backend_type = 'client backend'
GROUP BY 1 ORDER BY 3 DESC;
SQL_EOF

cat > "$PAINEIS/monitor.sh" <<MON_EOF
#!/usr/bin/env bash
printf '\033]0;sindri: monitor\007'
# +45s de folga: as cargas terminam por volta de FIM, mas o encerramento do
# BenchBase (gravar resultados, fechar conexões) leva mais alguns segundos.
FIM_CARGA=\$(( \$(date +%s) + $DURACAO ))
FIM_LACO=\$(( FIM_CARGA + 45 ))
ANTERIOR=0
while [ \$(date +%s) -lt \$FIM_LACO ]; do
  AGORA=\$(date +%s)
  RESTA=\$(( FIM_CARGA - AGORA ))
  [ \$RESTA -lt 0 ] && RESTA=0
  BYTES=\$(du -sb "$PROJETO/logs" 2>/dev/null | cut -f1)
  MB=\$(( BYTES / 1024 / 1024 ))
  DELTA=\$(( (BYTES - ANTERIOR) / 1024 ))
  [ \$ANTERIOR -eq 0 ] && DELTA=0
  ANTERIOR=\$BYTES
  clear
  echo "===== sindri :: monitor ====="
  printf 'restam %02d:%02d de %s s\n' \$(( RESTA / 60 )) \$(( RESTA % 60 )) "$DURACAO"
  echo
  echo "logs/: \$(ls "$PROJETO"/logs/*.json 2>/dev/null | wc -l) arquivos, \${MB} MB (+\${DELTA} KB nos ultimos 5s)"
  echo
  docker exec -i sindri_postgres psql -U sindri -d tpch -P footer=off -f - < "$PAINEIS/monitor.sql" 2>/dev/null \
    || echo "(banco indisponivel)"
  echo
  echo "Ctrl+C encerra so o monitor; as cargas seguem nos outros paineis."
  sleep 5
done

echo
echo "===== corrida encerrada ====="
echo "planos gravados por carga:"
grep -h "ms  plan:" "$PROJETO"/logs/*.json 2>/dev/null | wc -l | xargs echo "  total:"
echo "  tamanho de logs/: \$(du -sh "$PROJETO/logs" | cut -f1)"
echo
read -r -p "Enter para fechar este painel..."
MON_EOF
chmod +x "$PAINEIS/monitor.sh"

# ------------------------------------------------------------------ resumo
msg "duração: ${DURACAO}s | rate de escrita: ${WRITE_RATE} req/s | distro: ${DISTRO}"
msg "painéis: leitura1 (TPC-H), leitura2 (complementar), escrita, monitor"
msg "logs em $PROJETO/logs — o log NÃO é apagado sozinho; limpe entre experimentos"

if [ "$DRY_RUN" = "1" ]; then
  msg "DRY_RUN=1, nada foi aberto. Scripts de painel em $PAINEIS:"
  ls -1 "$PAINEIS"
  exit 0
fi

# Layout 2x2. O ';' precisa chegar ao wt.exe como argumento, daí o escape.
"$WT" -w -1 new-tab --title "leitura1" wsl.exe -d "$DISTRO" -e bash "$PAINEIS/leitura1.sh" \
  \; split-pane -V --title "leitura2" wsl.exe -d "$DISTRO" -e bash "$PAINEIS/leitura2.sh" \
  \; split-pane -H --title "escrita"  wsl.exe -d "$DISTRO" -e bash "$PAINEIS/escrita.sh" \
  \; move-focus left \
  \; split-pane -H --title "monitor"  wsl.exe -d "$DISTRO" -e bash "$PAINEIS/monitor.sh"

msg "Windows Terminal aberto com os 4 painéis."
