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
               matriz, otimizacao, saida, vetores)
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
        rotulo = graficos.rotulos_tpch(pontos, args.consultas)
        casados = sum(1 for v in rotulo.values() if not v.startswith("#"))
        print(f"rodada {args.rodada}: {len(pontos)} queries, "
              f"{meta['clusters']} clusters, {meta['ruido']} em ruído")
        print(f"  {casados} de {len(pontos)} casadas com "
              f"{args.consultas or 'queries'}/*.sql")

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
    if not (args.rodadas or args.globais):
        print("de onde vêm os índices? --rodadas N,N,... e/ou --globais N",
              file=sys.stderr)
        return 2
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
        fontes = []
        if args.rodadas:
            fontes.append(("rodada", matriz.indices_das_rodadas(conn, args.rodadas)))
        if args.globais:
            # A heurística lê o catálogo do banco observado; com a sessão
            # preparada antes, nem essas consultas vão para o log dele.
            b.preparar_sessao()
            globais = matriz.indices_da_heuristica(
                conn, obs, [q.ref for q in qs], args.globais,
                trigramas=args.trigramas)
            trgm = sum(1 for _, ddl, _ in globais if "gin_trgm_ops" in ddl)
            print(f"  heurística: {len(globais)} candidatos para o corpus inteiro"
                  + (f", {trgm} de trigrama" if args.trigramas else ""))
            fontes.append(("heuristica", globais))
        indices = matriz.juntar(fontes)
        inicio = time.perf_counter()
        sessao = matriz.rodar(conn, b, indices, qs, rodadas=args.rodadas or [],
                              globais=args.globais, trigramas=args.trigramas,
                              forcar=args.forcar)
        print(f"\nsessão {sessao} concluída em "
              f"{(time.perf_counter() - inicio) / 60:.1f} min. Para avaliar:")
        if args.rodadas:
            print(f"  python -m src.index_provider comparar --sessao {sessao} "
                  f"--rodadas {','.join(map(str, args.rodadas))}")
        print(f"  python -m src.index_provider otimizar --sessao {sessao}")
    return 0


def cmd_comparar(args) -> int:
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        sessao = args.sessao or _ultima_sessao(conn)
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


def cmd_otimizar(args) -> int:
    """O conjunto de índices da carga inteira, pelo programa inteiro."""
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        if args.listar:
            return otimizacao.listar(conn)
        sessao = args.sessao or _ultima_sessao(conn)
        if sessao is None:
            print("nenhuma matriz concluída. Rode antes: matriz --globais N")
            return 2

        inst = otimizacao.carregar(conn, sessao, args.peso, args.limiar)
        otimizacao.mostrar_instancia(inst)
        rotulo = graficos.rotulos_tpch(
            [{"query_ref": r, "query_text": t} for r, t in inst.textos.items()],
            args.consultas)

        def parametros(orcamento):
            return otimizacao.Parametros(
                modelo=args.modelo, peso=args.peso, limiar=args.limiar,
                orcamento_mb=orcamento, lambda_mb=args.lambda_mb,
                custo_escrita=args.custo_escrita)

        resultados = []
        for orcamento in (args.orcamento_mb or [None]):
            par = parametros(orcamento)
            av, status = otimizacao.resolver(inst, par)
            oid = otimizacao.gravar(conn, inst, par, av, status)
            otimizacao.mostrar_solucao(inst, par, av, status, oid, rotulo)
            resultados.append(otimizacao.como_dict(inst, par, av, status, oid))

        if len(resultados) > 1:
            print(f"\n  {'orçamento':>10} {'índices':>8} {'MB':>9} "
                  f"{'benefício ms':>14} {'objetivo':>14} {'otimização':>11}")
            for r in resultados:
                orc = r["parametros"]["orcamento_mb"]
                print(f"  {'-' if orc is None else f'{orc:,.0f}':>10} "
                      f"{len(r['indices']):>8} {r['armazenamento_mb']:>9,.1f} "
                      f"{r['beneficio_previsto_ms']:>14,.0f} "
                      f"{r['objetivo']:>14,.0f} {r['otimizacao']:>11}")

        # O conjunto que cada rodada de clusters propôs, na mesma régua. Vai
        # gravado como otimização para que `validar` o meça do mesmo jeito:
        # criado inteiro, contra a mesma carga.
        rodadas = []
        if args.rodadas:
            print("\n  mesmo modelo, o conjunto que cada rodada propôs "
                  "(sem orçamento):")
        for rodada in args.rodadas or []:
            ids, fora = otimizacao.conjunto_da_rodada(conn, inst, rodada)
            par = parametros(None)
            par.rodada = rodada
            av = otimizacao.avaliar(inst, par, ids)
            status = f"conjunto da rodada {rodada}"
            oid = otimizacao.gravar(conn, inst, par, av, status)
            extra = f"   ({len(fora)} não medido(s) nesta sessão)" if fora else ""
            print(f"    rodada {rodada}: {len(av.indices)} índice(s), "
                  f"{av.mb:,.1f} MB, benefício {av.beneficio_ms:,.0f} ms, "
                  f"objetivo {av.objetivo:,.0f}  -> otimização {oid}{extra}")
            rodadas.append({"rodada": rodada, "nao_medidos": fora,
                            **otimizacao.como_dict(inst, par, av, status, oid)})

        if args.json:
            corpo = {"sessao": sessao,
                     "rotulos": {str(r): rotulo[r] for r in sorted(rotulo)},
                     "otimizacoes": resultados, "rodadas": rodadas}
            args.json.write_text(banco.como_json(corpo), encoding="utf-8")
            print(f"\n  JSON em {args.json}")
    return 0


def cmd_validar(args) -> int:
    """Cria o conjunto escolhido inteiro, mede, e põe ao lado do previsto."""
    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        if not args.relatorio:
            obs = banco.conectar(banco.DSN_OBSERVADO, "banco observado",
                                 app=bancada.NOME_APP)
            with obs:
                qs = _corpus(conn, args)
                faltando = corpus.anexar_parametros(conn, qs)
                if faltando:
                    print(f"  AVISO: {faltando} queries parametrizadas sem "
                          f"valores gravados; elas vão falhar no replay")
                b = bancada.Bancada(obs, conn, repeticoes=args.repeticoes,
                                    timeout_ms=args.timeout * 1000,
                                    verboso=not args.silencioso)
                inicio = time.perf_counter()
                if args.confirma_adaptacao and args.finalidade != "confirmacao":
                    raise SystemExit("--confirma-adaptacao pede --finalidade confirmacao")
                validacao = otimizacao.validar(
                    conn, b, args.otimizacao, qs, refazer=args.refazer,
                    forcar=args.forcar, nova=args.nova_validacao,
                    finalidade=args.finalidade, ordem=args.ordem,
                    confirma_adaptacao=args.confirma_adaptacao,
                    movimento=args.movimento)
                args.validacao = validacao
                print(f"\nvalidação {validacao} concluída em "
                      f"{(time.perf_counter() - inicio) / 60:.1f} min")

        rel = otimizacao.relatorio(conn, args.otimizacao, args.validacao)
        rotulo = graficos.rotulos_tpch(
            [{"query_ref": r, "query_text": t} for r, t in rel["textos"].items()],
            args.consultas)
        otimizacao.mostrar_relatorio(rel, rotulo)
        if args.json:
            rel["rotulos"] = {str(r): rotulo[r] for r in sorted(rotulo)}
            args.json.write_text(banco.como_json(rel), encoding="utf-8")
            print(f"\n  JSON em {args.json}")
    return 0


def _parametros_adaptativos(args):
    from . import adaptativa
    return adaptativa.Parametros(
        confianca=args.confianca, relevancia=args.relevancia,
        margem_ms=None if args.margem_s is None else args.margem_s * 1000,
        lambda_mb=args.lambda_mb, custo_entrada_ms=args.custo_entrada,
        paciencia=args.paciencia, max_validacoes=args.max_validacoes,
        max_medicoes=args.max_medicoes, exploracao=args.exploracao,
        resolucao=args.resolucao,
        vizinhos=args.vizinhos, amostras=args.amostras, semente=args.semente,
        peso=args.peso, modelo=args.modelo)


def cmd_adaptar(args) -> int:
    """Seleção adaptativa: decide sobre configurações completas medidas e
    diz até onde vale continuar. Ver adaptativa.py."""
    from . import adaptativa, estatistica

    conn = banco.conectar(args.dsn)
    with conn:
        banco.aplicar_esquema(conn)
        if args.listar:
            return adaptativa.listar(conn)
        sessao = args.sessao or _ultima_sessao(conn)
        if sessao is None:
            print("nenhuma matriz concluída. Rode antes: matriz --globais N")
            return 2
        par = _parametros_adaptativos(args)
        with conn.cursor() as cur:
            cur.execute("SELECT query_ref, query_text FROM monitoring.query_resumo")
            textos = [{"query_ref": r, "query_text": t} for r, t in cur.fetchall()]
        rotulo = graficos.rotulos_tpch(textos, args.consultas)

        precisa_bancada = args.executar
        obs = None
        if precisa_bancada or not args.sem_catalogo:
            try:
                obs = banco.psycopg.connect(banco.DSN_OBSERVADO,
                                            application_name=bancada.NOME_APP,
                                            connect_timeout=5)
            except Exception as erro:               # noqa: BLE001
                if precisa_bancada:
                    raise SystemExit(f"banco observado inacessível: {erro}")
                print(f"  (banco observado inacessível, seguindo sem catálogo: "
                      f"{str(erro).splitlines()[0]})")
        b = qs = None
        if obs is not None:
            b = bancada.Bancada(obs, conn, repeticoes=args.repeticoes,
                                timeout_ms=args.timeout * 1000,
                                verboso=not args.silencioso)
            # A sessão é preparada antes de qualquer leitura de catálogo,
            # para nem elas irem para o log do banco observado.
            b.preparar_sessao()
        if precisa_bancada:
            qs = _corpus(conn, args)
            faltando = corpus.anexar_parametros(conn, qs)
            if faltando:
                print(f"  AVISO: {faltando} queries parametrizadas sem valores "
                      f"gravados; elas vão falhar no replay")

        try:
            if args.ruido:
                an = adaptativa.preparar(conn, sessao, par, obs)
                adaptativa.mostrar_ruido(an, rotulo)
                return 0
            if args.confirmar is not None:
                return _confirmar(conn, b, qs, sessao, args, rotulo, obs)
            inicio = time.perf_counter()
            an, traj, rel, aid = adaptativa.executar(
                conn, b, qs, sessao, par, rotulo, obs=obs, forcar=args.forcar,
                modo="execucao" if args.executar else "analise",
                medir=args.executar)
            adaptativa.mostrar(rel)
            print(f"\n  adaptação {aid} gravada "
                  f"({rel['medicoes_nesta_execucao']} medição(ões) nesta execução, "
                  f"{(time.perf_counter() - inicio) / 60:.1f} min)")
            if rel.get("proxima"):
                print(f"  para continuar: {rel['proxima']['comando']}")
            elif rel["caminho"]:
                print(f"  para a confirmação independente: adaptar --sessao {sessao} "
                      f"--confirmar {aid} --executar")
            if args.json:
                args.json.write_text(banco.como_json(rel), encoding="utf-8")
                print(f"  JSON em {args.json}")
        finally:
            if obs is not None:
                obs.close()
    return 0


def _confirmar(conn, b, qs, sessao, args, rotulo, obs) -> int:
    """Mede à parte (finalidade 'confirmacao') o último passo aceito de uma
    adaptação e diz se ele se sustenta fora dos dados que o escolheram."""
    from psycopg.types.json import Jsonb
    from . import adaptativa, estatistica

    with conn.cursor() as cur:
        cur.execute("SELECT sessao_id, parametros, resultado FROM monitoring.adaptacao "
                    " WHERE id = %s", (args.confirmar,))
        linha = cur.fetchone()
    if not linha:
        raise SystemExit(f"adaptação {args.confirmar} não existe (adaptar --listar)")
    sessao_x, par_x, rel_x = linha
    if sessao_x != sessao:
        raise SystemExit(f"a adaptação {args.confirmar} é da sessão {sessao_x}")
    if not rel_x or len(rel_x.get("caminho_chaves") or []) < 2:
        raise SystemExit(f"a adaptação {args.confirmar} não aceitou movimento "
                         f"nenhum: nada a confirmar")
    # A confirmação usa os parâmetros declarados na seleção: margem e
    # confiança não se escolhem depois de ver o resultado.
    par = adaptativa.Parametros(**{k: v for k, v in par_x.items()
                                   if k in adaptativa.Parametros.__dataclass_fields__})
    alvos = [frozenset(c) for c in rel_x["caminho_chaves"][-2:] if c]
    if args.executar:
        aid = adaptativa.abrir(conn, sessao, par, "confirmacao")
        an = adaptativa.preparar(conn, sessao, par, obs)
        medidas = 0
        for c in alvos:
            ja = [v for v in an.exp.validacoes if v.configuracao == c
                  and v.finalidade == "confirmacao"
                  and v.confirma_adaptacao == args.confirmar]
            if ja and not args.repetir_confirmacao:
                print(f"  {adaptativa._c(c)} já tem {len(ja)} sessão(ões) de "
                      f"confirmação; --repetir-confirmacao mede mais uma")
                continue
            oid = adaptativa.otimizacao_para(conn, an, c, aid, "confirmacao")
            print(f"\n>>> confirmação: otimização {oid} {adaptativa._c(c)}")
            otimizacao.validar(conn, b, oid, qs, nova=True, finalidade="confirmacao",
                               ordem=adaptativa.ordem_da_validacao(an, c),
                               origem="adaptar", adaptacao_id=aid,
                               confirma_adaptacao=args.confirmar, forcar=args.forcar)
            medidas += 1
        with conn.cursor() as cur:
            cur.execute("UPDATE monitoring.adaptacao SET estado = 'concluida', "
                        "       medicoes = %s, motivo = %s, concluida_em = now() "
                        " WHERE id = %s",
                        (medidas, f"confirmação da adaptação {args.confirmar}", aid))
        conn.commit()
    an = adaptativa.preparar(conn, sessao, par, obs)
    res = adaptativa.confirmar(an, args.confirmar, rel_x)
    with conn.cursor() as cur:
        cur.execute("UPDATE monitoring.adaptacao SET confirmada = %s, confirmacao = %s "
                    " WHERE id = %s", (res.get("confirmada"),
                                       Jsonb(adaptativa._jsonavel(res)), args.confirmar))
    conn.commit()
    s = adaptativa._s
    print(f"\nconfirmação da adaptação {args.confirmar}:")
    if "confirmacao" not in res:
        print(f"  {res.get('motivo')} {res.get('faltam', '')}")
        print(f"  rode: adaptar --sessao {sessao} --confirmar {args.confirmar} --executar")
        return 0
    c, sel = res["confirmacao"], res["selecao"]
    print(f"  {res['de']} → {res['para']}, {res['teste']} com margem "
          f"{res['margem_ms'] / 1000:,.1f} s (a da seleção)")
    print(f"  seleção:    ΔB {s(sel['delta_ms'])} s {adaptativa._ic_txt(sel)}")
    print(f"  confirmação ΔB {s(c['delta_ms'])} s {adaptativa._ic_txt(c)} "
          f"({', '.join(c['sessoes'])})")
    rep = res.get("reproducao")
    if rep:
        print(f"  reprodução: a confirmação mediu {rep['razao_confirmacao_sobre_selecao']:.0%} "
              f"do ganho estimado na seleção"
              + ("" if rep["mesmo_sinal"] else ", com o sinal trocado")
              + ("; a estimativa da seleção cabe no intervalo da confirmação"
                 if rep["selecao_no_intervalo_da_confirmacao"] else
                 "; a estimativa da seleção fica FORA do intervalo da confirmação"))
    veredito = {True: "CONFIRMADA", False: "NÃO CONFIRMADA: a exploração se reabre; "
                "estas sessões passam a ser evidência de seleção e a próxima "
                "recomendação pede outra confirmação"}.get(
        res["confirmada"], "INCONCLUSIVA: meça mais uma sessão de confirmação "
                           "(--repetir-confirmacao)")
    print(f"  {veredito}")

    # Os outros conjuntos medidos à parte para esta adaptação (métodos de
    # comparação: clusters, `otimizar`), na mesma régua.
    av = adaptativa.avaliacao_independente(
        an, args.confirmar, rotulo, frozenset(rel_x["caminho_chaves"][-1]))
    if len(av["configuracoes"]) > 2:
        adaptativa.mostrar_avaliacao(av)
    if args.json:
        res["avaliacao_independente"] = av
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


def _ultima_sessao(conn) -> Optional[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM monitoring.matriz_sessao "
                    " WHERE concluida_em IS NOT NULL")
        return cur.fetchone()[0]


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

    def lista_de_numeros(texto: str) -> List[float]:
        return [float(x) for x in texto.split(",") if x.strip()]

    p = com_corpus(sub.add_parser(
        "matriz", help="cada índice proposto contra o corpus inteiro"))
    p.add_argument("--rodadas", type=lista_de_rodadas,
                   metavar="N,N,...", help="de quais rodadas vêm os índices")
    p.add_argument("--globais", type=int, metavar="N",
                   help="mede também os N primeiros candidatos da heurística "
                        "para o corpus inteiro, sem cluster (os de otimizar)")
    p.add_argument("--trigramas", action="store_true",
                   help="com --globais, oferece também GIN com gin_trgm_ops "
                        "para LIKE '%%x%%' e regex (pede a extensão pg_trgm)")
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

    p = sub.add_parser(
        "otimizar", help="escolhe o conjunto de índices da carga inteira "
                         "(programa inteiro sobre a matriz, sem clusters)")
    p.add_argument("--sessao", type=int,
                   help="sessão da matriz (padrão: a última concluída)")
    p.add_argument("--orcamento-mb", type=lista_de_numeros, metavar="MB[,MB...]",
                   help="espaço máximo do conjunto; uma lista resolve um "
                        "problema por orçamento (padrão: sem limite)")
    p.add_argument("--lambda", dest="lambda_mb", type=float, default=0.0,
                   metavar="MS_POR_MB", help="penalidade por MB, nas unidades "
                   "do benefício (padrão: 0)")
    p.add_argument("--custo-escrita", type=float, default=0.0, metavar="MS",
                   help="ms de manutenção por escrita na tabela, por índice "
                        "(padrão: 0, não medido)")
    p.add_argument("--peso", choices=otimizacao.PESOS, default="execucoes",
                   help="w_q: execuções da query no corpus, ou 1")
    p.add_argument("--modelo", choices=otimizacao.MODELOS, default="conservador",
                   help="como o Δ(q, i) medido vira Δ(q, S)")
    p.add_argument("--limiar", type=float, default=matriz.LIMIAR_PADRAO,
                   metavar="PCT", help="|Δ| abaixo disto é ruído e vale 0")
    p.add_argument("--rodadas", type=lista_de_rodadas, metavar="N,N,...",
                   help="avalia também o conjunto de cada rodada, no mesmo modelo")
    p.add_argument("--consultas", type=Path, metavar="PASTA",
                   help="SQL de referência dos rótulos (queries/ycsb no YCSB)")
    p.add_argument("--json", type=Path, metavar="ARQ")
    p.add_argument("--listar", action="store_true", help="lista as otimizações")
    p.set_defaults(func=cmd_otimizar)

    p = com_corpus(sub.add_parser(
        "validar", help="cria o conjunto de uma otimização de uma vez e "
                        "compara o medido com o previsto"))
    p.add_argument("--otimizacao", type=int, required=True)
    p.add_argument("--repeticoes", type=int, default=bancada.REPETICOES,
                   help="a primeira é descartada (cache frio)")
    p.add_argument("--timeout", type=int, default=bancada.TIMEOUT_MS // 1000,
                   metavar="SEG", help="statement_timeout por query")
    p.add_argument("--forcar", action="store_true",
                   help="medir mesmo com carga rodando no banco observado")
    p.add_argument("--refazer", action="store_true",
                   help="mede de novo e APAGA as validações anteriores")
    p.add_argument("--nova-validacao", action="store_true",
                   help="mede de novo e guarda as anteriores (outra sessão)")
    p.add_argument("--finalidade", choices=otimizacao.FINALIDADES,
                   default="selecao",
                   help="selecao: evidência para decidir; confirmacao: medida "
                        "independente do que foi decidido")
    p.add_argument("--ordem", choices=otimizacao.ORDENS, default="base_primeiro",
                   help="ordem dos blocos na sessão")
    p.add_argument("--confirma-adaptacao", type=int, metavar="N",
                   help="com --finalidade confirmacao: a adaptação cuja "
                        "recomendação esta medida avalia de forma independente")
    p.add_argument("--movimento", metavar="M",
                   help="o movimento de `adaptar` que pediu esta validação "
                        "(o comando que `adaptar` sugere já o traz); 'resolucao' "
                        "conta no orçamento de resolução de incerteza")
    p.add_argument("--validacao", type=int, metavar="N",
                   help="com --relatorio: qual validação mostrar (padrão: a última)")
    p.add_argument("--relatorio", action="store_true",
                   help="só mostra previsto × medido, sem bancada")
    p.add_argument("--consultas", type=Path, metavar="PASTA")
    p.add_argument("--json", type=Path, metavar="ARQ")
    p.set_defaults(func=cmd_validar)

    p = com_corpus(sub.add_parser(
        "adaptar", help="seleção adaptativa: configurações completas medidas, "
                        "benefício marginal com incerteza e critério de parada"))
    p.add_argument("--sessao", type=int,
                   help="sessão da matriz (padrão: a última concluída)")
    p.add_argument("--executar", action="store_true",
                   help="roda as validações que a decisão pede (sem isto, só "
                        "decide sobre a evidência existente)")
    p.add_argument("--max-medicoes", type=int, default=6, metavar="N",
                   help="validações por execução de --executar (padrão: 6)")
    p.add_argument("--max-validacoes", type=int, default=3, metavar="N",
                   help="sessões de seleção por configuração antes de declarar "
                        "um par não ordenável (padrão: 3)")
    p.add_argument("--confianca", type=float, default=0.90,
                   help="nível do intervalo bilateral (padrão: 0.90; cada "
                        "limite é unilateral a 95%%)")
    p.add_argument("--relevancia", type=float, default=0.01, metavar="FRAÇÃO",
                   help="ρ: ganho mínimo relevante como fração do tempo total "
                        "ponderado da configuração atual (padrão: 0.01)")
    p.add_argument("--margem-s", type=float, metavar="S",
                   help="margem de relevância absoluta, em s ponderados, no "
                        "lugar de ρ (declarada antes, por quem decide)")
    p.add_argument("--lambda", dest="lambda_mb", type=float, default=0.0,
                   metavar="MS_POR_MB", help="custo do espaço, em ms de "
                   "benefício por MB (padrão: 0: espaço só desempata)")
    p.add_argument("--custo-entrada", type=float, metavar="MS",
                   help="HIPÓTESE de custo de manutenção por entrada de índice; "
                        "sem isto e sem custo medido, só a leitura é avaliada")
    p.add_argument("--paciencia", type=int, default=2,
                   help="crescimentos seguidos sem ganho provado (rejeitados ou "
                        "não ordenáveis) para parar (padrão: 2)")
    p.add_argument("--exploracao", type=int, default=2,
                   help="combinações sem ganho previsto testadas antes de "
                        "parar (padrão: 2)")
    p.add_argument("--resolucao", type=int, default=3, metavar="N",
                   help="validações, no experimento, para remedir antes de parar "
                        "alternativas já medidas inconclusivas e ainda "
                        "competitivas: UCB(ΔB) − custo ≥ margem (padrão: 3)")
    p.add_argument("--vizinhos", type=int, default=4,
                   help="candidatas por tipo de movimento (padrão: 4)")
    p.add_argument("--amostras", type=int, default=4000,
                   help="sorteios do bootstrap paramétrico (padrão: 4000)")
    p.add_argument("--semente", type=int, default=42)
    p.add_argument("--peso", choices=otimizacao.PESOS, default="execucoes")
    p.add_argument("--modelo", choices=otimizacao.MODELOS, default="conservador",
                   help="modelo de interação usado só para PRIORIZAR candidatas")
    p.add_argument("--confirmar", type=int, metavar="ADAPTACAO",
                   help="confirmação independente da recomendação da adaptação "
                        "N (com --executar, mede; sem, só analisa)")
    p.add_argument("--repetir-confirmacao", action="store_true",
                   help="com --confirmar --executar: mede mais uma sessão")
    p.add_argument("--ruido", action="store_true",
                   help="só mostra o modelo de ruído (σ_α, σ_ε por query)")
    p.add_argument("--sem-catalogo", action="store_true",
                   help="não consulta o banco observado (sem --executar)")
    p.add_argument("--listar", action="store_true", help="lista as adaptações")
    p.add_argument("--repeticoes", type=int, default=bancada.REPETICOES)
    p.add_argument("--timeout", type=int, default=bancada.TIMEOUT_MS // 1000,
                   metavar="SEG")
    p.add_argument("--forcar", action="store_true",
                   help="medir mesmo com carga rodando no banco observado")
    p.add_argument("--consultas", type=Path, metavar="PASTA")
    p.add_argument("--json", type=Path, metavar="ARQ")
    p.set_defaults(func=cmd_adaptar)

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
    p.add_argument("--consultas", type=Path, metavar="PASTA",
                   help="SQL de referência dos rótulos (padrão: queries/, "
                        "o TPC-H; queries/ycsb para o YCSB)")
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
