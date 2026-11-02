#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A impressão digital de uma sessão de medida.

Duas sessões só podem entrar na mesma conta se mediram a mesma coisa nas
mesmas condições. Medido: a mesma query sem índice variou 16% (mediana) entre
linhas de base com os padrões do Postgres e 32% com o PGTune. Uma validação
feita em cada condição, somadas, inventariam variabilidade, ou ganho.

As GUCs são necessárias e não bastam. O que muda o tempo de uma query de uma
sessão para outra, sem mudar o SQL:

    postgres     versão e build
    extensoes    quais e em que versão (pg_trgm muda o que é indexável)
    gucs         as efetivas da sessão, e não só as do arquivo
    dados        o banco, o tamanho das tabelas e os índices que JÁ existem:
                 a linha de base é "sem os índices do experimento", e isso só
                 é a mesma coisa se os índices existentes forem os mesmos
    host         CPU, memória, sistema e kernel da máquina da bancada
    container    os limites de CPU e memória do container, quando o docker
                 responde
    concorrencia quem mais estava ativo no banco observado
    aquecimento  quantas repetições, qual descartada, em que ordem os blocos

Grava-se tudo, em valores brutos, para auditar. A comparação usa só o
subconjunto que precisa ser igual (``COMPARAVEL``), resumido num hash para
conferir rápido e comparado campo a campo para dizer o que mudou. A
concorrência e os contadores de escrita mudam de propósito entre sessões e não
entram no hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from typing import Any, Dict, List, Optional

# As que mudam plano ou tempo e precisam ser iguais entre sessões comparadas.
GUCS_COMPARAVEIS = (
    "server_version_num", "shared_buffers", "work_mem", "maintenance_work_mem",
    "effective_cache_size", "max_parallel_workers_per_gather",
    "max_parallel_workers", "max_worker_processes", "random_page_cost",
    "seq_page_cost", "effective_io_concurrency", "jit",
    "default_statistics_target", "huge_pages", "statement_timeout",
    "plan_cache_mode", "enable_seqscan", "enable_indexscan",
    "enable_bitmapscan", "enable_hashjoin", "enable_mergejoin",
    "enable_nestloop",
)

# O container do banco observado, para ler os limites dele com docker inspect.
CONTAINER = os.environ.get("BROKK_CONTAINER_OBSERVADO", "sindri_postgres")

# Os índices do experimento não fazem parte do "estado de base".
PREFIXO_EXPERIMENTO = "brokk_exp_"

SQL_GUCS = "SELECT name, setting, source FROM pg_settings ORDER BY name"
SQL_EXTENSOES = "SELECT extname, extversion FROM pg_extension ORDER BY extname"
SQL_TABELAS = """
SELECT c.relname, c.reltuples::bigint, c.relpages
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'm')
 ORDER BY c.relname
"""
SQL_INDICES = """
SELECT indexname, indexdef FROM pg_indexes
 WHERE schemaname = 'public' AND left(indexname, %s) <> %s
 ORDER BY indexname
"""
SQL_ESCRITA = """
SELECT relname, n_tup_ins, n_tup_upd, n_tup_hot_upd, n_tup_del,
       n_live_tup, n_dead_tup
  FROM pg_stat_user_tables WHERE schemaname = 'public' ORDER BY relname
"""
SQL_ATIVOS = """
SELECT coalesce(nullif(application_name, ''), '(sem nome)'), state, count(*)
  FROM pg_stat_activity
 WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
 GROUP BY 1, 2 ORDER BY 1, 2
"""


def _significativos(valor: float, digitos: int = 2) -> float:
    """O número com `digitos` algarismos significativos.

    A contagem de linhas muda um pouco a cada ANALYZE e com a carga de
    escrita; o que precisa ser igual entre sessões é a ordem de grandeza do
    conjunto de dados, não o último dígito.
    """
    if not valor:
        return 0.0
    from math import floor, log10
    casas = digitos - 1 - int(floor(log10(abs(valor))))
    return float(round(valor, casas))


def _memoria_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for linha in f:
                if linha.startswith("MemTotal:"):
                    return int(linha.split()[1]) // 1024
    except OSError:
        pass
    return None


def _container(nome: str = CONTAINER) -> Dict[str, Any]:
    """Limites do container pelo docker inspect. Melhor esforço: sem docker,
    diz que não sabe, e não inventa."""
    try:
        saida = subprocess.run(
            ["docker", "inspect", "--format",
             "{{json .HostConfig}}|{{.Config.Image}}|{{.Image}}", nome],
            capture_output=True, text=True, timeout=10, check=True).stdout
        host_cfg, imagem, imagem_id = saida.strip().split("|", 2)
        cfg = json.loads(host_cfg)
        return {"disponivel": True, "nome": nome, "imagem": imagem,
                "imagem_id": imagem_id,
                "nano_cpus": cfg.get("NanoCpus"), "cpu_quota": cfg.get("CpuQuota"),
                "cpuset": cfg.get("CpusetCpus"), "memoria": cfg.get("Memory"),
                "shm_size": cfg.get("ShmSize")}
    except (OSError, subprocess.SubprocessError, ValueError) as erro:
        return {"disponivel": False, "nome": nome, "motivo": str(erro)[:200]}


def coletar(conn_observado, aquecimento: Optional[Dict[str, Any]] = None,
            container: bool = True) -> Dict[str, Any]:
    """Lê a impressão digital. Rode depois de Bancada.preparar_sessao(): as
    consultas ao catálogo não vão para o log, e as GUCs lidas são as da
    sessão que vai medir."""
    with conn_observado.cursor() as cur:
        cur.execute("SELECT version(), current_database(), "
                    "       pg_database_size(current_database())")
        versao, banco, tamanho = cur.fetchone()
        cur.execute(SQL_GUCS)
        gucs = cur.fetchall()
        cur.execute(SQL_EXTENSOES)
        extensoes = dict(cur.fetchall())
        cur.execute(SQL_TABELAS)
        tabelas = {t: {"linhas": int(n), "paginas": int(p)}
                   for t, n, p in cur.fetchall()}
        cur.execute(SQL_INDICES, (len(PREFIXO_EXPERIMENTO), PREFIXO_EXPERIMENTO))
        indices = dict(cur.fetchall())
        cur.execute(SQL_ESCRITA)
        escrita = {r[0]: {"ins": r[1], "upd": r[2], "hot_upd": r[3],
                          "del": r[4], "vivas": r[5], "mortas": r[6]}
                   for r in cur.fetchall()}
        cur.execute(SQL_ATIVOS)
        ativos = [{"app": a, "estado": e, "sessoes": n} for a, e, n in cur.fetchall()]
    conn_observado.commit()

    efetivas = {nome: valor for nome, valor, origem in gucs
                if origem not in ("default", "override")}
    comparaveis = {nome: valor for nome, valor, _ in gucs
                   if nome in GUCS_COMPARAVEIS}
    uname = platform.uname()
    amb = {
        "postgres": {"versao": versao},
        "extensoes": extensoes,
        "gucs": {"comparaveis": comparaveis, "efetivas": efetivas},
        "dados": {"banco": banco, "tamanho_bytes": int(tamanho),
                  "tabelas": tabelas, "indices_existentes": indices},
        "escrita_observada": escrita,
        "host": {"sistema": uname.system, "kernel": uname.release,
                 "build_kernel": uname.version, "maquina": uname.machine,
                 "cpus": os.cpu_count(), "memoria_mb": _memoria_mb(),
                 "python": platform.python_version()},
        "container": _container() if container else {"disponivel": False,
                                                      "motivo": "não consultado"},
        "concorrencia": {"ativos_no_inicio": ativos},
        "aquecimento": aquecimento or {},
    }
    amb["hash"] = hash_comparavel(amb)
    return amb


def ativos(conn_observado) -> List[Dict[str, Any]]:
    """Quem mais está conectado agora. A validação grava isto no começo e no
    fim: a checagem de ociosidade só olha o começo."""
    with conn_observado.cursor() as cur:
        cur.execute(SQL_ATIVOS)
        linhas = cur.fetchall()
    conn_observado.commit()
    return [{"app": a, "estado": e, "sessoes": n} for a, e, n in linhas]


def comparavel(amb: Dict[str, Any]) -> Dict[str, Any]:
    """O subconjunto que precisa ser igual entre sessões comparadas."""
    dados = amb.get("dados", {})
    host = amb.get("host", {})
    cont = amb.get("container", {})
    mem = host.get("memoria_mb")
    return {
        "postgres.versao": amb.get("postgres", {}).get("versao"),
        "extensoes": amb.get("extensoes"),
        "gucs": amb.get("gucs", {}).get("comparaveis"),
        "dados.banco": dados.get("banco"),
        "dados.tabelas": {t: _significativos(v.get("linhas", 0))
                          for t, v in (dados.get("tabelas") or {}).items()},
        "dados.indices_existentes": dados.get("indices_existentes"),
        "host.cpus": host.get("cpus"),
        # Em GB com uma casa: o MemTotal da VM oscila alguns MB entre boots.
        "host.memoria_gb": None if mem is None else round(mem / 1024, 1),
        "host.kernel": host.get("kernel"),
        "container": ({k: cont.get(k) for k in ("imagem_id", "nano_cpus",
                                                "cpu_quota", "cpuset", "memoria")}
                      if cont.get("disponivel") else None),
    }


def hash_comparavel(amb: Dict[str, Any]) -> str:
    texto = json.dumps(comparavel(amb), sort_keys=True, ensure_ascii=True,
                       default=str)
    return hashlib.sha1(texto.encode("ascii")).hexdigest()[:16]


def diferencas(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> List[str]:
    """O que difere entre dois ambientes, campo a campo. [] = comparáveis.

    Um lado desconhecido (sessão anterior ao registro) não é diferença: é
    falta de informação, e quem chama decide o que fazer com ela.
    """
    if not a or not b:
        return []
    ca, cb = comparavel(a), comparavel(b)
    saida = []
    for campo in sorted(set(ca) | set(cb)):
        va, vb = ca.get(campo), cb.get(campo)
        if va == vb:
            continue
        if isinstance(va, dict) and isinstance(vb, dict):
            for k in sorted(set(va) | set(vb)):
                if va.get(k) != vb.get(k):
                    saida.append(f"{campo}.{k}: {va.get(k)} ≠ {vb.get(k)}")
        else:
            saida.append(f"{campo}: {va} ≠ {vb}")
    return saida
