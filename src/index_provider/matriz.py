#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A matriz índice × query, e a comparação de métodos de agrupamento sobre ela.

`medir` responde "o índice tirado de uma query serve ao resto do cluster?",
mas só olha para dentro do cluster. Não vê quem o índice ajudaria fora dele, e
é exatamente aí que o agrupamento por texto falha: lineitem(l_partkey) ajudou
Q17, Q19, Q8, Q9 e Q20, espalhadas por três clusters.

A matriz mede cada índice distinto proposto pelas rodadas pedidas contra o
corpus inteiro, numa sessão só:

1. **Linha de base única.** Cada query, sem índice, uma vez. Todos os índices
   são comparados com ela — e a Q17 e a Q20 estouram o timeout uma vez, não
   uma vez por índice.
2. **EXPLAIN antes de medir.** Com o índice criado, um EXPLAIN sem ANALYZE diz
   se o plano o usa. Só quem usa é medido; quem não usa tem ganho 0 (ver
   ``matriz_resultado``). Medir quem não usa só mediria cache.
3. **Um índice, uma medição.** O mesmo DDL proposto por dois métodos, com
   nomes diferentes, é medido uma vez (``chave_ddl``).

Com a matriz pronta, ``comparar`` avalia qualquer rodada sem voltar à
bancada: para cada cluster e o índice que ele propôs, quantas das queries que
o índice beneficia estão no cluster (cobertura) e quantas do cluster ele
beneficia (precisão).
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import bancada as bc
from .corpus import Query

LIMIAR_PADRAO = 10.0     # % de redução a partir da qual a query "foi beneficiada"


def chave_ddl(ddl: str) -> str:
    """O DDL sem o nome do índice, em caixa baixa e espaço simples."""
    sem_nome = re.sub(r"(?i)\bINDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON\b",
                      "INDEX ON", ddl)
    return " ".join(sem_nome.split()).rstrip(";").lower()


SQL_DDLS = """
SELECT ddl, relacao
  FROM monitoring.experimento
 WHERE rodada_id = ANY (%s)
   AND coalesce(ddl, '') <> ''
   AND estado IN ('proposto', 'medido')
 ORDER BY id
"""

SQL_SESSAO = """
INSERT INTO monitoring.matriz_sessao (rodadas, repeticoes, timeout_ms)
VALUES (%s, %s, %s) RETURNING id
"""

SQL_INDICE = """
INSERT INTO monitoring.matriz_indice (sessao_id, chave, ddl, relacao)
VALUES (%s, %s, %s, %s) RETURNING id
"""

SQL_FECHAR_INDICE = """
UPDATE monitoring.matriz_indice
   SET build_ms = %s, tamanho_bytes = %s, erro = %s
 WHERE id = %s
"""

SQL_MEDIDA = """
INSERT INTO monitoring.matriz_medida
       (sessao_id, indice_id, query_ref, repeticao, tempo_ms, usou_indice,
        censurado, erro)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def indices_das_rodadas(conn, rodadas: Sequence[int]) -> List[Tuple[str, str, Optional[str]]]:
    """(chave, ddl, relacao) de cada índice distinto proposto pelas rodadas."""
    with conn.cursor() as cur:
        cur.execute(SQL_DDLS, (list(rodadas),))
        linhas = cur.fetchall()
    vistos: Dict[str, Tuple[str, str, Optional[str]]] = {}
    for ddl, relacao in linhas:
        vistos.setdefault(chave_ddl(ddl), (chave_ddl(ddl), ddl, relacao))
    return list(vistos.values())


def _usa_indice(b: bc.Bancada, q: Query, nome: str) -> Tuple[Optional[bool], Optional[str]]:
    """O plano da query cita o índice? EXPLAIN sem ANALYZE: não executa nada."""
    try:
        comando, _via = b._montar(q, analisar=False, formato_json=True)
        with b.obs.cursor() as cur:
            cur.execute(comando)
            plano = cur.fetchone()[0]
        b.obs.commit()
        return bool(bc.ler_explain(plano, nome)["usou_indice"]), None
    except Exception as erro:                   # noqa: BLE001
        b.obs.rollback()
        return None, str(erro)[:400]
    finally:
        b._soltar_preparada()


def _gravar(conn, sessao_id: int, indice_id: Optional[int],
            medidas: Sequence[bc.Medida]) -> None:
    with conn.cursor() as cur:
        for m in medidas:
            cur.execute(SQL_MEDIDA, (sessao_id, indice_id, m.query_ref,
                                     m.repeticao, m.tempo_ms, m.usou_indice,
                                     m.censurado, m.erro))
    conn.commit()


def rodar(conn, b: bc.Bancada, rodadas: Sequence[int], queries: Sequence[Query],
          forcar: bool = False) -> int:
    """Mede a matriz inteira. Devolve o id da sessão."""
    indices = indices_das_rodadas(conn, rodadas)
    if not indices:
        raise SystemExit(f"nenhum índice proposto nas rodadas {list(rodadas)}. "
                         f"Rode candidatar antes.")

    b.preparar_sessao()
    b.checar_ociosidade(forcar)

    replicaveis = []
    for q in queries:
        pode, _via, erro = b.checar_replay(q)
        if pode:
            replicaveis.append(q)
        else:
            print(f"  ref={q.ref:<4} fora (não replicável): {erro.splitlines()[0][:60]}")

    with conn.cursor() as cur:
        cur.execute(SQL_SESSAO, (list(rodadas), b.repeticoes, b.timeout_ms))
        sessao_id = cur.fetchone()[0]
    conn.commit()
    print(f"sessão {sessao_id}: {len(indices)} índices distintos × "
          f"{len(replicaveis)} queries")

    for rel in sorted({r for _, _, r in indices if r}):
        b.analisar_relacao(rel)

    print(f"\nlinha de base: {len(replicaveis)} queries × {b.repeticoes} repetições")
    inicio = time.perf_counter()
    for q in replicaveis:
        medidas = b.medir(q, "antes", None)
        _gravar(conn, sessao_id, None, medidas)
        b._linha(q, medidas)
    print(f"  = {time.perf_counter() - inicio:.1f}s")

    for n, (chave, ddl, relacao) in enumerate(indices, 1):
        with conn.cursor() as cur:
            cur.execute(SQL_INDICE, (sessao_id, chave, ddl, relacao))
            indice_id = cur.fetchone()[0]
        conn.commit()
        print(f"\níndice {n}/{len(indices)}: {ddl}")
        nome = build_ms = tamanho = None
        erro_geral = None
        inicio = time.perf_counter()
        try:
            nome, build_ms, tamanho = b.criar_indice(ddl)
            if relacao:
                b.analisar_relacao(relacao)
            usam = []
            for q in replicaveis:
                usou, erro = _usa_indice(b, q, nome)
                _gravar(conn, sessao_id, indice_id,
                        [bc.Medida(q.ref, "depois", 0, usou_indice=usou, erro=erro)])
                if usou:
                    usam.append(q)
            print(f"  {len(usam)} de {len(replicaveis)} queries usam o índice: "
                  + ", ".join(f"ref={q.ref}" for q in usam))
            for q in usam:
                medidas = b.medir(q, "depois", nome)
                _gravar(conn, sessao_id, indice_id, medidas)
                b._linha(q, medidas)
        except KeyboardInterrupt:
            erro_geral = "interrompido"
            raise
        except Exception as erro:               # noqa: BLE001
            erro_geral = str(erro)[:400]
            print(f"  falhou: {erro}")
        finally:
            # O índice sai, tenha a medição terminado ou não.
            if nome:
                b.derrubar_indice(nome)
            with conn.cursor() as cur:
                cur.execute(SQL_FECHAR_INDICE, (build_ms, tamanho, erro_geral, indice_id))
            conn.commit()
        print(f"  = {time.perf_counter() - inicio:.1f}s")

    with conn.cursor() as cur:
        cur.execute("UPDATE monitoring.matriz_sessao SET concluida_em = now() WHERE id = %s",
                    (sessao_id,))
    conn.commit()
    return sessao_id


# --------------------------------------------------------------------------
# A comparação
# --------------------------------------------------------------------------

SQL_RESULTADO = """
SELECT chave, query_ref, usou_indice, ganho_pct, antes_censurado, depois_censurado
  FROM monitoring.matriz_resultado WHERE sessao_id = %s
"""

SQL_CLUSTERS = """
SELECT e.rodada_id, e.cluster_id, e.ddl, e.query_origem,
       r.normalizacao, r.modelo, r.parametros ->> 'min_cluster_size' AS tam_min,
       array_agg(c.query_ref ORDER BY c.query_ref) AS membros
  FROM monitoring.experimento e
  JOIN monitoring.rodada_cluster r ON r.id = e.rodada_id
  JOIN monitoring.query_cluster c  ON c.rodada_id = e.rodada_id
                                  AND c.cluster_id = e.cluster_id
 WHERE e.rodada_id = ANY (%s) AND coalesce(e.ddl, '') <> ''
 GROUP BY 1, 2, 3, 4, 5, 6, 7
 ORDER BY 1, 2
"""


def comparar(conn, sessao_id: int, rodadas: Sequence[int],
             limiar: float = LIMIAR_PADRAO) -> dict:
    """Avalia cada rodada sobre a matriz. Devolve um dicionário pronto para JSON."""
    with conn.cursor() as cur:
        cur.execute(SQL_RESULTADO, (sessao_id,))
        matriz: Dict[str, Dict[int, dict]] = {}
        for chave, ref, usou, ganho, a_cens, d_cens in cur.fetchall():
            matriz.setdefault(chave, {})[ref] = {
                "usou_indice": usou,
                "ganho_pct": None if ganho is None else float(ganho),
                "antes_e_piso": a_cens, "depois_e_piso": d_cens}
        cur.execute(SQL_CLUSTERS, (list(rodadas),))
        clusters = cur.fetchall()

    def beneficia(info) -> bool:
        return bool(info and info["usou_indice"] and info["ganho_pct"] is not None
                    and info["ganho_pct"] >= limiar)

    por_rodada: Dict[int, dict] = {}
    for rodada, cid, ddl, origem, norm, modelo, tam_min, membros in clusters:
        linha = matriz.get(chave_ddl(ddl), {})
        medidos = [r for r in membros if r in linha]
        beneficiadas = sorted(r for r, info in linha.items() if beneficia(info))
        dentro = [r for r in beneficiadas if r in membros]
        fora_da_origem = [r for r in dentro if r != origem]
        ganhos = [linha[r]["ganho_pct"] for r in medidos
                  if linha[r]["ganho_pct"] is not None]
        rr = por_rodada.setdefault(rodada, {
            "rodada": rodada, "normalizacao": norm, "modelo": modelo,
            "min_cluster_size": int(tam_min) if tam_min else None, "clusters": []})
        rr["clusters"].append({
            "cluster": cid,
            "indice": ddl,
            "query_origem": origem,
            "membros": list(membros),
            "beneficiadas_no_corpus": beneficiadas,
            "beneficiadas_no_cluster": dentro,
            "beneficiadas_fora_do_cluster": [r for r in beneficiadas if r not in membros],
            "beneficiadas_fora_da_origem": fora_da_origem,
            "cobertura": round(len(dentro) / len(beneficiadas), 3) if beneficiadas else None,
            "precisao": round(len(dentro) / len(medidos), 3) if medidos else None,
            "media_geral_pct": round(sum(ganhos) / len(ganhos), 2) if ganhos else None,
            "media_e_piso": any(linha[r]["antes_e_piso"] for r in medidos),
        })

    for rr in por_rodada.values():
        cs = rr["clusters"]
        cob = [c["cobertura"] for c in cs if c["cobertura"] is not None]
        pre = [c["precisao"] for c in cs if c["precisao"] is not None]
        med = [c["media_geral_pct"] for c in cs if c["media_geral_pct"] is not None]
        rr["resumo"] = {
            "clusters": len(cs),
            "cobertura_media": round(sum(cob) / len(cob), 3) if cob else None,
            "precisao_media": round(sum(pre) / len(pre), 3) if pre else None,
            "media_geral_pct": round(sum(med) / len(med), 2) if med else None,
            "beneficiadas_fora_da_origem": sum(len(c["beneficiadas_fora_da_origem"]) for c in cs),
            "beneficiadas_fora_do_cluster": sum(len(c["beneficiadas_fora_do_cluster"]) for c in cs),
        }

    return {"sessao": sessao_id, "limiar_pct": limiar,
            "rodadas": [por_rodada[r] for r in sorted(por_rodada)]}
