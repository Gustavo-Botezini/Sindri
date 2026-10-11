#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""O CLI do recomendador.

    ./.venv/bin/python -m src.index_provider <subcomando> [opções]

Os subcomandos do modo de clusters são etapas encadeadas, e cada uma grava o
resultado no banco: dá para parar entre duas, olhar o que saiu, e retomar. A
`--rodada` é o que costura tudo.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

from . import (banco, bancada, candidatos, corpus, dossie, graficos, grupos,
               matriz, saida, vetores)
from .banco import como_json

SQL_LISTAR_RODADAS = """
SELECT r.id, r.criado_em, r.database_name, r.normalizacao,
       r.queries, r.clusters, r.ruido,
       r.parametros ->> 'n_neighbors'      AS vizinhos,
       r.parametros ->> 'n_components'     AS dimensoes,
       r.parametros ->> 'min_dist'         AS min_dist,
       r.parametros ->> 'min_cluster_size' AS tam_min,
       coalesce(r.parametros ->> 'cluster_selection_method', 'eom') AS selecao,
       (SELECT count(*) FROM monitoring.experimento e
         WHERE e.rodada_id = r.id)                        AS experimentos,
       (SELECT count(*) FROM monitoring.experimento e
         WHERE e.rodada_id = r.id AND e.estado = 'medido') AS medidos
  FROM monitoring.rodada_cluster r
 ORDER BY r.id DESC
 LIMIT 20
"""

SQL_GRAVAR_EXPERIMENTO = """
INSERT INTO monitoring.experimento
       (rodada_id, cluster_id, query_origem, origem, ddl, relacao,
        justificativa, candidatos, estado, erro)
VALUES (%s, %s, %s, 'claude', %s, %s, %s, %s, %s, %s)
RETURNING id
"""

SQL_EXPERIMENTOS = """
SELECT id, cluster_id, ddl, relacao, estado
  FROM monitoring.experimento
 WHERE rodada_id = %s
   AND (%s::int IS NULL OR cluster_id = %s)
 ORDER BY cluster_id
"""


# --------------------------------------------------------------------------
# Subcomandos
# --------------------------------------------------------------------------

def cmd_esquema(args) -> int:
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn, verboso=True)
    return 0


def cmd_dossie(args) -> int:
    print(f"montando o dossiê ({args.banco}, top {args.top})...")
    d = dossie.montar(args.banco, top=args.top)
    print(f"  {dossie.resumir(d)}")
    if args.salvar_dossie:
        args.salvar_dossie.write_text(como_json(d), encoding="utf-8")
        print(f"  dossiê em {args.salvar_dossie}")
    else:
        print(como_json(d))
    return 0


def cmd_recomendar(args) -> int:
    from . import modelo

    print(f"montando o dossiê ({args.banco}, top {args.top})...")
    d = dossie.montar(args.banco, top=args.top)
    print(f"  {dossie.resumir(d)}")
    if args.salvar_dossie:
        args.salvar_dossie.write_text(como_json(d), encoding="utf-8")
        print(f"  dossiê em {args.salvar_dossie}")

    print(f"analisando com {modelo.MODELO}...")
    resposta = modelo.analisar(d)
    recs = resposta.get("recomendacoes", [])
    print(f"  {len(recs)} recomendação(ões)")

    if args.json_saida:
        args.json_saida.write_text(como_json(resposta), encoding="utf-8")
        print(f"  JSON em {args.json_saida}")

    linhas = saida.em_linhas(resposta)
    if args.csv:
        saida.gravar_csv(linhas, args.csv)
    if args.planilha:
        contexto = [
            ["Gerado em", d["gerado_em"]],
            ["Banco analisado", d["banco"]],
            ["Modelo", modelo.MODELO],
            ["Queries no dossiê", str(len(d["workload"]))],
            ["Índices existentes", str(len(d["indices"]))],
            ["Tabelas", str(len(d["tabelas"]))],
            ["", ""],
            ["Observações do modelo", resposta.get("observacoes", "")],
            ["", ""],
            ["Aviso", "Espaço e ganho são ESTIMATIVAS. Confira a coluna "
                      "'Base da estimativa' e valide com EXPLAIN antes de "
                      "aplicar. Para medir de verdade, use o modo de "
                      "clusters (vetorizar/agrupar/candidatar/medir)."],
        ]
        url = saida.gravar_planilha(linhas, contexto, args.credencial,
                                    args.nome_planilha, args.compartilhar)
        print(f"  planilha em {url}")

    if not (args.csv or args.planilha or args.json_saida):
        # Sem destino escolhido, mostra no terminal em vez de perder o trabalho.
        for linha in linhas:
            print("  " + " | ".join(c[:40] for c in linha[:6]))
        print(f"\n{resposta.get('observacoes', '')}")
    return 0


def cmd_vetorizar(args) -> int:
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        qs = _corpus(conn, args)
        print(f"vetorizando {len(qs)} queries ({args.normalizacao})...")
        vetores.gerar(conn, qs, modelo=args.modelo_embedding,
                      normalizacao=args.normalizacao, refazer=args.refazer,
                      verboso=not args.silencioso)
    return 0


def cmd_conferir(args) -> int:
    """Descobre quais queries do corpus a bancada consegue reexecutar.

    Roda antes de `agrupar`: uma query que a bancada nunca vai medir não
    deveria ocupar vaga num cluster.
    """
    conn = banco.conectar(args.dsn)
    obs = banco.conectar(banco.DSN_OBSERVADO, "banco observado",
                         app=bancada.NOME_APP)
    with conn, obs:
        banco.aplicar_esquema(conn)
        qs = corpus.carregar(conn, args.banco,
                             normalizacao=args.normalizacao,
                             minimo_execucoes=args.minimo_execucoes,
                             incluir_catalogo=args.incluir_catalogo,
                             incluir_irreplicaveis=True)
        corpus.anexar_parametros(conn, qs)
        print(f"conferindo {len(qs)} queries (EXPLAIN sem ANALYZE, "
              f"nada é executado)...")
        b = bancada.Bancada(obs, conn, verboso=not args.silencioso)
        bancada.conferir_replay(b, qs, verboso=not args.silencioso)
    return 0


def cmd_agrupar(args) -> int:
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        if args.listar:
            return _listar_rodadas(conn)

        qs = _corpus(conn, args)
        refs, matriz = vetores.carregar_vetores(
            conn, [q.ref for q in qs], args.modelo_embedding, args.normalizacao)
        if len(refs) != len(qs):
            faltam = len(qs) - len(refs)
            print(f"  {faltam} queries sem vetor — rode vetorizar antes",
                  file=sys.stderr)
            return 2

        par = grupos.Parametros(
            n_neighbors=args.vizinhos,
            min_dist=args.min_dist,
            n_components=args.dimensoes,
            min_cluster_size=args.tamanho_minimo,
            min_samples=args.amostras_minimas,
            cluster_selection_method=args.selecao,
            semente=args.semente,
        )
        print(f"agrupando {len(refs)} queries "
              f"(n_neighbors={par.n_neighbors}, min_dist={par.min_dist}, "
              f"n_components={par.n_components}, "
              f"min_cluster_size={par.min_cluster_size}, "
              f"seleção={par.cluster_selection_method}, "
              f"semente={par.semente})...")
        res = grupos.agrupar(refs, matriz, par, verboso=not args.silencioso)

        rodada_id = grupos.gravar(conn, args.banco, args.modelo_embedding,
                                  args.normalizacao, res, par)
        print(f"  = rodada {rodada_id}: {res.clusters} clusters, "
              f"{res.ruido} em ruído")
        grupos.mostrar(res, qs)

        # A conferência: vetores quase idênticos que a projeção separou.
        rasgados = grupos.pares_separados(refs, matriz, res.rotulos)
        if rasgados:
            print("\n  AVISO: pares quase idênticos em clusters diferentes —")
            print("  a projeção rasgou o que estava junto; reveja os parâmetros.")
            for a, b, s in rasgados[:5]:
                print(f"      ref={a} e ref={b}, cosseno {s:.4f}")
        else:
            print("\n  conferência: nenhum par quase idêntico foi separado")
    return 0


def cmd_candidatar(args) -> int:
    from psycopg.types.json import Jsonb
    from . import modelo

    conn = banco.conectar(args.dsn)
    obs = banco.conectar(banco.DSN_OBSERVADO, "banco observado")
    with conn, obs:
        banco.aplicar_esquema(conn)
        meta, agrupados = grupos.carregar_rodada(conn, args.rodada)
        alvos = {c: r for c, r in agrupados.items() if c >= 0}
        if args.cluster is not None:
            alvos = {c: r for c, r in alvos.items() if c == args.cluster}
        if not alvos:
            print("nenhum cluster para trabalhar (só ruído?)", file=sys.stderr)
            return 2

        for cid, refs in alvos.items():
            print(f"\ncluster {cid}: {len(refs)} queries {refs}")
            props, redundantes = candidatos.propor(
                conn, obs, refs, limite=args.candidatos)
            extra = (f", {redundantes} descartados por já existirem"
                     if redundantes else "")
            print(f"  {len(props)} candidatos da heurística{extra}")
            for c in props[:6]:
                print(f"      {c.familia:16s} {c.ms_total:>10,.0f} ms  "
                      f"{c.relacao}({', '.join(c.colunas)})")
            if not props:
                print("  nenhum candidato — cluster sem evidência de índice")
                continue

            d = dossie.montar(meta["database_name"], refs=refs,
                              conn_brokk=conn, conn_observado=obs)

            if args.dossie:
                corpo = {"cluster": cid, "queries": refs,
                         "candidatos": [c.como_dict() for c in props], **d}
                if args.salvar_dossie:
                    alvo = args.salvar_dossie.with_suffix(f".c{cid}.json")
                    alvo.write_text(como_json(corpo), encoding="utf-8")
                    print(f"  dossiê em {alvo}")
                else:
                    print(como_json(corpo))
                continue

            escolha = modelo.escolher(cid, refs, d, props,
                                      verboso=not args.silencioso)
            if not escolha.get("escolhido"):
                print(f"  o modelo recusou: {escolha.get('justificativa', '')[:150]}")
                estado, erro = "recusado", escolha.get("justificativa", "")[:400]
                ddl, relacao, origem_ref = "", None, None
            else:
                estado, erro = "proposto", None
                ddl = escolha["ddl"]
                relacao = escolha.get("relacao")
                origem_ref = escolha.get("query_origem") or None
                if origem_ref not in refs:
                    origem_ref = None
                print(f"  escolhido: {ddl}")
                print(f"      espera melhorar {escolha.get('queries_beneficiadas')}")

            with conn.cursor() as cur:
                cur.execute(SQL_GRAVAR_EXPERIMENTO, (
                    args.rodada, cid, origem_ref, ddl, relacao,
                    escolha.get("justificativa"),
                    Jsonb([c.como_dict() for c in props]), estado, erro))
                exp_id = cur.fetchone()[0]
            conn.commit()
            print(f"  experimento {exp_id} gravado ({estado})")
    return 0


def cmd_medir(args) -> int:
    conn = banco.conectar(args.dsn)
    # O application_name entra no handshake, não num SET depois: é o que
    # deixa identificável até o statement que desliga o log.
    obs = banco.conectar(banco.DSN_OBSERVADO, "banco observado",
                         app=bancada.NOME_APP)
    with conn, obs:
        banco.aplicar_esquema(conn)
        meta, agrupados = grupos.carregar_rodada(conn, args.rodada)

        with conn.cursor() as cur:
            cur.execute(SQL_EXPERIMENTOS,
                        (args.rodada, args.cluster, args.cluster))
            experimentos = cur.fetchall()

        pendentes = [e for e in experimentos
                     if e[2] and (args.refazer or e[4] == "proposto")]
        if not pendentes:
            print("nada para medir. Estados encontrados: "
                  + ", ".join(f"c{e[1]}={e[4]}" for e in experimentos)
                  + "\nUse --refazer para remedir o que já foi medido.")
            return 0

        qs = {q.ref: q for q in _corpus(conn, args)}
        faltando = corpus.anexar_parametros(conn, list(qs.values()))
        if faltando:
            print(f"  AVISO: {faltando} queries parametrizadas sem valores "
                  f"gravados; elas vão falhar no replay")

        b = bancada.Bancada(obs, conn, repeticoes=args.repeticoes,
                            timeout_ms=args.timeout * 1000,
                            verboso=not args.silencioso)

        for exp_id, cid, ddl, relacao, _estado in pendentes:
            refs = agrupados.get(cid, [])
            do_cluster = [qs[r] for r in refs if r in qs]
            if not do_cluster:
                print(f"cluster {cid}: nenhuma query replicável, pulando")
                continue
            print(f"\nexperimento {exp_id} (cluster {cid}, "
                  f"{len(do_cluster)} queries)")
            inicio = time.perf_counter()
            resumo = b.rodar(exp_id, ddl, relacao, do_cluster,
                             forcar=args.forcar)
            print(f"  = {resumo['estado']} em "
                  f"{time.perf_counter() - inicio:.1f}s")

        print()
        saida.mostrar_veredito(conn, args.rodada)
    return 0


def cmd_exportar(args) -> int:
    """O agrupamento em arquivo, para conferir fora do banco.

    Existe porque `agrupar` grava em tabela, e tabela não se lê de relance.
    O CSV abre em planilha; o JSON carrega os parâmetros da rodada junto dos
    pontos, que é o que torna o resultado reproduzível por outra pessoa.
    """
    conn = banco.conectar(args.dsn)
    with conn:
        meta, agrupados = grupos.carregar_rodada(conn, args.rodada)
        registros = saida.pontos(conn, args.rodada)

        # O texto que de fato virou vetor, e não o SQL como está no corpus.
        for r in registros:
            r["texto_embedado"] = corpus.normalizar(
                r.pop("query_text"), meta["normalizacao"])

        print(f"rodada {args.rodada}: {meta['queries']} queries, "
              f"{meta['clusters']} clusters, {meta['ruido']} em ruído")
        print(f"  {meta['modelo']}, normalização {meta['normalizacao']}")
        print(f"  parâmetros {meta['parametros']}")

        if args.csv:
            saida.gravar_csv(saida.pontos_em_linhas(registros), args.csv)
        if args.json_saida:
            corpo = {
                "rodada": {k: v for k, v in meta.items()},
                "clusters": {str(c): refs for c, refs in agrupados.items()},
                "pontos": registros,
            }
            args.json_saida.write_text(como_json(corpo), encoding="utf-8")
            print(f"  JSON em {args.json_saida}")

        if not (args.csv or args.json_saida):
            # Sem destino, mostra no terminal em vez de não fazer nada.
            for linha in saida.pontos_em_linhas(registros):
                print("  " + " | ".join(c[:34] for c in linha[:6]))
    return 0


def cmd_grafico(args) -> int:
    """As figuras dos clusters, em PNG."""
    from . import graficos

    conn = banco.conectar(args.dsn)
    with conn:
        meta, _ = grupos.carregar_rodada(conn, args.rodada)
        pontos = graficos.carregar_pontos(conn, args.rodada)
        rotulo = graficos.rotulos_tpch(pontos)
        casados = sum(1 for v in rotulo.values() if not v.startswith("#"))
        print(f"rodada {args.rodada}: {len(pontos)} queries, "
              f"{meta['clusters']} clusters, {meta['ruido']} em ruído")
        print(f"  {casados} de {len(pontos)} casadas com queries/q*.sql")

        alvo = args.saida or Path(f"clusters-rodada{args.rodada}.png")
        graficos.desenhar_clusters(
            pontos, rotulo, alvo,
            f"Clusters da rodada {args.rodada} — {meta['database_name']}")
        print(f"  figura em {alvo}")

        if args.projecoes:
            alvo2 = alvo.with_name(alvo.stem + "-projecoes" + alvo.suffix)
            graficos.desenhar_projecoes(
                conn, args.rodada, pontos, rotulo, alvo2,
                meta["modelo"], meta["normalizacao"])
            print(f"  figura em {alvo2}")
    return 0


def cmd_relatar(args) -> int:
    conn = banco.conectar(args.dsn)
    with conn:
        meta, agrupados = grupos.carregar_rodada(conn, args.rodada)
        print(f"rodada {args.rodada}: {meta['queries']} queries, "
              f"{meta['clusters']} clusters, {meta['ruido']} em ruído")
        print(f"  {meta['modelo']}, normalização {meta['normalizacao']}, "
              f"parâmetros {meta['parametros']}")
        print()
        saida.mostrar_veredito(conn, args.rodada)

        linhas = saida.resultado_em_linhas(conn, args.rodada)
        if args.csv:
            saida.gravar_csv(linhas, args.csv)
        if args.planilha:
            contexto = [
                ["Rodada", str(args.rodada)],
                ["Criada em", str(meta["criado_em"])],
                ["Banco", meta["database_name"]],
                ["Modelo de embedding", meta["modelo"]],
                ["Normalização", meta["normalizacao"]],
                ["Parâmetros", str(meta["parametros"])],
                ["Queries", str(meta["queries"])],
                ["Clusters", str(meta["clusters"])],
                ["Em ruído", str(meta["ruido"])],
                ["", ""],
                ["O que estas colunas são", "Tempo MEDIDO com EXPLAIN "
                 "(ANALYZE) antes e depois de criar o índice de verdade, "
                 "mediana das repetições, descartada a primeira (cache "
                 "frio). O índice foi derrubado ao fim de cada experimento."],
                ["A coluna que importa", "'Usou o índice'. Ganho numa query "
                 "cujo plano não cita o índice novo é variação de cache, "
                 "não efeito do índice."],
            ]
            url = saida.gravar_planilha(linhas, contexto, args.credencial,
                                        args.nome_planilha, args.compartilhar)
            print(f"  planilha em {url}")
    return 0


def cmd_matriz(args) -> int:
    conn = banco.conectar(args.dsn)
    obs = banco.conectar(banco.DSN_OBSERVADO, "banco observado",
                         app=bancada.NOME_APP)
    with conn, obs:
        banco.aplicar_esquema(conn)
        qs = _corpus(conn, args)
        faltando = corpus.anexar_parametros(conn, qs)
        if faltando:
            print(f"  AVISO: {faltando} queries parametrizadas sem valores "
                  f"gravados; elas vão falhar no replay")
        b = bancada.Bancada(obs, conn, repeticoes=args.repeticoes,
                            timeout_ms=args.timeout * 1000,
                            verboso=not args.silencioso)
        inicio = time.perf_counter()
        sessao = matriz.rodar(conn, b, args.rodadas, qs, forcar=args.forcar)
        print(f"\nsessão {sessao} concluída em "
              f"{(time.perf_counter() - inicio) / 60:.1f} min. Para avaliar:\n"
              f"  python -m src.index_provider comparar --sessao {sessao} "
              f"--rodadas {','.join(map(str, args.rodadas))}")
    return 0


def cmd_comparar(args) -> int:
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        sessao = args.sessao
        if sessao is None:
            with conn.cursor() as cur:
                cur.execute("SELECT max(id) FROM monitoring.matriz_sessao "
                            " WHERE concluida_em IS NOT NULL")
                sessao = cur.fetchone()[0]
            if sessao is None:
                print("nenhuma matriz concluída. Rode antes: matriz --rodadas ...")
                return 2
        res = matriz.comparar(conn, sessao, args.rodadas, args.limiar)

        with conn.cursor() as cur:
            cur.execute("SELECT query_ref, query_text FROM monitoring.query_resumo")
            textos = [{"query_ref": r, "query_text": t} for r, t in cur.fetchall()]
        rotulo = graficos.rotulos_tpch(textos)
        res["rotulos"] = {str(r): rotulo[r] for r in sorted(rotulo)}

        def nomes(refs):
            return " ".join(rotulo.get(r, f"#{r}") for r in refs) or "-"

        print(f"sessão {sessao}, beneficiada = usou o índice e caiu "
              f">= {args.limiar:g}%")
        for rr in res["rodadas"]:
            s = rr["resumo"]
            print(f"\n=== rodada {rr['rodada']}  ({rr['normalizacao']}, "
                  f"{rr['modelo']}, min {rr['min_cluster_size']})")
            print(f"    cobertura média {s['cobertura_media']}  precisão média "
                  f"{s['precisao_media']}  média geral {s['media_geral_pct']}%  "
                  f"fora da origem {s['beneficiadas_fora_da_origem']}  "
                  f"perdidas fora do cluster {s['beneficiadas_fora_do_cluster']}")
            for c in rr["clusters"]:
                print(f"  cluster {c['cluster']}: {c['indice']}")
                print(f"      beneficia no corpus: {nomes(c['beneficiadas_no_corpus'])}"
                      f"  | no cluster: {nomes(c['beneficiadas_no_cluster'])}"
                      f"  | cobertura {c['cobertura']} precisão {c['precisao']}")

        if args.json:
            args.json.write_text(banco.como_json(res), encoding="utf-8")
            print(f"\n  JSON em {args.json}")
    return 0


def cmd_limpar(args) -> int:
    obs = banco.conectar(banco.DSN_OBSERVADO, "banco observado")
    with obs:
        bancada.limpar(obs)
    return 0


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------

def _corpus(conn, args) -> List[corpus.Query]:
    return corpus.carregar(conn, args.banco,
                           normalizacao=args.normalizacao,
                           minimo_execucoes=args.minimo_execucoes,
                           incluir_catalogo=args.incluir_catalogo,
                           incluir_irreplicaveis=args.incluir_irreplicaveis)


def _listar_rodadas(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(SQL_LISTAR_RODADAS)
        linhas = banco.linhas_como_dicts(cur)
    if not linhas:
        print("nenhuma rodada ainda. Rode: agrupar")
        return 0
    print(f"{'id':>4}  {'quando':<20} {'banco':<10} {'qs':>4} {'cl':>3} "
          f"{'ruído':>5} {'viz':>4} {'dim':>4} {'dist':>5} {'min':>4} "
          f"{'sel':>5} {'exps':>5} {'medidos':>7}")
    for l in linhas:
        print(f"{l['id']:>4}  {str(l['criado_em'])[:19]:<20} "
              f"{l['database_name']:<10} {l['queries']:>4} {l['clusters']:>3} "
              f"{l['ruido']:>5} {l['vizinhos']:>4} {l['dimensoes']:>4} "
              f"{l['min_dist']:>5} {l['tam_min']:>4} {l['selecao']:>5} "
              f"{l['experimentos']:>5} {l['medidos']:>7}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    # Fora do terminal, o print() do Python usa buffer de bloco: uma corrida
    # de `candidatar` ou `medir` redirecionada para arquivo fica 15 minutos
    # sem mostrar nada e parece travada. Em job longo, ver o progresso vale
    # mais que o buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover
        pass

    ap = argparse.ArgumentParser(
        prog="python -m src.index_provider",
        description="Recomenda e mede índices a partir do warehouse do BrokkPG.")
    ap.add_argument("--dsn", default=banco.DSN_BROKK,
                    help="banco de monitoramento (padrão: brokk:5433)")
    ap.add_argument("--banco", default="tpch",
                    help="qual banco do corpus analisar (padrão: tpch)")
    ap.add_argument("--silencioso", action="store_true")

    sub = ap.add_subparsers(dest="comando", required=True)

    def com_corpus(p):
        p.add_argument("--normalizacao", default=corpus.NORMALIZACAO_PADRAO,
                       choices=corpus.NORMALIZACOES)
        p.add_argument("--minimo-execucoes", type=int,
                       default=corpus.MINIMO_EXECUCOES,
                       help="abaixo disto a query é acidente, não carga")
        p.add_argument("--incluir-catalogo", action="store_true",
                       help="não descartar as consultas a pg_* do pgAdmin/JDBC")
        p.add_argument("--incluir-irreplicaveis", action="store_true",
                       help="não descartar o que `conferir` reprovou")
        p.add_argument("--modelo-embedding", default=vetores.MODELO_EMBEDDING)
        return p

    def com_planilha(p):
        p.add_argument("--csv", type=Path, metavar="ARQ")
        p.add_argument("--planilha", action="store_true")
        p.add_argument("--credencial", default=banco.CREDENCIAL_GOOGLE)
        p.add_argument("--nome-planilha", default=banco.PLANILHA_NOME)
        p.add_argument("--compartilhar", metavar="EMAIL",
                       help="e-mail que recebe acesso de escrita à planilha")
        return p

    p = sub.add_parser("esquema", help="aplica esquema.sql (idempotente)")
    p.set_defaults(func=cmd_esquema)

    p = sub.add_parser("dossie", help="monta e imprime o dossiê, sem API")
    p.add_argument("--top", type=int, default=dossie.TOP_QUERIES)
    p.add_argument("--salvar-dossie", type=Path, metavar="ARQ")
    p.set_defaults(func=cmd_dossie)

    p = sub.add_parser("recomendar", help="dossiê global -> Claude -> CSV/Sheets")
    p.add_argument("--top", type=int, default=dossie.TOP_QUERIES)
    p.add_argument("--salvar-dossie", type=Path, metavar="ARQ")
    p.add_argument("--json", type=Path, metavar="ARQ", dest="json_saida",
                   help="grava a resposta crua do modelo")
    com_planilha(p)
    p.set_defaults(func=cmd_recomendar)

    p = com_corpus(sub.add_parser("vetorizar", help="gera os embeddings"))
    p.add_argument("--refazer", action="store_true",
                   help="ignora o cache e reembeda tudo")
    p.set_defaults(func=cmd_vetorizar)

    p = com_corpus(sub.add_parser(
        "conferir", help="quais queries a bancada consegue reexecutar"))
    p.set_defaults(func=cmd_conferir)

    p = com_corpus(sub.add_parser("agrupar", help="UMAP + HDBSCAN"))
    p.add_argument("--listar", action="store_true", help="lista as rodadas")
    p.add_argument("--vizinhos", type=int, default=5, help="UMAP n_neighbors")
    p.add_argument("--dimensoes", type=int, default=5, help="UMAP n_components")
    p.add_argument("--min-dist", type=float, default=0.0, help="UMAP min_dist")
    p.add_argument("--tamanho-minimo", type=int, default=3,
                   help="HDBSCAN min_cluster_size")
    p.add_argument("--amostras-minimas", type=int, default=2,
                   help="HDBSCAN min_samples")
    p.add_argument("--selecao", choices=("eom", "leaf"), default="eom",
                   help="HDBSCAN cluster_selection_method")
    p.add_argument("--semente", type=int, default=42)
    p.set_defaults(func=cmd_agrupar)

    p = sub.add_parser("candidatar",
                       help="heurística propõe, Claude escolhe um por cluster")
    p.add_argument("--rodada", type=int, required=True)
    p.add_argument("--cluster", type=int, help="só este cluster")
    p.add_argument("--candidatos", type=int, default=12,
                   help="quantos candidatos oferecer por cluster")
    p.add_argument("--dossie", action="store_true",
                   help="só mostra o que seria enviado, sem chamar a API")
    p.add_argument("--salvar-dossie", type=Path, metavar="ARQ")
    p.set_defaults(func=cmd_candidatar)

    p = com_corpus(sub.add_parser(
        "medir", help="cria o índice, remede o cluster, derruba o índice"))
    p.add_argument("--rodada", type=int, required=True)
    p.add_argument("--cluster", type=int, help="só este cluster")
    p.add_argument("--repeticoes", type=int, default=bancada.REPETICOES,
                   help="a primeira é descartada (cache frio)")
    p.add_argument("--timeout", type=int, default=bancada.TIMEOUT_MS // 1000,
                   metavar="SEG", help="statement_timeout por query")
    p.add_argument("--forcar", action="store_true",
                   help="medir mesmo com carga rodando no banco observado")
    p.add_argument("--refazer", action="store_true",
                   help="remede experimentos já medidos")
    p.set_defaults(func=cmd_medir)

    def lista_de_rodadas(texto: str) -> List[int]:
        return [int(x) for x in texto.split(",") if x.strip()]

    p = com_corpus(sub.add_parser(
        "matriz", help="cada índice proposto contra o corpus inteiro"))
    p.add_argument("--rodadas", type=lista_de_rodadas, required=True,
                   metavar="N,N,...", help="de quais rodadas vêm os índices")
    p.add_argument("--repeticoes", type=int, default=bancada.REPETICOES,
                   help="a primeira é descartada (cache frio)")
    p.add_argument("--timeout", type=int, default=bancada.TIMEOUT_MS // 1000,
                   metavar="SEG", help="statement_timeout por query")
    p.add_argument("--forcar", action="store_true",
                   help="medir mesmo com carga rodando no banco observado")
    p.set_defaults(func=cmd_matriz)

    p = sub.add_parser("comparar",
                       help="avalia rodadas sobre a matriz: cobertura e precisão")
    p.add_argument("--rodadas", type=lista_de_rodadas, required=True,
                   metavar="N,N,...")
    p.add_argument("--sessao", type=int,
                   help="sessão da matriz (padrão: a última concluída)")
    p.add_argument("--limiar", type=float, default=matriz.LIMIAR_PADRAO,
                   metavar="PCT", help="redução mínima para contar como beneficiada")
    p.add_argument("--json", type=Path, metavar="ARQ")
    p.set_defaults(func=cmd_comparar)

    p = sub.add_parser("exportar",
                       help="o agrupamento em CSV/JSON, para conferir fora do banco")
    p.add_argument("--rodada", type=int, required=True)
    p.add_argument("--csv", type=Path, metavar="ARQ")
    p.add_argument("--json", type=Path, metavar="ARQ", dest="json_saida",
                   help="pontos + os parâmetros que os produziram")
    p.set_defaults(func=cmd_exportar)

    p = sub.add_parser("grafico", help="as figuras dos clusters, em PNG")
    p.add_argument("--rodada", type=int, required=True)
    p.add_argument("--saida", type=Path, metavar="ARQ",
                   help="padrão: clusters-rodadaN.png")
    p.add_argument("--projecoes", action="store_true",
                   help="também gera UMAP x PCA x t-SNE lado a lado")
    p.set_defaults(func=cmd_grafico)

    p = sub.add_parser("relatar", help="o veredito da rodada")
    p.add_argument("--rodada", type=int, required=True)
    com_planilha(p)
    p.set_defaults(func=cmd_relatar)

    p = sub.add_parser("limpar", help="derruba índices brokk_exp_* órfãos")
    p.set_defaults(func=cmd_limpar)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrompido — o que já foi medido está gravado")
        return 130


if __name__ == "__main__":
    sys.exit(main())
