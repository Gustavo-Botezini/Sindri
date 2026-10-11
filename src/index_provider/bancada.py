#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A bancada: cria o índice de verdade, remede o cluster, derruba o índice.

É o que separa este modo do recomendador clássico. Lá a saída é uma opinião
com números de entrada bons e nenhum número de saída; aqui a opinião vira
``CREATE INDEX``, as queries do cluster rodam de novo com
``EXPLAIN (ANALYZE, BUFFERS)``, e o ganho — ou a perda — de cada uma fica
gravado.

    checagens -> ANALYZE -> baseline -> CREATE INDEX -> ANALYZE -> replay -> DROP

Quatro cuidados, cada um por um motivo concreto.

**Não contaminar o próprio corpus.** O banco observado sobe com
``log_min_duration_statement=0`` e ``auto_explain.log_min_duration=0`` (ver o
``command:`` do serviço postgres no docker-compose.yml). Cada replay da
bancada viraria registro de log e, na passada seguinte do coletor, entraria no
corpus como se fosse carga — o mesmo problema que
``initdb-monitoring/01-schema.sql`` descreve para o banco de monitoramento. A
sessão desliga os dois e se marca com ``application_name``, que é a rede de
segurança: se algo escapar, dá para achar e descartar. Desligar o auto_explain
tem um segundo efeito, bem-vindo: sem ``log_timing`` por cima do nosso próprio
``ANALYZE``, o tempo medido é o da query e não o da instrumentação dobrada.

**Parâmetros por PREPARE/EXECUTE, com os valores como literais.** Substituir
``$1`` no texto da query na unha quebraria em query com ``%`` (o corpus tem
``like 'PROMO%'``) ou com ``$`` dentro de literal; quem deve resolver os ``$n``
é o Postgres. Mas passar os valores como parâmetros do ``EXECUTE`` pelo
protocolo estendido **não funciona** — medido: as 27 queries do corpus falham
com ``could not determine data type of parameter $1``, porque o Postgres não
infere tipo para parâmetro em posição de argumento de EXECUTE. Então o
``PREPARE`` declara os tipos a partir do corpo da query (isso ele faz bem), e
os valores entram no ``EXECUTE`` como literais montados por
``psycopg.sql.Literal``, que cuida do escape. O literal chega como ``unknown``
e o Postgres o converte para o tipo que o PREPARE já declarou.

E ``plan_cache_mode = force_custom_plan`` porque, sem ele, a partir da sexta
execução da mesma prepared statement o plano genérico entra e a medição passa
a ser de outra coisa.

**A primeira repetição é descartada.** Ela paga o cache frio. Com 3
repetições sobram 2, e é por isso que ``experimento_resultado`` usa mediana.

**Timeout é piso, não erro.** Quem estoura o ``statement_timeout`` é gravado
com ``tempo_ms`` igual ao limite e ``censurado = true``, e a fase para de
repetir ali. Como erro, a query sumiria do resultado exatamente quando o
índice faz mais diferença; com o piso, o ganho sai como "pelo menos".

**DROP em try/finally.** Um Ctrl+C no meio não pode deixar um índice de
300 MB para trás. O prefixo ``brokk_exp_`` e o subcomando ``limpar`` são a
segunda linha de defesa.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from psycopg import sql

from .candidatos import PREFIXO, nome_do_ddl
from .corpus import Query, substituir_parametros

NOME_APP = "brokk_bancada"

# 5, e não 3, porque a primeira é descartada e a mediana de 2 amostras é
# frágil. Medido: o mesmo experimento repetido com 3 repetições deu ganho
# atribuível de 124,6 ms numa corrida e 507,5 ms na seguinte. Com 4 amostras
# úteis a mediana para de girar. Custa mais tempo de bancada, e vale.
REPETICOES = 5
TIMEOUT_MS = 300_000
PREPARADA = "brokk_bancada_q"


SQL_GRAVAR_MEDIDA = """
INSERT INTO monitoring.experimento_medida
       (experimento_id, query_ref, fase, repeticao, tempo_ms, custo_estimado,
        linhas_reais, shared_hit, shared_read, usou_indice, node_type_topo, erro,
        censurado)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (experimento_id, query_ref, fase, repeticao) DO UPDATE
   SET tempo_ms = EXCLUDED.tempo_ms, custo_estimado = EXCLUDED.custo_estimado,
       linhas_reais = EXCLUDED.linhas_reais, shared_hit = EXCLUDED.shared_hit,
       shared_read = EXCLUDED.shared_read, usou_indice = EXCLUDED.usou_indice,
       node_type_topo = EXCLUDED.node_type_topo, erro = EXCLUDED.erro,
       censurado = EXCLUDED.censurado
"""

SQL_FECHAR = """
UPDATE monitoring.experimento
   SET estado = %s, erro = %s, build_ms = %s, tamanho_bytes = %s,
       medido_em = now()
 WHERE id = %s
"""

# Sessão de outro cliente rodando carga. O application_name do BenchBase vem
# da config XML; qualquer backend ativo que não seja esta bancada conta.
SQL_OCUPADO = """
SELECT application_name, count(*) AS sessoes
  FROM pg_stat_activity
 WHERE backend_type = 'client backend'
   AND state = 'active'
   AND pid <> pg_backend_pid()
   AND application_name <> %s
 GROUP BY 1
 ORDER BY 2 DESC
"""

SQL_INDICES_EXP = """
SELECT c.relname
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'i' AND n.nspname = 'public'
   AND left(c.relname, %s) = %s
 ORDER BY c.relname
"""


@dataclass
class Medida:
    query_ref: int
    fase: str
    repeticao: int
    tempo_ms: Optional[float] = None
    custo_estimado: Optional[float] = None
    linhas_reais: Optional[float] = None
    shared_hit: Optional[int] = None
    shared_read: Optional[int] = None
    usou_indice: Optional[bool] = None
    node_type_topo: Optional[str] = None
    erro: Optional[str] = None
    censurado: bool = False


def estourou_timeout(erro: Exception) -> bool:
    """O statement_timeout desta sessão, e não um cancelamento qualquer."""
    return (getattr(erro, "sqlstate", None) == "57014"
            and "statement timeout" in str(erro))


# --------------------------------------------------------------------------
# Leitura do plano
# --------------------------------------------------------------------------

def _percorrer(no: dict):
    yield no
    for filho in no.get("Plans", []) or []:
        yield from _percorrer(filho)


def ler_explain(saida: Any, indice: Optional[str]) -> Dict[str, Any]:
    """Extrai da saída FORMAT JSON o que a medição guarda.

    `Execution Time` e não `Actual Total Time` do nó de topo: o primeiro
    inclui o tempo de materializar o resultado e é o que o cliente sente.
    Buffers do nó de topo são cumulativos dos filhos, então servem como total.
    """
    raiz = saida[0] if isinstance(saida, list) else saida
    plano = raiz.get("Plan", {})
    usou = None
    if indice:
        usou = any(no.get("Index Name") == indice for no in _percorrer(plano))
    return {
        "tempo_ms": raiz.get("Execution Time"),
        "custo_estimado": plano.get("Total Cost"),
        "linhas_reais": plano.get("Actual Rows"),
        "shared_hit": plano.get("Shared Hit Blocks"),
        "shared_read": plano.get("Shared Read Blocks"),
        "usou_indice": usou,
        "node_type_topo": plano.get("Node Type"),
    }


# --------------------------------------------------------------------------
# A bancada
# --------------------------------------------------------------------------

class Bancada:
    """Recebe as conexões prontas; não abre nenhuma."""

    def __init__(self, conn_observado, conn_brokk,
                 repeticoes: int = REPETICOES,
                 timeout_ms: int = TIMEOUT_MS,
                 verboso: bool = True) -> None:
        self.obs = conn_observado
        self.brokk = conn_brokk
        self.repeticoes = repeticoes
        self.timeout_ms = timeout_ms
        self.verboso = verboso

    # -- preparo da sessão -----------------------------------------------

    def preparar_sessao(self) -> None:
        # O psycopg 3 prepara sozinho, no servidor, todo comando que vê mais
        # de cinco vezes, com nomes _pg3_N. Numa bancada isso é duplamente
        # ruim: o DEALLOCATE da nossa prepared statement derrubava as dele
        # junto (medido: "prepared statement _pg3_1 does not exist" na
        # terceira query do replay), e um EXPLAIN preparado do lado do cliente
        # é mais uma camada de cache de plano por cima da que se quer medir.
        self.obs.prepare_threshold = None

        with self.obs.cursor() as cur:
            # Os dois primeiros, e nesta ordem: o servidor loga com
            # log_min_duration_statement=0 até o statement que o desliga, e
            # cada SET que vier antes é mais uma linha de ruído no corpus.
            # Medido: com estes dois por último, quatro statements da bancada
            # sobravam no log; com eles na frente, sobra um.
            for guc in ("log_min_duration_statement",
                        "auto_explain.log_min_duration"):
                try:
                    cur.execute(f"SET {guc} = -1")
                except Exception as erro:       # noqa: BLE001
                    self.obs.rollback()
                    print(f"AVISO: não consegui desligar {guc} ({erro}).\n"
                          f"  A bancada vai aparecer no log e o coletor pode "
                          f"ingeri-la como carga.\n"
                          f"  Descarte depois por application_name = "
                          f"'{NOME_APP}'", file=sys.stderr)
            cur.execute(f"SET application_name = '{NOME_APP}'")
            cur.execute("SET plan_cache_mode = force_custom_plan")
            cur.execute(f"SET statement_timeout = {int(self.timeout_ms)}")
        self.obs.commit()

    def checar_ociosidade(self, forcar: bool = False) -> None:
        """Recusa medir com carga rodando. Medir sob carga mede a carga."""
        with self.obs.cursor() as cur:
            cur.execute(SQL_OCUPADO, (NOME_APP,))
            ativos = cur.fetchall()
        if not ativos:
            return
        detalhe = ", ".join(f"{a or '(sem nome)'}={n}" for a, n in ativos)
        if forcar:
            print(f"  AVISO: há sessão ativa no banco observado ({detalhe}); "
                  f"--forcar ligado, medindo assim mesmo")
            return
        sys.exit(
            f"há carga rodando no banco observado: {detalhe}\n"
            "Medir durante a carga mede a carga, não o índice. Espere o "
            "BenchBase terminar, ou use --forcar se souber o que está fazendo."
        )

    # -- o índice ---------------------------------------------------------

    def criar_indice(self, ddl: str) -> Tuple[str, float, Optional[int]]:
        """Cria e devolve (nome, ms de build, bytes)."""
        nome = nome_do_ddl(ddl)
        inicio = time.perf_counter()
        with self.obs.cursor() as cur:
            cur.execute(ddl)
        self.obs.commit()
        gasto = (time.perf_counter() - inicio) * 1000

        with self.obs.cursor() as cur:
            cur.execute("SELECT pg_relation_size(to_regclass(%s))", (nome,))
            linha = cur.fetchone()
        return nome, gasto, (linha[0] if linha else None)

    def derrubar_indice(self, nome: str) -> None:
        try:
            with self.obs.cursor() as cur:
                cur.execute(f"DROP INDEX IF EXISTS {nome}")
            self.obs.commit()
        except Exception as erro:               # noqa: BLE001
            self.obs.rollback()
            print(f"AVISO: não consegui derrubar {nome} ({erro}). "
                  f"Rode: python -m src.index_provider limpar", file=sys.stderr)

    def analisar_relacao(self, relacao: str) -> None:
        """ANALYZE antes de cada fase: as duas partem de estatística fresca."""
        try:
            with self.obs.cursor() as cur:
                cur.execute(f"ANALYZE {relacao}")
            self.obs.commit()
        except Exception:                       # noqa: BLE001
            self.obs.rollback()

    # -- uma query --------------------------------------------------------

    def _montar(self, q: Query, analisar: bool = True,
                formato_json: bool = False) -> Tuple[Any, str]:
        """Devolve (comando de EXPLAIN, como foi montado).

        Três caminhos, nesta ordem:

          direto    query sem parâmetro: o texto entra no EXPLAIN como está
          prepare   PREPARE declara os tipos, os valores vão como literais no
                    EXECUTE
          literais  o PREPARE falhou porque o Postgres não infere o tipo do
                    parâmetro naquela posição — medido no corpus em
                    ``concat($1, ' day')::interval``, onde concat é variádica
                    "any". Aí os literais entram direto no texto da query.

        O fallback não é equivalente ao PREPARE: literal sem tipo chega como
        `unknown` e o planner o converte pelo contexto, que é exatamente o
        que aconteceria se a query tivesse sido escrita com a constante.
        Para medir índice isso serve; para reproduzir o plano genérico de uma
        prepared statement, não serviria.
        """
        if analisar:
            explain = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
        else:
            # Sem ANALYZE: planeja e não executa. Em JSON quando quem chama
            # quer ler o plano (a matriz pergunta se ele usa o índice).
            explain = "EXPLAIN (FORMAT JSON) " if formato_json else "EXPLAIN "
        params = q.parametros or []
        if not params:
            return explain + q.texto, "direto"

        with self.obs.cursor() as cur:
            literais = [sql.Literal(v).as_string(cur) for v in params]
            try:
                # Só a nossa, e só se existir: DEALLOCATE de nome ausente
                # é erro, e erro aqui aborta a transação inteira.
                cur.execute("SELECT 1 FROM pg_prepared_statements "
                            " WHERE name = %s", (PREPARADA,))
                if cur.fetchone():
                    cur.execute(f"DEALLOCATE {PREPARADA}")
                # Sem parâmetros nesta execute(): o psycopg usa o protocolo
                # simples e não tenta interpretar o % que a query possa ter
                # dentro de um literal.
                cur.execute(f"PREPARE {PREPARADA} AS {q.texto}")
            except Exception:                   # noqa: BLE001
                self.obs.rollback()
                return (explain + substituir_parametros(q.texto, literais),
                        "literais")
        self.obs.commit()
        argumentos = sql.SQL(", ").join(sql.Literal(v) for v in params)
        return (sql.SQL(explain + "EXECUTE {} ({})")
                .format(sql.Identifier(PREPARADA), argumentos)), "prepare"

    def checar_replay(self, q: Query) -> Tuple[bool, str, Optional[str]]:
        """Planeja a query sem executá-la. Devolve (ok, via, erro).

        EXPLAIN sem ANALYZE: resolve nomes, tipos e permissões, e não roda
        nada. É o que separa "a bancada consegue medir isto" de "isto só faz
        sentido dentro da função/transação de onde veio", sem gastar uma
        execução de query analítica para descobrir.
        """
        try:
            comando, via = self._montar(q, analisar=False)
        except Exception as erro:               # noqa: BLE001
            self.obs.rollback()
            return False, "prepare", str(erro)[:400]
        try:
            with self.obs.cursor() as cur:
                cur.execute(comando)
                cur.fetchall()
            self.obs.commit()
            return True, via, None
        except Exception as erro:               # noqa: BLE001
            self.obs.rollback()
            return False, via, str(erro)[:400]
        finally:
            self._soltar_preparada()

    def _soltar_preparada(self) -> None:
        try:
            with self.obs.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_prepared_statements "
                            " WHERE name = %s", (PREPARADA,))
                if cur.fetchone():
                    cur.execute(f"DEALLOCATE {PREPARADA}")
            self.obs.commit()
        except Exception:                       # noqa: BLE001
            self.obs.rollback()

    def medir(self, q: Query, fase: str,
              indice: Optional[str]) -> List[Medida]:
        """Roda a query `repeticoes` vezes e devolve uma Medida por repetição."""
        medidas: List[Medida] = []
        try:
            comando, via = self._montar(q)
        except Exception as erro:               # noqa: BLE001
            self.obs.rollback()
            return [Medida(q.ref, fase, r + 1, erro=f"montagem: {erro}"[:400])
                    for r in range(self.repeticoes)]
        preparada = via == "prepare"

        for rep in range(1, self.repeticoes + 1):
            try:
                with self.obs.cursor() as cur:
                    cur.execute(comando)
                    saida = cur.fetchone()[0]
                self.obs.commit()
                medidas.append(Medida(q.ref, fase, rep,
                                      **ler_explain(saida, indice)))
            except Exception as erro:           # noqa: BLE001
                self.obs.rollback()
                if estourou_timeout(erro):
                    # O tempo real passa do limite, e o limite é o que se
                    # sabe: vira piso, não erro. Como erro, a query sumiria do
                    # resultado justamente onde o índice mais pesa (Q17 e Q20
                    # sem índice em l_partkey levam horas). E repetir só
                    # confirmaria o mesmo timeout mais quatro vezes.
                    medidas.append(Medida(q.ref, fase, rep,
                                          tempo_ms=float(self.timeout_ms),
                                          censurado=True))
                    break
                medidas.append(Medida(q.ref, fase, rep, erro=str(erro)[:400]))

        if preparada:
            self._soltar_preparada()
        return medidas

    def _gravar(self, experimento_id: int, medidas: Sequence[Medida]) -> None:
        with self.brokk.cursor() as cur:
            for m in medidas:
                cur.execute(SQL_GRAVAR_MEDIDA, (
                    experimento_id, m.query_ref, m.fase, m.repeticao,
                    m.tempo_ms, m.custo_estimado, m.linhas_reais,
                    m.shared_hit, m.shared_read, m.usou_indice,
                    m.node_type_topo, m.erro, m.censurado))
        self.brokk.commit()

    # -- o experimento inteiro -------------------------------------------

    def rodar(self, experimento_id: int, ddl: str, relacao: Optional[str],
              queries: Sequence[Query], forcar: bool = False) -> dict:
        """Mede um experimento ponta a ponta. Devolve um resumo."""
        self.preparar_sessao()
        self.checar_ociosidade(forcar)

        # Rede de segurança para quem não rodou `conferir`: planejar é barato,
        # e uma query irreplicável descoberta só no meio do baseline gasta o
        # tempo das repetições e deixa o cluster medido pela metade.
        reprovadas = []
        replicaveis = []
        for q in queries:
            pode, _via, erro = self.checar_replay(q)
            (replicaveis if pode else reprovadas).append((q, erro))
        queries = [q for q, _ in replicaveis]
        if reprovadas and self.verboso:
            print(f"  {len(reprovadas)} query(s) não replicável(is), fora da "
                  f"medição:")
            for q, erro in reprovadas:
                print(f"      ref={q.ref:<4} {erro.splitlines()[0][:66]}")
        for q, erro in reprovadas:
            # Uma linha por fase, para o motivo ficar no registro em vez de a
            # query simplesmente sumir do cluster.
            self._gravar(experimento_id,
                         [Medida(q.ref, f, 1, erro=erro) for f in
                          ("antes", "depois")])
        if not queries:
            with self.brokk.cursor() as cur:
                cur.execute(SQL_FECHAR, ("falhou",
                                         "nenhuma query replicável no cluster",
                                         None, None, experimento_id))
            self.brokk.commit()
            return {"estado": "falhou", "indice": None, "build_ms": None,
                    "tamanho_bytes": None,
                    "erro": "nenhuma query replicável no cluster"}

        if relacao:
            self.analisar_relacao(relacao)

        nome = None
        build_ms = tamanho = None
        estado, erro_geral = "medido", None

        try:
            if self.verboso:
                print(f"  baseline: {len(queries)} queries × "
                      f"{self.repeticoes} repetições")
            for q in queries:
                medidas = self.medir(q, "antes", None)
                self._gravar(experimento_id, medidas)
                if self.verboso:
                    self._linha(q, medidas)

            if self.verboso:
                print(f"  {ddl}")
            nome, build_ms, tamanho = self.criar_indice(ddl)
            if self.verboso:
                mb = (tamanho or 0) / 1048576
                print(f"  + índice {nome} em {build_ms/1000:.1f}s, {mb:,.1f} MB")

            if relacao:
                self.analisar_relacao(relacao)

            if self.verboso:
                print(f"  replay:")
            for q in queries:
                medidas = self.medir(q, "depois", nome)
                self._gravar(experimento_id, medidas)
                if self.verboso:
                    self._linha(q, medidas)

        except KeyboardInterrupt:
            estado, erro_geral = "falhou", "interrompido"
            raise
        except Exception as erro:               # noqa: BLE001
            estado, erro_geral = "falhou", str(erro)[:400]
            print(f"  experimento falhou: {erro}", file=sys.stderr)
        finally:
            # O finally é o ponto inteiro deste desenho: o índice sai, tenha o
            # replay terminado ou não.
            if nome:
                self.derrubar_indice(nome)
                if self.verboso:
                    print(f"  - índice {nome} derrubado")
            with self.brokk.cursor() as cur:
                cur.execute(SQL_FECHAR, (estado, erro_geral, build_ms,
                                         tamanho, experimento_id))
            self.brokk.commit()

        return {"estado": estado, "indice": nome, "build_ms": build_ms,
                "tamanho_bytes": tamanho, "erro": erro_geral}

    def _linha(self, q: Query, medidas: Sequence[Medida]) -> None:
        uteis = [m.tempo_ms for m in medidas[1:] if m.tempo_ms is not None]
        erros = [m.erro for m in medidas if m.erro]
        if any(m.censurado for m in medidas):
            print(f"      ref={q.ref:<4} ≥ {self.timeout_ms:>8,.0f} ms "
                  f"(estourou o timeout; vale como piso)")
        elif erros:
            print(f"      ref={q.ref:<4} erro: {erros[0][:60]}")
        elif uteis:
            media = sum(uteis) / len(uteis)
            usou = any(m.usou_indice for m in medidas)
            marca = " (usou o índice)" if usou else ""
            print(f"      ref={q.ref:<4} {media:>10,.1f} ms{marca}")


# --------------------------------------------------------------------------
# Limpeza
# --------------------------------------------------------------------------

SQL_GRAVAR_REPLAY = """
INSERT INTO monitoring.query_replay (query_ref, replicavel, via, erro)
VALUES (%s, %s, %s, %s)
ON CONFLICT (query_ref) DO UPDATE
   SET replicavel = EXCLUDED.replicavel, via = EXCLUDED.via,
       erro = EXCLUDED.erro, conferido_em = now()
"""


def conferir_replay(b: "Bancada", queries: Sequence[Query],
                    verboso: bool = True) -> Tuple[int, int]:
    """Planeja cada query e grava se a bancada consegue reexecutá-la.

    Devolve (replicáveis, reprovadas).
    """
    b.preparar_sessao()
    ok = ruim = 0
    for q in queries:
        pode, via, erro = b.checar_replay(q)
        with b.brokk.cursor() as cur:
            cur.execute(SQL_GRAVAR_REPLAY, (q.ref, pode, via, erro))
        b.brokk.commit()
        if pode:
            ok += 1
            if verboso and via != "direto":
                print(f"  + ref={q.ref:<4} ok (via {via})")
        else:
            ruim += 1
            if verboso:
                print(f"  - ref={q.ref:<4} {erro.splitlines()[0][:70]}")
    if verboso:
        print(f"  = {ok} replicáveis, {ruim} reprovadas")
    return ok, ruim


def indices_orfaos(conn_observado) -> List[str]:
    with conn_observado.cursor() as cur:
        cur.execute(SQL_INDICES_EXP, (len(PREFIXO), PREFIXO))
        return [r[0] for r in cur.fetchall()]


def limpar(conn_observado, verboso: bool = True) -> int:
    """Derruba todo índice brokk_exp_*. A segunda linha de defesa."""
    nomes = indices_orfaos(conn_observado)
    for nome in nomes:
        with conn_observado.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {nome}")
        if verboso:
            print(f"  - {nome}")
    conn_observado.commit()
    if verboso:
        print(f"  = {len(nomes)} índice(s) de experimento derrubado(s)")
    return len(nomes)
