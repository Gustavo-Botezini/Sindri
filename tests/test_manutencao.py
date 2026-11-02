"""Exposição estática à escrita: o que o SQL permite deduzir, e só isso."""

from __future__ import annotations

import unittest

from src.index_provider import manutencao as mnt


class Classificar(unittest.TestCase):
    def test_update_com_colunas(self):
        e = mnt.classificar("UPDATE usertable SET field10 = left(md5($1::text) "
                            "|| field10, 100) WHERE ycsb_key = $2", 7)
        self.assertEqual((e.tipo, e.tabela, e.colunas), ("UPDATE", "usertable",
                                                        frozenset({"field10"})))
        e = mnt.classificar("UPDATE usertable SET FIELD1=$1,FIELD9=$2 WHERE YCSB_KEY=$3", 1)
        self.assertEqual(e.colunas, frozenset({"field1", "field9"}))

    def test_update_from_alias(self):
        e = mnt.classificar("UPDATE usertable u SET field2 = v.field2 FROM "
                            "(SELECT $1::int AS k) p, usertable v WHERE u.ycsb_key "
                            "BETWEEN p.k AND p.k + 99", 3)
        self.assertEqual((e.tabela, e.colunas), ("usertable", frozenset({"field2"})))

    def test_insert_delete_upsert(self):
        self.assertEqual(mnt.classificar("INSERT INTO t (a) VALUES ($1)", 1).tipo, "INSERT")
        self.assertEqual(mnt.classificar("DELETE FROM t WHERE a = $1", 1).tipo, "DELETE")
        u = mnt.classificar("INSERT INTO t (a, b) VALUES (1, 2) ON CONFLICT (a) "
                            "DO UPDATE SET b = EXCLUDED.b", 1)
        self.assertTrue(u.upsert)
        self.assertEqual(u.colunas, frozenset({"b"}))
        self.assertIsNone(mnt.classificar("SELECT 1", 1))

    def test_sem_parser_e_conservador(self):
        e = mnt.classificar("UPDATE t SET a = $1 WHERE ((( sintaxe quebrada", 1)
        self.assertEqual(e.tipo, "UPDATE")
        self.assertIsNone(e.colunas)        # conta como se atribuísse todas


class Dependencias(unittest.TestCase):
    def test_chave_include_expressao_predicado(self):
        t, d = mnt.dependencias_do_indice(
            "CREATE INDEX x ON lineitem (l_partkey, l_suppkey) INCLUDE (l_quantity)")
        self.assertEqual((t, d), ("lineitem", frozenset({"l_partkey", "l_suppkey",
                                                         "l_quantity"})))
        _t, d = mnt.dependencias_do_indice("CREATE INDEX x ON usertable (left(field2, 3))")
        self.assertEqual(d, frozenset({"field2"}))
        _t, d = mnt.dependencias_do_indice(
            "CREATE INDEX x ON usertable USING gin (field9 gin_trgm_ops)")
        self.assertEqual(d, frozenset({"field9"}))
        _t, d = mnt.dependencias_do_indice(
            "CREATE INDEX x ON t (a) WHERE status = 'aberto'")
        self.assertEqual(d, frozenset({"a", "status"}))

    def test_com_catalogo(self):
        _t, d = mnt.dependencias_do_indice("CREATE INDEX x ON t (upper(nome))",
                                           ["id", "nome"])
        self.assertEqual(d, frozenset({"nome"}))


class Exposicao(unittest.TestCase):
    """O cenário do YCSB: GIN em field9, expressão em field2."""

    def setUp(self):
        self.indices = {1: ("usertable", frozenset({"field9"})),
                        2: ("usertable", frozenset({"field2"}))}
        self.escritas = [
            mnt.Escrita("INSERT", "usertable", None, 1000),
            mnt.Escrita("UPDATE", "usertable", frozenset({"field10"}), 500),
            mnt.Escrita("UPDATE", "usertable", frozenset({"field2"}), 200),
            mnt.Escrita("UPDATE", "usertable", None, 50),
            mnt.Escrita("DELETE", "usertable", None, 300),
            mnt.Escrita("INSERT", "outra", None, 999),
        ]

    def test_classes(self):
        e = mnt.exposicao(self.indices, [1], self.escritas)
        self.assertEqual(e.certas, 1000)          # INSERT
        self.assertEqual(e.provaveis, 50)         # UPDATE de colunas desconhecidas
        self.assertEqual(e.possiveis, 700)        # field10 e field2: elegíveis a HOT
        self.assertEqual(e.deletes, 300)          # à parte: custo do VACUUM

    def test_outro_indice_tira_o_hot(self):
        """Com o índice em field2 no conjunto, o UPDATE de field2 deixa de ser
        elegível a HOT e passa a manter TAMBÉM o GIN."""
        e = mnt.exposicao(self.indices, [1, 2], self.escritas)
        self.assertEqual(e.por_indice[1]["provaveis"], 250)
        self.assertEqual(e.por_indice[1]["possiveis"], 500)

    def test_indice_existente_tira_o_hot(self):
        e = mnt.exposicao(self.indices, [1], self.escritas,
                          existentes={"usertable": frozenset({"field10"})})
        self.assertEqual(e.provaveis, 550)

    def test_hot_observado_fica_entre_os_extremos(self):
        obs = {"usertable": {"upd": 1000, "hot_upd": 600}}
        e = mnt.exposicao(self.indices, [1], self.escritas, escrita_observada=obs)
        self.assertAlmostEqual(mnt.fracao_hot(obs, "usertable"), 0.6)
        self.assertLess(e.minimo, e.observada)
        self.assertLess(e.observada, e.maximo)
        self.assertAlmostEqual(e.observada, 1050 + 700 * 0.4)

    def test_sem_escrita_na_tabela(self):
        e = mnt.exposicao({1: ("lineitem", frozenset({"a"}))}, [1], self.escritas)
        self.assertFalse(e.tem_escrita)


class Custo(unittest.TestCase):
    def test_equilibrio(self):
        self.assertAlmostEqual(mnt.equilibrio(83_000.0, 186_221), 0.4457, places=3)
        self.assertIsNone(mnt.equilibrio(10.0, 0))

    def test_custo_parcial_nao_vira_numero(self):
        total, faltam = mnt.custo_medido({"k1": 0.2}, {1: "k1", 2: "k2"}, [1, 2],
                                         {"t": 1000.0}, {1: "t", 2: "t"})
        self.assertIsNone(total)
        self.assertEqual(faltam, [2])
        total, faltam = mnt.custo_medido({"k1": 0.2}, {1: "k1"}, [1],
                                         {"t": 1000.0}, {1: "t"})
        self.assertAlmostEqual(total, 200.0)


if __name__ == "__main__":
    unittest.main()
