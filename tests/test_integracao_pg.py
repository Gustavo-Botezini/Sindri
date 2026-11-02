"""Integração com PostgreSQL, num banco descartável.

Cria um banco novo no servidor de monitoramento, aplica os scripts de
initdb-monitoring/ (o caminho de um volume novo), grava medições sintéticas
com DUAS validações do mesmo conjunto e exercita o que só o SQL testa: a
migração idempotente com dados, a view por validação, o carregador de
evidência, a persistência da adaptação e o relatório por validação. O banco
é derrubado no fim. Não toca no banco observado.

Só roda com a variável de ambiente apontando o servidor:

    BROKK_TESTE_PG=postgresql://brokk:brokk_dev_pwd@localhost:5433/postgres \\
        ./.venv/bin/python -m unittest tests.test_integracao_pg -v
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from src.index_provider import adaptativa, banco, evidencia, otimizacao
from src.index_provider.estatistica import VAZIA
from tests.simulado import Bancada, Verdade, efeito_por_indice

ADMIN = os.environ.get("BROKK_TESTE_PG")
RAIZ = Path(__file__).resolve().parent.parent
NOME = f"brokk_teste_adaptativa_{os.getpid()}"


@unittest.skipUnless(ADMIN, "defina BROKK_TESTE_PG para rodar contra um servidor")
class IntegracaoPostgres(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{NOME}"')
        base = ADMIN.rsplit("/", 1)[0]
        cls.conn = psycopg.connect(f"{base}/{NOME}")
        for arq in sorted((RAIZ / "initdb-monitoring").glob("0*.sql")):
            with cls.conn.cursor() as cur:
                cur.execute(arq.read_text(encoding="utf-8"))
            cls.conn.commit()
        cls._popular()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        with cls.psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{NOME}" WITH (FORCE)')

    @classmethod
    def _popular(cls):
        """A bancada sintética, gravada como a matriz e `validar` gravariam."""
        from psycopg.types.json import Jsonb
        base = {q: 200.0 * (1 + q % 3) for q in range(10)}
        tabela = {1: {0: 0.2, 1: 0.5}, 2: {2: 0.6}}
        v = Verdade(base, {q: 10.0 for q in base}, {1: 20.0, 2: 30.0},
                    efeito_por_indice(tabela), sigma_alfa=0.05)
        sim = Bancada(v, 11)
        alvo = frozenset([1, 2])
        sim.validar(alvo)
        sim.validar(alvo, ordem="conjunto_primeiro")
        cls.sim = sim
        c = cls.conn
        with c.cursor() as cur:
            ref = {}
            for q in base:
                cur.execute("INSERT INTO monitoring.query (database_name, query_hash, "
                            "query_text) VALUES ('teste', %s, %s) RETURNING id",
                            (f"h{q}", f"SELECT {q} FROM t"))
                ref[q] = cur.fetchone()[0]
                cur.execute("INSERT INTO monitoring.query_resumo (query_ref, "
                            "database_name, query_text, execucoes) VALUES "
                            "(%s, 'teste', %s, 10)", (ref[q], f"SELECT {q} FROM t"))
            cur.execute("INSERT INTO monitoring.matriz_sessao (rodadas, repeticoes, "
                        "timeout_ms) VALUES ('{}', 5, 300000) RETURNING id")
            cls.sessao = cur.fetchone()[0]
            ind = {}
            for i, mb in v.tamanho_mb.items():
                cur.execute("INSERT INTO monitoring.matriz_indice (sessao_id, chave, ddl, "
                            "relacao, tamanho_bytes, origem) VALUES (%s, %s, %s, 't', %s, "
                            "'heuristica') RETURNING id",
                            (cls.sessao, f"k{i}", f"CREATE INDEX brokk_exp_i{i} ON t (c{i})",
                             int(mb * 1048576)))
                ind[i] = cur.fetchone()[0]
            for chave, por_q in sim.medidas.items():
                if not chave.startswith("m1/"):
                    continue
                i = chave.split("/")[1]
                indice = None if i == "base" else ind[int(i)]
                for q, linhas in por_q.items():
                    for fase, rep, tempo, cens, erro, usou in linhas:
                        cur.execute("INSERT INTO monitoring.matriz_medida (sessao_id, "
                                    "indice_id, query_ref, repeticao, tempo_ms, "
                                    "usou_indice, censurado, erro) VALUES "
                                    "(%s, %s, %s, %s, %s, %s, %s, %s)",
                                    (cls.sessao, indice, ref[q], rep, tempo,
                                     usou if rep == 0 else None, cens, erro))
            cur.execute("INSERT INTO monitoring.otimizacao (sessao_id, parametros, status, "
                        "indices, previsao, validado_em) VALUES (%s, '{}', 'teste', %s, %s, "
                        "now()) RETURNING id",
                        (cls.sessao, sorted(ind[i] for i in alvo),
                         Jsonb({str(ref[q]): {"peso": 10.0, "delta_ms": 0.0, "atende": None,
                                              "pioram": [], "piso": False} for q in base})))
            cls.oid = cur.fetchone()[0]
            for x in sim.validacoes:
                cur.execute("INSERT INTO monitoring.otimizacao_validacao (otimizacao_id, "
                            "validacao, finalidade, origem, ordem, concluida_em) VALUES "
                            "(%s, %s, 'selecao', 'validar', %s, now())",
                            (cls.oid, x.validacao, x.ordem))
                for q, linhas in sim.medidas[x.sessao].items():
                    for fase, rep, tempo, cens, erro, usados in linhas:
                        cur.execute("INSERT INTO monitoring.otimizacao_medida (otimizacao_id, "
                                    "validacao, query_ref, fase, repeticao, tempo_ms, "
                                    "indices_usados, censurado, erro) VALUES "
                                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                    (cls.oid, x.validacao, ref[q], fase, rep, tempo,
                                     None if usados is None else [ind[i] for i in usados],
                                     cens, erro))
        c.commit()
        cls.ref, cls.ind = ref, ind

    def test_1_migracao_idempotente_com_dados(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*), sum(tempo_ms) FROM monitoring.otimizacao_medida")
            antes = cur.fetchone()
        banco.aplicar_esquema(self.conn)
        banco.aplicar_esquema(self.conn)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*), sum(tempo_ms) FROM monitoring.otimizacao_medida")
            self.assertEqual(cur.fetchone(), antes)
            # as validações gravadas não viram 'legado' na segunda passada
            cur.execute("SELECT finalidade FROM monitoring.otimizacao_validacao")
            self.assertEqual({r[0] for r in cur.fetchall()}, {"selecao"})

    def test_2_view_separa_validacoes(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT validacao, count(*) FROM monitoring.otimizacao_resultado "
                        "WHERE otimizacao_id = %s GROUP BY 1 ORDER BY 1", (self.oid,))
            self.assertEqual(cur.fetchall(), [(1, 10), (2, 10)])
        r1 = otimizacao.relatorio(self.conn, self.oid, 1)
        r2 = otimizacao.relatorio(self.conn, self.oid)
        self.assertEqual((r1["validacao"], r2["validacao"]), (1, 2))
        self.assertEqual(len(r2["validacoes"]), 2)

    def test_3_carregador_e_caminhada(self):
        exp = evidencia.carregar(self.conn, self.sessao)
        alvo = frozenset(self.ind.values())
        self.assertEqual([v.sessao for v in exp.validacoes_de(alvo)],
                         [f"v{self.oid}.1", f"v{self.oid}.2"])
        ev = exp.evidencia()
        sessoes = {b.sessao for b in ev.blocos}
        self.assertEqual(sessoes, {f"m{self.sessao}", f"v{self.oid}.1", f"v{self.oid}.2"})
        par = adaptativa.Parametros(amostras=500)
        an = adaptativa.analisar(exp, par)
        self.assertTrue(an.ruido.suficiente)
        cmp = an.comparar(VAZIA, alvo)
        self.assertGreater(cmp.li_ms, 0)
        traj = adaptativa.caminhar(an)
        rel = adaptativa.relatorio(an, traj, {})
        aid = adaptativa.abrir(self.conn, self.sessao, par, "analise")
        adaptativa.fechar(self.conn, aid, an, traj, rel, 0)
        with self.conn.cursor() as cur:
            cur.execute("SELECT estado, validacoes_lidas FROM monitoring.adaptacao "
                        "WHERE id = %s", (aid,))
            estado, lidas = cur.fetchone()
            cur.execute("SELECT count(*) FROM monitoring.adaptacao_passo "
                        "WHERE adaptacao_id = %s", (aid,))
            passos = cur.fetchone()[0]
        self.assertIn(estado, ("concluida", "aguardando_medicao"))
        self.assertEqual(lidas, 2)
        self.assertEqual(passos, len(traj.passos))


if __name__ == "__main__":
    unittest.main()
