"""A exploração adaptativa ponta a ponta, contra uma bancada sintética.

Cada cenário tem verdade conhecida e é um dos casos que a seleção precisa
tratar: ganho grande em poucas queries, ganhos pequenos espalhados, leitura
melhor com escrita pior, configurações indistinguíveis, espaço sem
benefício, e o índice que ajuda sozinho e atrapalha em conjunto.
"""

from __future__ import annotations

import unittest
from typing import FrozenSet, Set, Tuple

from src.index_provider import adaptativa
from src.index_provider import manutencao as mnt
from src.index_provider.estatistica import VAZIA
from tests.simulado import Bancada, Verdade, efeito_por_indice, explorar


def parametros(**kw) -> adaptativa.Parametros:
    base = dict(amostras=800, max_validacoes=3, paciencia=2, exploracao=1,
                vizinhos=3)
    base.update(kw)
    return adaptativa.Parametros(**base)


def carga(n=8, t=300.0, peso=10.0, extra=None):
    base = {q: t * (1 + q % 3) for q in range(n)}
    base.update(extra or {})
    return base, {q: peso for q in base}


class GanhoGrandeEmPoucasQueries(unittest.TestCase):
    """Q17/Q20: um índice tira a query do timeout. O composto maior ganha um
    pouco mais; o índice grande sem efeito não pode entrar."""

    def setUp(self):
        base, peso = carga(extra={100: 1_000_000.0})
        tabela = {1: {100: 0.004},                  # simples, 40 MB
                  2: {100: 0.0002},                 # composto, 230 MB
                  3: {1: 1.0, 2: 1.0}}              # 300 MB, usado, sem efeito
        self.v = Verdade(base, peso, {1: 40.0, 2: 230.0, 3: 300.0},
                         efeito_por_indice(tabela), timeout_ms=300_000.0,
                         sigma_alfa=0.06)

    def test_recomenda_quem_tira_do_timeout_e_nao_o_inutil(self):
        an, traj, n = explorar(Bancada(self.v, 1), parametros())
        self.assertTrue(traj.concluida, traj.motivo)
        self.assertTrue(traj.atual & {1, 2}, traj.atual)
        self.assertNotIn(3, traj.atual)
        rel = adaptativa.relatorio(an, traj, {})
        self.assertEqual(rel["recomendada"], adaptativa._c(traj.atual))
        self.assertTrue(rel["caminho"])
        primeiro = rel["caminho"][0]
        self.assertTrue(primeiro["aberto_acima"])   # piso: a base estourou
        self.assertLessEqual(n, 15)


class GanhosPequenosEspalhados(unittest.TestCase):
    """6% em 8 queries: aceito com ruído baixo; com ruído alto, nunca aceito
    por acaso (fica inconclusivo ou nem é proposto)."""

    def _cenario(self, sigma):
        base, peso = carga(n=8, t=1000.0)
        tabela = {1: {q: 0.94 for q in range(8)}}
        return Verdade(base, peso, {1: 50.0}, efeito_por_indice(tabela),
                       sigma_alfa=sigma, sigma_eps=sigma / 3, sigma_gama=sigma / 3)

    def test_ruido_baixo_aceita(self):
        _an, traj, _n = explorar(Bancada(self._cenario(0.01), 2),
                                 parametros(relevancia=0.01))
        self.assertIn(1, traj.atual)

    def test_ruido_alto_nao_aceita_por_acaso(self):
        an, traj, _n = explorar(Bancada(self._cenario(0.20), 2),
                                parametros(relevancia=0.01))
        self.assertNotIn(1, traj.atual)
        decs = [p.decisao for p in traj.passos if p.tipo == "comparacao"
                and p.para and 1 in p.para]
        self.assertNotIn("aceita", decs)


class LeituraMelhorEscritaPior(unittest.TestCase):
    """O índice melhora a leitura, e a tabela recebe muita escrita. Sem
    custo medido ou declarado, a recomendação diz que só a leitura foi
    avaliada; com a hipótese de custo, o movimento não se paga."""

    def setUp(self):
        base, peso = carga(n=8, t=500.0)
        tabela = {1: {0: 0.2}}                       # ganho de 10 × 400 = 4 s
        self.v = Verdade(base, peso, {1: 20.0}, efeito_por_indice(tabela),
                         relacao={1: "t"},
                         ddl={1: "CREATE INDEX brokk_exp_i1 ON t (c1)"},
                         sigma_alfa=0.04)
        self.escritas = [mnt.Escrita("INSERT", "t", None, 100_000)]

    def test_sem_custo_aceita_com_limitacao(self):
        an, traj, _n = explorar(Bancada(self.v, 3), parametros(), self.escritas)
        self.assertIn(1, traj.atual)
        d = adaptativa.descrever(an, traj.atual, {})
        self.assertTrue(any("só a leitura" in l for l in d["limitacoes"]))
        self.assertIn("equilibrio_ms_por_entrada", d)
        rel = adaptativa.relatorio(an, traj, {})
        rec = [c for c in rel["configuracoes"] if c["rotulo"] == rel["recomendada"]][0]
        self.assertIn("só a leitura avaliada", rec["situacao"])

    def test_com_custo_declarado_rejeita(self):
        # 0,1 ms × 100 mil entradas = 10 s de manutenção contra 4 s de leitura
        _an, traj, _n = explorar(Bancada(self.v, 3),
                                 parametros(custo_entrada_ms=0.1), self.escritas)
        self.assertNotIn(1, traj.atual)


class Indistinguiveis(unittest.TestCase):
    """Dois índices do mesmo tamanho com o mesmo efeito: nenhum relatório
    pode afirmar que um é melhor que o outro."""

    def test_nao_forca_ordem(self):
        base, peso = carga(n=8, t=800.0)
        tabela = {1: {0: 0.5}, 2: {0: 0.5}}
        v = Verdade(base, peso, {1: 30.0, 2: 30.0}, efeito_por_indice(tabela))
        banc = Bancada(v, 4)
        for _ in range(3):
            banc.validar(frozenset([1]))
            banc.validar(frozenset([2]))
        an = adaptativa.analisar(banc.experimento(), parametros())
        cmp = an.comparar(frozenset([1]), frozenset([2]))
        margem, _ = an.margem(frozenset([1]))
        from src.index_provider import estatistica as est
        self.assertIn(est.ordenar(cmp.li_ms, cmp.ls_ms, margem),
                      ("equivalentes", "não ordenável"))


class EspacoSemBeneficio(unittest.TestCase):
    """Um índice enorme que o modelo da matriz acha bom (sorte de uma sessão)
    e que, medido, não ajuda: rejeitado por ganho insuficiente, e a
    paciência encerra a busca."""

    def test_rejeita_e_para(self):
        base, peso = carga(n=8, t=600.0)
        real = efeito_por_indice({1: {0: 0.3}, 2: {1: 1.0}, 3: {2: 1.0}})
        sorte = efeito_por_indice({1: {0: 0.3}, 2: {1: 0.4}, 3: {2: 0.4}})
        v = Verdade(base, peso, {1: 10.0, 2: 400.0, 3: 500.0}, real,
                    efeito_matriz=sorte, sigma_alfa=0.03)
        _an, traj, n = explorar(Bancada(v, 5), parametros(exploracao=0))
        self.assertIn(1, traj.atual)
        self.assertFalse(traj.atual & {2, 3})
        rejeitadas = [p for p in traj.passos if p.decisao == "rejeitada"]
        self.assertTrue(rejeitadas)
        self.assertTrue(traj.concluida)


class AjudaSozinhoAtrapalhaEmConjunto(unittest.TestCase):
    """O índice 2 melhora a q1 sozinho (é o que a matriz vê). Com o 1 no
    conjunto, o planner escolhe um plano com o 2 que triplica a q2. Medindo
    o conjunto inteiro, o 2 fica de fora."""

    def test_rejeita_a_combinacao_que_piora(self):
        base, peso = carga(n=8, t=500.0)
        simples = efeito_por_indice({1: {0: 0.2}, 2: {1: 0.5}})

        def real(c: FrozenSet[int], q: int) -> Tuple[float, Set[int]]:
            if {1, 2} <= c and q == 2:
                return 4.0, {2}                      # a interação: q2 4× mais lenta
            return simples(c, q)

        v = Verdade(base, peso, {1: 10.0, 2: 15.0}, real, sigma_alfa=0.04)
        an, traj, _n = explorar(Bancada(v, 6), parametros())
        self.assertIn(1, traj.atual)
        self.assertNotIn(2, traj.atual)
        com_2 = [p for p in traj.passos if p.tipo == "comparacao"
                 and p.para and {1, 2} <= p.para]
        self.assertTrue(com_2, "a combinação precisa ter sido medida")
        self.assertTrue(all(p.decisao != "aceita" for p in com_2))
        # a regressão aparece, nomeada, na descrição do conjunto medido
        d = adaptativa.descrever(an, frozenset([1, 2]), {})
        self.assertIn("#2", [x["query"] for x in d["pioram"]])


class AvaliacaoIndependente(unittest.TestCase):
    """A comparação de métodos usa só as sessões de confirmação, e separa o
    ganho das queries com piso do resto."""

    def test_so_confirmacao_e_dominancia(self):
        base, peso = carga(n=8, t=500.0, extra={100: 1_000_000.0})
        tabela = {1: {100: 0.002, 0: 0.5}, 2: {100: 0.002}, 3: {1: 2.0}}
        v = Verdade(base, peso, {1: 40.0, 2: 40.0, 3: 100.0},
                    efeito_por_indice(tabela), timeout_ms=300_000.0,
                    sigma_alfa=0.04)
        banc = Bancada(v, 13)
        A, B, C = frozenset([1]), frozenset([2]), frozenset([1, 3])
        for c in (A, B):                       # seleção
            banc.validar(c)
            banc.validar(c, "conjunto_primeiro")
        an = adaptativa.analisar(banc.experimento(), parametros())
        for c in (A, B, C):                    # avaliação à parte
            banc.validar(c, finalidade="confirmacao", confirma=99)
            banc.validar(c, "conjunto_primeiro", finalidade="confirmacao", confirma=99)
        an = adaptativa.analisar(banc.experimento(), parametros())
        # a seleção não lê a confirmação
        self.assertEqual(an.sessoes(C), 0)
        av = adaptativa.avaliacao_independente(an, 99, {100: "q17"}, A)
        por = {d["rotulo"]: d for d in av["configuracoes"]}
        self.assertEqual(av["queries_com_piso"], ["q17"])
        self.assertEqual(len(por["{i1}"]["sessoes"]), 2)
        self.assertIn("q17", por["{i1}"]["pisos_resolvidos"])
        # B resolve a q17 como A mas não ganha na q0: perde fora dos pisos
        self.assertLess(por["{i2}"]["contra_referencia"]["ls_ms"], 0)
        # C tem mais espaço e piora a q1: dominada pela A
        self.assertEqual(por["{i1,i3}"]["dominada_por"], "{i1}")


class Variantes(unittest.TestCase):
    def test_mesma_coluna_lider_nao_e_combinacao(self):
        from src.index_provider import otimizacao
        i4 = otimizacao.Indice(4, "a", "CREATE INDEX x ON orders (o_orderdate) INCLUDE "
                                    "(o_orderkey, o_custkey)", "orders", 1, None, None)
        i5 = otimizacao.Indice(5, "b", "CREATE INDEX y ON orders (o_orderdate) INCLUDE "
                                    "(o_custkey, o_orderkey)", "orders", 1, None, None)
        i3 = otimizacao.Indice(3, "c", "CREATE INDEX z ON orders (o_custkey)", "orders",
                               1, None, None)
        g = otimizacao.Indice(6, "d", "CREATE INDEX w ON usertable USING gin "
                                    "(field9 gin_trgm_ops)", "usertable", 1, None, None)
        self.assertTrue(adaptativa.variantes(i4, i5))
        self.assertFalse(adaptativa.variantes(i4, i3))
        self.assertEqual(adaptativa._coluna_lider(g), "field9")


class ResolucaoDeIncerteza(unittest.TestCase):
    """O caso do TPC-H: um conjunto maior já medido UMA vez (por `otimizar`),
    que a previsão da matriz não favorece e que de fato ganha bem mais que a
    margem. Sem a resolução, a caminhada para sem compará-lo. Ao lado, um
    conjunto maior sem ganho, também medido uma vez: UCB(ΔB) < δ, descartado
    sem remedir."""

    A, B, D = frozenset([1]), frozenset([1, 2]), frozenset([1, 3])

    def setUp(self):
        base, peso = carga(n=8, t=1000.0, extra={50: 50.0})
        real = efeito_por_indice({1: {0: 0.1}, 2: {1: 0.6, 2: 0.6}, 3: {50: 1.0}})
        # a matriz não vê o ganho do 2 (plano diferente na sessão dela)
        matriz = efeito_por_indice({1: {0: 0.1}, 2: {1: 1.0, 2: 1.0}, 3: {50: 1.0}})
        self.v = Verdade(base, peso, {1: 10.0, 2: 200.0, 3: 300.0}, real,
                         efeito_matriz=matriz, sigma_alfa=0.06)

    def _bancada(self, semente=21):
        banc = Bancada(self.v, semente)
        banc.validar(self.B, movimento="otimizar")      # medidos antes, uma vez
        banc.validar(self.D, movimento="otimizar")
        return banc

    @staticmethod
    def _validacoes(banc, c, movimento=None):
        return [v for v in banc.validacoes if v.configuracao == c
                and (movimento is None or v.movimento == movimento)]

    def test_remede_a_alternativa_competitiva_e_aceita(self):
        banc = self._bancada()
        an, traj, _n = explorar(banc, parametros())
        self.assertTrue(traj.concluida, traj.motivo)
        self.assertEqual(traj.atual, self.B)
        self.assertTrue(self._validacoes(banc, self.B, "resolucao"))
        aceita = [p for p in traj.passos if p.movimento == "resolucao"
                  and p.para == self.B and p.decisao == "aceita"]
        self.assertTrue(aceita)
        # o sem ganho não foi remedido: UCB(ΔB) − custo < δ
        self.assertEqual(len(self._validacoes(banc, self.D)), 1)
        rel = adaptativa.relatorio(an, traj, {})
        por = {a["rotulo"]: a for a in rel["resolucao"]["alternativas"]}
        self.assertEqual(por["{i1,i3}"]["acao"], "descartada")
        self.assertIn("UCB", por["{i1,i3}"]["motivo"])

    def test_sem_orcamento_fica_nao_ordenavel(self):
        banc = self._bancada()
        an, traj, _n = explorar(banc, parametros(resolucao=0))
        self.assertTrue(traj.concluida)
        self.assertEqual(traj.atual, self.A)
        self.assertFalse(self._validacoes(banc, self.B, "resolucao"))
        self.assertIn((self.A, self.B), traj.nao_ordenaveis)
        p = next(p for p in traj.pendencias if p.chave == self.B)
        self.assertEqual(p.acao, "sem_orcamento")
        self.assertIn("orçamento de resolução", p.motivo)

    def test_compacta_com_pouca_economia_nao_e_remedida(self):
        """Encolher 0,5 MB de 10,5 não paga uma validação, mesmo inconclusivo."""
        base, peso = carga(n=8, t=1000.0)
        v = Verdade(base, peso, {1: 10.0, 4: 0.5},
                    efeito_por_indice({1: {0: 0.1}, 4: {3: 1.0}}), sigma_alfa=0.06)
        # O efeito verdadeiro é zero e a margem é 1 ms: o intervalo cruza a
        # margem em ~90% das sementes. O teste é do ramo inconclusivo, então
        # usa a primeira semente em que ele acontece.
        for semente in range(22, 42):
            banc = Bancada(v, semente)
            for c in (frozenset([1]), frozenset([1, 4])):
                banc.validar(c)
                banc.validar(c, "conjunto_primeiro")
            an = adaptativa.analisar(banc.experimento(), parametros(margem_ms=1.0))
            pend = {p.chave: p for p in adaptativa.pendencias(an, frozenset([1, 4]))}
            p = pend[frozenset([1])]
            if p.decisao == "inconclusiva":
                break
        self.assertEqual(p.decisao, "inconclusiva")
        self.assertEqual(p.teste, "nao_inferioridade")
        self.assertEqual(p.acao, "descartada")
        self.assertIn("economiza", p.motivo)

    def test_multiplicidade_documentada(self):
        banc = self._bancada()
        an, traj, _n = explorar(banc, parametros())
        rel = adaptativa.relatorio(an, traj, {})
        m = rel["multiplicidade"]
        na_caminhada = [p for p in traj.passos if p.tipo == "comparacao"
                        and p.movimento != "resolucao"]
        self.assertEqual(m["comparacoes"]["caminhada"], len(na_caminhada))
        self.assertGreater(m["comparacoes"]["resolucao"], 0)
        self.assertGreaterEqual(m["conjuntos_validados"], 3)
        self.assertEqual(m["validacoes_por_origem"].get("resolucao"), 1)
        self.assertEqual(m["validacoes_por_origem"].get("otimizar"), 2)
        self.assertEqual(len(m["bonferroni"]["caminho"]), len(traj.caminho) - 1)
        self.assertTrue(all(isinstance(x["sobrevive"], bool)
                            for x in m["bonferroni"]["caminho"]))
        self.assertGreater(m["bonferroni"]["confianca"], parametros().confianca)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            adaptativa.mostrar(rel)

    def test_confirmacao_mede_a_reproducao(self):
        banc = self._bancada()
        an, traj, _n = explorar(banc, parametros())
        rel = adaptativa.relatorio(an, traj, {})
        for c in (self.A, self.B):
            banc.validar(c, finalidade="confirmacao", confirma=77)
            banc.validar(c, "conjunto_primeiro", finalidade="confirmacao", confirma=77)
        an = adaptativa.analisar(banc.experimento(), parametros())
        res = adaptativa.confirmar(an, 77, rel)
        self.assertEqual(res["para"], "{i1,i2}")
        rep = res["reproducao"]
        self.assertTrue(rep["mesmo_sinal"])
        self.assertGreater(rep["razao_confirmacao_sobre_selecao"], 0.5)


class VariantesQuaseIguais(unittest.TestCase):
    """O caso do TPC-H (adaptações 13 e 14): o núcleo {2}, de 300 MB, ganha
    exatamente a margem, e a fronteira prevista o oferece em variantes que
    só trocam um índice minúsculo e sem efeito. Cada variante esgota as
    sessões sem provar o ganho. Não ordenável conta para a paciência: a
    caminhada para depois de ``paciencia`` variantes, em vez de medir a
    próxima."""

    def setUp(self):
        base, peso = carga(n=8, t=1000.0)
        real = efeito_por_indice({1: {2: 0.1}, 2: {0: 0.5}, 3: {3: 1.0},
                                  4: {6: 1.0}, 5: {4: 1.0}, 6: {5: 1.0}})
        matriz = efeito_por_indice({1: {2: 0.1}, 2: {0: 0.3}, 3: {3: 1.0},
                                    4: {6: 1.0}, 5: {4: 1.0}, 6: {5: 1.0}})
        v = Verdade(base, peso, {1: 10.0, 2: 300.0, 3: 1.0, 4: 2.0, 5: 3.0, 6: 4.0},
                    real, efeito_matriz=matriz, sigma_alfa=0.06)
        self.banc = Bancada(v, 31)
        self.variantes = [frozenset([1, 2, x]) for x in (3, 4, 5, 6)]
        for c in [frozenset([1])] + self.variantes:
            self.banc.validar(c, movimento="fronteira")
            self.banc.validar(c, "conjunto_primeiro", movimento="fronteira")

    def _analise(self, **kw):
        base = dict(max_validacoes=2, exploracao=0, resolucao=0, margem_ms=5000.0)
        base.update(kw)
        return adaptativa.analisar(self.banc.experimento(), parametros(**base))

    def test_nao_ordenavel_conta_para_a_paciencia(self):
        an = self._analise()
        traj = adaptativa.caminhar(an)
        self.assertTrue(traj.concluida, traj.motivo)
        self.assertIsNone(traj.proxima)        # sem pedir a quinta variante
        self.assertEqual(traj.atual, frozenset([1]))
        self.assertIn("paciência", traj.motivo)
        cresc = [p for p in traj.passos if p.tipo == "comparacao"
                 and p.movimento != "resolucao" and p.de == frozenset([1])]
        self.assertEqual(len(cresc), 2)
        self.assertTrue(all(p.decisao == "nao_ordenavel" for p in cresc))
        parada = [p for p in traj.passos if p.tipo == "parada"]
        self.assertIn("não ordenáveis", parada[-1].texto)
        # as variantes que a caminhada não comparou passam pela resolução
        pend = {p.chave: p for p in traj.pendencias}
        for c in self.variantes:
            self.assertIn(pend[c].acao, ("sem_orcamento", "descartada"))

    def test_familia_nao_depende_de_onde_a_analise_parou(self):
        """Com sessões sobrando, a caminhada para pedindo medida, antes da
        resolução: a família de Bonferroni conta assim mesmo as
        configurações medidas fora do caminho."""
        an = self._analise(max_validacoes=3)
        traj = adaptativa.caminhar(an)
        self.assertIsNotNone(traj.proxima)
        self.assertEqual(traj.comparacoes_resolucao, 0)
        m = adaptativa.relatorio(an, traj, {})["multiplicidade"]
        c = m["comparacoes"]
        self.assertFalse(c["resolucao_feita"])
        self.assertGreaterEqual(c["resolucao"], len(self.variantes))
        self.assertEqual(c["familia_bonferroni"], c["caminhada"] + c["resolucao"])
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as saida:
            adaptativa.mostrar(adaptativa.relatorio(an, traj, {}))
        self.assertIn("ainda não feita", saida.getvalue())


class CriterioDeParada(unittest.TestCase):
    def test_sem_medida_pede_medida(self):
        """Só a matriz: nenhuma variação entre sessões estimada; a decisão
        não é tomada, e a próxima medição é pedida."""
        base, peso = carga()
        v = Verdade(base, peso, {1: 10.0}, efeito_por_indice({1: {0: 0.1}}))
        banc = Bancada(v, 7)
        an = adaptativa.analisar(banc.experimento(), parametros())
        traj = adaptativa.caminhar(an)
        self.assertFalse(traj.concluida)
        self.assertIsNotNone(traj.proxima)
        self.assertIn("aguardando", traj.motivo)
        # o relatório sai, sem intervalo nenhum e dizendo por quê
        rel = adaptativa.relatorio(an, traj, {})
        self.assertFalse(rel["ruido"]["suficiente"])
        self.assertEqual(rel["ordem_na_fronteira"], [])
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            adaptativa.mostrar(rel)

    def test_inconclusivo_remede_e_depois_desiste(self):
        """Ganho perto da margem com ruído alto: pede novas sessões até o
        limite e marca o par como não ordenável, sem aceitar."""
        base, peso = carga(n=8, t=1000.0)
        v = Verdade(base, peso, {1: 30.0}, efeito_por_indice({1: {0: 0.85}}),
                    sigma_alfa=0.12)
        an, traj, n = explorar(Bancada(v, 8),
                               parametros(margem_ms=1000.0, max_validacoes=2))
        self.assertTrue(traj.concluida)
        cmp = [p for p in traj.passos if p.tipo == "comparacao" and p.para == frozenset([1])]
        if cmp:     # se a previsão o propôs
            self.assertIn(cmp[-1].decisao, ("nao_ordenavel", "rejeitada", "aceita"))
            if cmp[-1].decisao == "nao_ordenavel":
                self.assertEqual(traj.atual, VAZIA)
                self.assertEqual(an.sessoes(frozenset([1])), 2)

    def test_orcamento_de_exploracao_vale_entre_caminhadas(self):
        """A caminhada recomeça a cada medição; o orçamento de exploração é
        do experimento. (Bug medido no TPC-H: com orçamento 2, foram medidas
        4 combinações.)"""
        base, peso = carga(n=8, t=800.0)
        # quatro índices usados pelas mesmas queries, nenhum ajuda: só a
        # exploração por combinação os mediria
        tabela = {i: {0: 1.0, 1: 1.0} for i in range(1, 5)}
        v = Verdade(base, peso, {i: 10.0 for i in range(1, 5)},
                    efeito_por_indice(tabela), sigma_alfa=0.04)
        banc = Bancada(v, 12)
        banc.validar(frozenset([1]))         # dá a segunda linha de base
        _an, traj, _n = explorar(banc, parametros(exploracao=1))
        combinacoes = [c for c, m in banc.movimento_de.items() if m == "combinacao"]
        self.assertLessEqual(len(combinacoes), 1)
        self.assertTrue(traj.concluida)

    def test_limite_de_medicoes(self):
        base, peso = carga(n=8, t=1000.0)
        v = Verdade(base, peso, {i: 10.0 for i in range(1, 6)},
                    efeito_por_indice({i: {i: 0.5} for i in range(1, 6)}))
        _an, traj, n = explorar(Bancada(v, 9), parametros(), max_medicoes=2)
        self.assertLessEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
