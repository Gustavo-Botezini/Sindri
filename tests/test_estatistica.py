"""Testes de estatistica.py com dados sintéticos de verdade conhecida.

    ./.venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import math
import unittest
from typing import Dict, FrozenSet, List, Optional

import numpy as np

from src.index_provider import estatistica as est
from src.index_provider.estatistica import VAZIA

A = frozenset([1])
B = frozenset([2])
AB = frozenset([1, 2])


def gerar(base: Dict[int, float], efeitos: Dict[FrozenSet[int], Dict[int, float]],
          peso: Dict[int, float], sessoes_base: int = 2,
          sessoes_cfg: Optional[Dict[FrozenSet[int], int]] = None,
          sigma_alfa=0.08, sigma_eps=0.03, sigma_gama=0.0, reps=4,
          timeout: Optional[float] = None, semente=0,
          uso_parcial: Optional[Dict[FrozenSet[int], Dict[int, float]]] = None,
          pareamento: float = 1.0) -> est.Evidencia:
    """Evidência sintética: `sessoes_base` sessões só de linha de base, e
    para cada configuração c, `sessoes_cfg[c]` sessões com linha de base e
    bloco de c. efeitos[c][q] é o fator do tempo; query fora do dicionário
    não usa índice da configuração."""
    rng = np.random.default_rng(semente)
    sessoes_cfg = sessoes_cfg or {c: 2 for c in efeitos}
    blocos: List[est.Bloco] = []
    uso = {}

    def bloco(sid, c, q, t, g):
        a = rng.normal(0, sigma_alfa)
        xs = t * np.exp(g + a + rng.normal(0, sigma_eps, reps))
        if timeout and (xs >= timeout).any():
            k = int(np.argmax(xs >= timeout))
            medidos = tuple(xs[:k])
            return est.Bloco(sid, c, q, medidos + (timeout,), 1, timeout)
        return est.Bloco(sid, c, q, tuple(xs))

    n = 0
    for _ in range(sessoes_base):
        g = rng.normal(0, sigma_gama)
        for q, t in base.items():
            blocos.append(bloco(f"b{n}", VAZIA, q, t, g))
        n += 1
    for c, ef in efeitos.items():
        n_c = sessoes_cfg.get(c, 2)
        for k in range(n_c):
            sid = f"v{n}"           # validação: bloco de configuração pareado
            g = rng.normal(0, sigma_gama)
            for q, t in base.items():
                blocos.append(bloco(sid, VAZIA, q, t, g))
                # Uso parcial determinístico: as primeiras p·n sessões usam.
                p = (uso_parcial or {}).get(c, {}).get(q, 1.0)
                usou = q in ef and k < round(p * n_c)
                uso[(sid, c, q)] = usou
                if usou:
                    blocos.append(bloco(sid, c, q, t * ef[q], pareamento * g))
            n += 1
    return est.Evidencia(blocos, uso, peso)


def amostrador(ev, amostras=2000, semente=1):
    r = est.estimar_ruido(ev)
    return est.Amostrador(ev, r, amostras, semente)


def verdade_b(base, ef, peso) -> float:
    return sum(peso[q] * base[q] * (1 - ef.get(q, 1.0)) for q in base)


class BeneficioPonderado(unittest.TestCase):
    """1. B(I) = Σ f_q (T_q,0 − T_q,I), na unidade dos tempos."""

    def test_soma_ponderada_pela_frequencia(self):
        base = {1: 100.0, 2: 100.0, 3: 20.0}
        peso = {1: 10.0, 2: 1.0, 3: 1000.0}
        ef = {A: {1: 0.5, 2: 0.5}}
        ev = gerar(base, ef, peso, sigma_alfa=1e-4, sigma_eps=1e-4, sessoes_base=3)
        cmp = est.comparar(amostrador(ev), VAZIA, A)
        # 10 × 50 + 1 × 50 = 550 ms; q3 não usa o índice e não entra.
        self.assertAlmostEqual(cmp.delta_ms, 550.0, delta=550 * 0.01)
        self.assertLess(cmp.ls_ms - cmp.li_ms, 0.05 * 550)

    def test_ganho_por_execucao_nao_e_ganho_ponderado(self):
        base = {1: 100.0}
        ev = gerar(base, {A: {1: 0.5}}, {1: 10.0}, sigma_alfa=1e-4, sigma_eps=1e-4)
        e = est.comparar(amostrador(ev), VAZIA, A).por_query[0]
        self.assertAlmostEqual(e.ganho_ms, 50.0, delta=1.0)
        self.assertAlmostEqual(e.ganho_ponderado_ms, 500.0, delta=10.0)
        self.assertAlmostEqual(e.ganho_pct, 50.0, delta=1.0)

    def test_query_que_nao_usa_e_identica_a_linha_de_base(self):
        base = {1: 100.0, 2: 300.0}
        ev = gerar(base, {A: {1: 0.5}}, {1: 1.0, 2: 1.0})
        am = amostrador(ev)
        self.assertIs(am.termo(2, A), am.termo(2, VAZIA))
        e2 = [e for e in est.comparar(am, VAZIA, A).por_query if e.query == 2][0]
        self.assertEqual(e2.mudanca, "igual")
        self.assertEqual(e2.ganho_ponderado_ms, 0.0)


class BeneficioMarginal(unittest.TestCase):
    """2. ΔB = B(I_novo) − B(I_atual), comparando configurações completas."""

    def setUp(self):
        self.base = {1: 1000.0, 2: 500.0, 3: 200.0}
        self.peso = {1: 5.0, 2: 5.0, 3: 5.0}
        ef = {A: {1: 0.5}, AB: {1: 0.5, 2: 0.4}}
        self.ev = gerar(self.base, ef, self.peso, sessoes_base=3,
                        sessoes_cfg={A: 3, AB: 3}, semente=3)
        self.am = amostrador(self.ev)

    def test_aditivo_nas_estimativas_pontuais(self):
        b_a = est.comparar(self.am, VAZIA, A).delta_ms
        b_ab = est.comparar(self.am, VAZIA, AB).delta_ms
        marginal = est.comparar(self.am, A, AB).delta_ms
        self.assertAlmostEqual(marginal, b_ab - b_a, places=6)

    def test_marginal_cobre_a_verdade(self):
        cmp = est.comparar(self.am, A, AB)
        # q2 só existe em AB: 5 × 500 × 0,6 = 1500. q1 tem o mesmo fator nas
        # duas, mas são blocos diferentes: entra como ruído, não como zero.
        self.assertLess(cmp.li_ms, 1500.0)
        self.assertGreater(cmp.ls_ms, 1500.0)


class Variabilidade(unittest.TestCase):
    """3. As duas variâncias, separadas, e a moderação."""

    def test_separa_sessao_de_repeticao(self):
        base = {q: 100.0 * (q + 1) for q in range(12)}
        peso = {q: 1.0 for q in base}
        ev = gerar(base, {}, peso, sessoes_base=30, sigma_alfa=0.10,
                   sigma_eps=0.03, reps=4, semente=5)
        r = est.estimar_ruido(ev)
        alfas = [rq.sigma_alfa for rq in r.por_query.values()]
        epss = [rq.sigma_eps for rq in r.por_query.values()]
        self.assertAlmostEqual(float(np.median(alfas)), 0.10, delta=0.03)
        self.assertAlmostEqual(float(np.median(epss)), 0.03, delta=0.012)

    def test_repeticoes_nao_valem_sessoes(self):
        """Com σ_α ≫ σ_ε, 20 repetições em 2 sessões não estreitam o
        intervalo como 8 sessões de 4 repetições."""
        base = {q: 500.0 for q in range(10)}
        peso = {q: 1.0 for q in base}
        ef = {A: {0: 0.5}}

        def largura(sess, reps):
            ls = []
            for s in range(6):
                ev = gerar(base, ef, peso, sessoes_base=0, sessoes_cfg={A: sess},
                           sigma_alfa=0.15, sigma_eps=0.02, reps=reps, semente=s)
                c = est.comparar(amostrador(ev, 1000), VAZIA, A)
                ls.append(c.ls_ms - c.li_ms)
            return float(np.median(ls))

        poucas_sessoes = largura(2, 20)
        muitas_sessoes = largura(8, 4)
        self.assertGreater(poucas_sessoes, 1.5 * muitas_sessoes)

    def test_deslocamento_comum_de_sessao(self):
        base = {q: 100.0 + 10 * q for q in range(12)}
        peso = {q: 1.0 for q in base}
        ev = gerar(base, {}, peso, sessoes_base=5, sigma_alfa=0.02,
                   sigma_eps=0.01, semente=2)
        # A sessão b2 inteira 20% mais lenta.
        ev.blocos = [est.Bloco(b.sessao, b.configuracao, b.query,
                               tuple(t * 1.2 for t in b.tempos))
                     if b.sessao == "b2" else b for b in ev.blocos]
        g = est.deslocamentos(ev.blocos)
        self.assertAlmostEqual(g["b2"] - np.median(list(g.values())),
                               math.log(1.2), delta=0.03)

    def test_pareamento_parcial_e_recuperado(self):
        """Metade do deslocamento da sessão chega ao bloco de configuração:
        β̂ ≈ 0,5, como os dados do TPC-H (0,57)."""
        base = {q: 100.0 + 20 * q for q in range(12)}
        ef = {frozenset([i]): {i: 0.6, i + 6: 0.8} for i in range(6)}
        ev = gerar(base, ef, {q: 1.0 for q in base}, sessoes_base=2,
                   sessoes_cfg={c: 4 for c in ef}, sigma_alfa=0.02,
                   sigma_eps=0.01, sigma_gama=0.15, pareamento=0.5, semente=21)
        beta, ep, pontos = est.estimar_pareamento(ev.blocos, est.deslocamentos(ev.blocos))
        self.assertGreaterEqual(pontos, est.PONTOS_MINIMOS_PAREAMENTO)
        self.assertAlmostEqual(beta, 0.5, delta=max(3 * ep, 0.1))

    def test_pareamento_sem_dado_nao_ajusta(self):
        base = {q: 100.0 for q in range(10)}
        ev = gerar(base, {A: {0: 0.5}}, {q: 1.0 for q in base}, sessoes_cfg={A: 1})
        r = est.estimar_ruido(ev)
        self.assertEqual(r.beta, 0.0)

    def test_poucas_queries_nao_estimam_deslocamento(self):
        ev = gerar({1: 100.0, 2: 200.0}, {}, {1: 1.0, 2: 1.0}, sessoes_base=4)
        self.assertEqual(est.deslocamentos(ev.blocos), {})

    def test_moderacao_heterogenea_e_homogenea(self):
        rng = np.random.default_rng(0)
        # Variâncias iguais: d0 grande, todos emprestam. (Com poucos graus e
        # poucas queries d0 não é identificável; aqui há dado para isso.)
        n, gl = 200, 10
        s2 = [0.01 * rng.chisquare(gl) / gl for _ in range(n)]
        s0, d0 = est.moderar(s2, [gl] * n)
        self.assertGreater(d0, 20)
        self.assertAlmostEqual(math.sqrt(s0), 0.1, delta=0.01)
        # Variâncias muito diferentes: d0 pequeno, cada um fica com a sua.
        s2h = [v * f for v, f in zip(s2, rng.choice([0.04, 1, 25], n))]
        _s0, d0h = est.moderar(s2h, [gl] * n)
        self.assertLess(d0h, d0)
        self.assertLess(d0h, 5)

    def test_query_com_pouco_dado_empresta_da_carga(self):
        base = {q: 100.0 for q in range(10)}
        ev = gerar(base, {}, {q: 1.0 for q in base}, sessoes_base=2,
                   sigma_alfa=0.10, semente=9)
        r = est.estimar_ruido(ev)
        for rq in r.por_query.values():
            self.assertGreater(rq.gl, rq.gl_entre)    # emprestou graus


class IntervalosDeConfianca(unittest.TestCase):
    """4. Cobertura: o intervalo de 90% contém a verdade em ~90% das vezes."""

    def test_cobertura(self):
        acertos = n = 0
        for s in range(120):
            rng = np.random.default_rng(1000 + s)
            base = {q: float(rng.uniform(50, 2000)) for q in range(10)}
            peso = {q: float(rng.integers(1, 20)) for q in base}
            ef = {A: {q: float(rng.uniform(0.3, 1.2)) for q in range(4)}}
            ev = gerar(base, ef, peso, sessoes_base=2, sessoes_cfg={A: 2},
                       sigma_alfa=0.10, sigma_eps=0.03, sigma_gama=0.03,
                       semente=s)
            cmp = est.comparar(amostrador(ev, 1000, semente=s), VAZIA, A)
            verdade = verdade_b(base, ef[A], peso)
            acertos += cmp.li_ms <= verdade <= cmp.ls_ms
            n += 1
        cobertura = acertos / n
        self.assertGreater(cobertura, 0.80, f"cobertura {cobertura:.2f}")
        self.assertLess(cobertura, 0.99, f"cobertura {cobertura:.2f}")

    def test_reprodutivel_em_qualquer_ordem(self):
        base = {q: 100.0 * (q + 1) for q in range(9)}
        ev = gerar(base, {A: {0: 0.5}, B: {1: 0.7}}, {q: 1.0 for q in base})
        r = est.estimar_ruido(ev)
        am1 = est.Amostrador(ev, r, 1000, 7)
        x1 = est.comparar(am1, VAZIA, A)
        am2 = est.Amostrador(ev, r, 1000, 7)
        est.comparar(am2, VAZIA, B)           # outra ordem de consulta
        x2 = est.comparar(am2, VAZIA, A)
        self.assertEqual((x1.li_ms, x1.ls_ms), (x2.li_ms, x2.ls_ms))


class Censura(unittest.TestCase):
    """Timeout é T ≥ limite, nunca T = limite."""

    def test_linha_de_base_censurada_da_piso(self):
        base = {1: 1_000_000.0, 2: 100.0}
        ev = gerar(base, {A: {1: 0.001}}, {1: 1.0, 2: 1.0}, timeout=300_000.0,
                   sessoes_base=2)
        cmp = est.comparar(amostrador(ev), VAZIA, A)
        self.assertTrue(cmp.aberto_acima)
        self.assertFalse(cmp.aberto_abaixo)
        # o piso: 300 s − ~1 s; a verdade (999 s) está acima, como deve
        self.assertLess(cmp.li_ms, 300_000)
        self.assertEqual(est.superioridade(cmp.li_ms, cmp.ls_ms, 1000.0), est.ACEITA)
        # rejeitar por "ganho insuficiente" é impossível com o lado aberto
        self.assertNotEqual(est.superioridade(cmp.li_ms, cmp.ls_ms, 1e9), est.REJEITADA)

    def test_piso_cancela_entre_configuracoes_que_terminam(self):
        base = {1: 1_000_000.0, 2: 100.0}
        ef = {A: {1: 0.002}, AB: {1: 0.0001}}
        ev = gerar(base, ef, {1: 1.0, 2: 1.0}, timeout=300_000.0, sessoes_base=2,
                   sessoes_cfg={A: 3, AB: 3})
        cmp = est.comparar(amostrador(ev), A, AB)
        self.assertFalse(cmp.aberto_acima or cmp.aberto_abaixo)
        verdade = 1_000_000 * (0.002 - 0.0001)
        self.assertLess(cmp.li_ms, verdade)
        self.assertGreater(cmp.ls_ms, verdade)

    def test_duas_configuracoes_censuradas_nao_se_ordenam(self):
        base = {1: 1_000_000.0, 2: 100.0}
        ef = {A: {1: 0.9}, B: {1: 0.5}}            # as duas ainda ≥ 300 s
        ev = gerar(base, ef, {1: 1.0, 2: 1.0}, timeout=300_000.0)
        cmp = est.comparar(amostrador(ev), A, B)
        self.assertTrue(cmp.aberto_acima and cmp.aberto_abaixo)
        self.assertTrue(cmp.insuficiencias)
        self.assertEqual(est.ordenar(cmp.li_ms, cmp.ls_ms, 1.0), "não ordenável")

    def test_tobit_usa_a_cauda_censurada(self):
        """Grupo com parte dos blocos acima do limite: a estimativa censurada
        fica acima da que trata o limite como valor."""
        verdade = 1050.0
        rng = np.random.default_rng(4)
        blocos = []
        for k, t in enumerate(verdade * np.exp(rng.normal(0, 0.05, 10))):
            if t >= 1000:
                blocos.append(est.Bloco(f"x{k}", VAZIA, 1, (1000.0,), 1, 1000.0))
            else:
                blocos.append(est.Bloco(f"x{k}", VAZIA, 1, (float(t),)))
            blocos.append(est.Bloco(f"x{k}", VAZIA, 2, (100 * math.exp(rng.normal(0, 0.05)),)))
        ev = est.Evidencia(blocos, {}, {1: 1.0, 2: 1.0})
        am = amostrador(ev)
        ingenuo = math.exp(np.mean([b.centro for b in blocos if b.query == 1]))
        t = am.termo(1, VAZIA)
        self.assertFalse(t.piso)
        self.assertGreater(t.ponto, ingenuo)
        self.assertLess(abs(math.log(t.ponto / verdade)), abs(math.log(ingenuo / verdade)))

    def test_bloco_censurado_pela_metade(self):
        b = est.Bloco("s", VAZIA, 1, (100.0, 110.0, 300.0), 1, 300.0)
        self.assertFalse(b.censurado)     # a mediana (110) é valor
        b2 = est.Bloco("s", VAZIA, 1, (100.0, 300.0), 1, 300.0)
        self.assertTrue(b2.censurado)     # a mediana usa o censurado


class Regressoes(unittest.TestCase):
    """5. Melhora numa query e piora noutra, as duas identificadas."""

    def test_melhora_e_piora(self):
        base = {q: 200.0 for q in range(10)}
        ef = {A: {0: 0.3, 1: 2.5}}
        ev = gerar(base, ef, {q: 1.0 for q in base}, sessoes_base=2,
                   sessoes_cfg={A: 3}, semente=11)
        cmp = est.comparar(amostrador(ev), VAZIA, A)
        self.assertEqual([e.query for e in cmp.melhoram()], [0])
        self.assertEqual([e.query for e in cmp.pioram()], [1])

    def test_sessoes_que_discordam_nao_viram_regressao(self):
        """O caso da Q2 no TPC-H: −31% numa sessão do conjunto, +3% na
        outra. Com a variância da linha de base da query, a média (−14%)
        pareceria regressão; com a do próprio grupo, é indefinida."""
        base = {q: 200.0 for q in range(10)}
        ev = gerar(base, {A: {0: 1.0}}, {q: 1.0 for q in base}, sessoes_base=6,
                   sessoes_cfg={A: 2}, sigma_alfa=0.05, sigma_eps=0.02, semente=3)
        cfg = [b for b in ev.blocos if b.configuracao == A]
        fatores = [1.31, 0.97]       # as duas sessões do conjunto
        ev.blocos = [b for b in ev.blocos if b.configuracao != A] + [
            est.Bloco(b.sessao, A, b.query, tuple(200.0 * f for _ in b.tempos))
            for b, f in zip(cfg, fatores)]
        am = amostrador(ev)
        e = [x for x in est.comparar(am, VAZIA, A).por_query if x.query == 0][0]
        self.assertTrue(e.dispersa)
        self.assertEqual(e.mudanca, "indefinida")

    def test_plano_instavel_e_mistura(self):
        base = {q: 200.0 for q in range(10)}
        ev = gerar(base, {A: {0: 0.5}}, {q: 1.0 for q in base},
                   sessoes_cfg={A: 4}, uso_parcial={A: {0: 0.5}}, semente=1)
        am = amostrador(ev)
        t = am.termo(0, A)
        self.assertTrue(0 < t.fracao_uso < 1)
        e = [x for x in est.comparar(am, VAZIA, A).por_query if x.query == 0][0]
        self.assertTrue(e.plano_instavel)


class Decisoes(unittest.TestCase):
    """6 e 8. Os testes de decisão, cada um com a sua pergunta."""

    def test_superioridade(self):
        self.assertEqual(est.superioridade(12, 20, 10), est.ACEITA)
        self.assertEqual(est.superioridade(-5, 8, 10), est.REJEITADA)
        self.assertEqual(est.superioridade(5, 15, 10), est.INCONCLUSIVA)

    def test_nao_inferioridade(self):
        self.assertEqual(est.nao_inferioridade(-8, 3, 10), est.ACEITA)
        self.assertEqual(est.nao_inferioridade(-30, -12, 10), est.REJEITADA)
        self.assertEqual(est.nao_inferioridade(-15, 2, 10), est.INCONCLUSIVA)

    def test_equivalencia_so_com_o_intervalo_dentro_da_margem(self):
        self.assertTrue(est.equivalencia(-4, 6, 10))
        # sem significância NÃO é equivalência
        self.assertFalse(est.equivalencia(-40, 60, 10))
        self.assertEqual(est.ordenar(-40, 60, 10), "não ordenável")
        self.assertEqual(est.ordenar(-4, 6, 10), "equivalentes")
        # diferença detectável, mas dentro da margem: equivalentes na prática
        self.assertEqual(est.ordenar(1, 6, 10), "equivalentes")
        self.assertEqual(est.ordenar(1, 16, 10), "b melhor")
        self.assertEqual(est.ordenar(-16, -1, 10), "a melhor")

    def test_indistinguiveis_nao_sao_ordenados(self):
        """Duas configurações com a mesma verdade: quase nunca 'melhor'."""
        base = {q: 300.0 for q in range(10)}
        falsos = 0
        for s in range(20):
            ev = gerar(base, {A: {0: 0.5}, B: {0: 0.5}}, {q: 1.0 for q in base},
                       sessoes_cfg={A: 2, B: 2}, semente=200 + s)
            c = est.comparar(amostrador(ev, 1000, s), A, B)
            falsos += est.ordenar(c.li_ms, c.ls_ms, 5.0) in ("a melhor", "b melhor")
        self.assertLessEqual(falsos, 4)

    def test_equivalencia_com_sessoes_suficientes(self):
        base = {q: 300.0 for q in range(10)}
        ev = gerar(base, {A: {0: 0.5}, B: {0: 0.5}}, {q: 1.0 for q in base},
                   sessoes_cfg={A: 8, B: 8}, sigma_alfa=0.02, sigma_eps=0.01)
        c = est.comparar(amostrador(ev), A, B)
        self.assertEqual(est.ordenar(c.li_ms, c.ls_ms, 30.0), "equivalentes")


class Pareto(unittest.TestCase):
    """7 e 10. Dominância, com tamanhos diferentes."""

    def test_fronteira_pontual(self):
        p = [est.Ponto(frozenset([1]), 100, 10), est.Ponto(frozenset([2]), 90, 50),
             est.Ponto(frozenset([3]), 150, 60), est.Ponto(frozenset([4]), 150, 300)]
        fr = [x.chave for x in est.fronteira_pareto(p)]
        self.assertEqual(fr, [frozenset([1]), frozenset([3])])
        self.assertTrue(est.domina(p[0], p[1]))     # mais benefício, menos espaço
        self.assertTrue(est.domina(p[2], p[3]))     # mesmo benefício, 5× o espaço

    def test_mais_espaco_sem_ganho_e_dominada_com_evidencia(self):
        base = {q: 400.0 for q in range(10)}
        ef = {A: {0: 0.4}, AB: {0: 0.4, 1: 1.0}}   # o índice 2 é usado e não ajuda
        ev = gerar(base, ef, {q: 1.0 for q in base}, sessoes_cfg={A: 3, AB: 3},
                   sigma_alfa=0.05)
        am = amostrador(ev)
        pa = est.Ponto(A, est.comparar(am, VAZIA, A).delta_ms, 40.0)
        pab = est.Ponto(AB, est.comparar(am, VAZIA, AB).delta_ms, 290.0)
        cmp = est.comparar(am, AB, A)
        self.assertTrue(est.dominada_com_evidencia(pa, pab, cmp, margem=100.0))

    def test_maior_com_ganho_nao_e_dominada(self):
        base = {q: 400.0 for q in range(10)}
        ef = {A: {0: 0.4}, AB: {0: 0.4, 1: 0.2}}
        ev = gerar(base, ef, {q: 1.0 for q in base}, sessoes_cfg={A: 3, AB: 3},
                   sigma_alfa=0.05)
        am = amostrador(ev)
        pa = est.Ponto(A, est.comparar(am, VAZIA, A).delta_ms, 40.0)
        pab = est.Ponto(AB, est.comparar(am, VAZIA, AB).delta_ms, 290.0)
        self.assertFalse(est.dominada_com_evidencia(pa, pab, est.comparar(am, AB, A), 10.0))
        self.assertIn(AB, [x.chave for x in est.fronteira_pareto([pa, pab])])


class MedicoesInsuficientes(unittest.TestCase):
    """9. Sem bloco repetido, nada é decidido."""

    def test_uma_sessao_so(self):
        base = {q: 100.0 for q in range(10)}
        ev = gerar(base, {A: {0: 0.01}}, {q: 1.0 for q in base}, sessoes_base=0,
                   sessoes_cfg={A: 1})
        r = est.estimar_ruido(ev)
        self.assertFalse(r.suficiente)
        self.assertTrue(r.avisos)
        with self.assertRaises(ValueError):
            est.comparar(est.Amostrador(ev, r), VAZIA, A)

    def test_flutuacao_cresce_com_o_ruido(self):
        base = {q: 1000.0 for q in range(10)}
        quieto = amostrador(gerar(base, {}, {q: 1.0 for q in base}, 4, sigma_alfa=0.01))
        ruidoso = amostrador(gerar(base, {}, {q: 1.0 for q in base}, 4, sigma_alfa=0.2))
        self.assertGreater(est.flutuacao_sessao(ruidoso, VAZIA),
                           5 * est.flutuacao_sessao(quieto, VAZIA))


if __name__ == "__main__":
    unittest.main()
