#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""As duas conversas com Claude: a análise da carga e a escolha do candidato.

``analisar()`` é o modo clássico — o dossiê inteiro entra, uma lista de
recomendações sai. ``escolher()`` é o modo de clusters: o dossiê de um
cluster mais os candidatos que a heurística mediu, e sai **um** índice.

A diferença entre as duas não é de tamanho, é de pergunta. A primeira pergunta
"o que fazer com estes índices?"; a segunda, "destes candidatos medidos, qual
merece ir para a bancada?" — e a resposta dela vai ser testada empiricamente
em seguida, o que muda o que se pede ao modelo: não uma lista boa, uma aposta
falsificável.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Sequence

from .banco import chave, como_json

# A chave da Anthropic. Deixe vazia e exporte ANTHROPIC_API_KEY, ou cole aqui.
#
# Se colar aqui: este arquivo está versionado. Uma chave commitada é uma chave
# vazada — o GitHub varre pushes públicos e a Anthropic revoga automaticamente
# o que aparece. Para uso local sem risco, deixe vazio e use a variável de
# ambiente, ou ponha a chave em .env (que o .gitignore já cobre).
CHAVE_ANTHROPIC = ""

MODELO = "claude-opus-5"


# --------------------------------------------------------------------------
# O prompt do modo clássico
# --------------------------------------------------------------------------

PROMPT_SISTEMA = """\
Você é um especialista em ajuste de índices no PostgreSQL 18. Recebe o retrato
medido de uma carga real — não o palpite de alguém sobre ela — e propõe
mudanças no conjunto de índices.

O dossiê tem quatro partes:

  workload   as queries mais caras, com quantas vezes rodaram, quanto levaram,
             o que cada Seq Scan filtrou e quantas linhas descartou, as
             condições de junção, as chaves de ordenação e quais índices já são
             usados. Tudo isso vem dos planos do auto_explain, ou seja, é
             execução real e não estimativa do planner.
  indices    o que existe hoje no banco: definição, tamanho em disco e quantas
             varreduras cada um recebeu desde o último reset das estatísticas.
  tabelas    linhas vivas, tamanho, e a atividade de escrita (inserts, updates,
             deletes) de cada tabela.
  colunas    pg_stats: largura média, n_distinct e correlação física de cada
             coluna. É o que permite estimar tamanho e seletividade.

Procure, nesta ordem de valor:

1. Índice faltando. Seq Scan repetido que descarta muita linha por um filtro
   estável é o caso clássico. Prefira um índice que sirva a várias queries do
   dossiê a um que sirva a uma só.
2. Consolidação. Dois ou mais índices com prefixo comum costumam virar um só,
   às vezes com INCLUDE para cobrir as colunas projetadas e permitir Index Only
   Scan. Diga quais índices o novo torna redundantes.
3. Índice que não paga o que custa. idx_scan igual ou próximo de zero num
   índice grande, numa tabela que recebe escrita, é candidato a remoção —
   exceto se for UNIQUE ou der suporte a chave primária ou estrangeira, caso em
   que ele existe por integridade e não por desempenho, e deve ficar.

Regras que valem para toda recomendação:

- Estime o tamanho a partir dos números do dossiê, não de intuição. Ancore no
  bytes-por-linha dos índices que já existem naquela tabela e no avg_width das
  colunas. Escreva em `base_da_estimativa` a conta que você fez, com os números
  que usou. Se não der para estimar, diga isso ali em vez de inventar.
- Todo índice novo custa escrita. Se a tabela tem update ou insert relevante no
  dossiê, diga isso em `risco` — com o número.
- Cite as queries pelo `query_ref` do dossiê. Não descreva "a query de vendas".
- Quando o dado não sustentar uma recomendação, use a ação `investigar` e diga
  o que falta medir. Um "não sei" é mais útil que um palpite confiante.
- Não recomende índice para tabela do catálogo (pg_*).

Não há prêmio por encher a lista. Poucas recomendações fortes valem mais que
muitas fracas.
"""

# ---------------------------------------------------------------------------
# O prompt do modo de clusters
# ---------------------------------------------------------------------------
# Escrito para uma pergunta diferente: a resposta vai ser MEDIDA logo em
# seguida, criando o índice de verdade e reexecutando as queries do cluster.
# Isso é dito ao modelo de propósito — o que se quer não é a recomendação mais
# defensável, é a que melhor distingue "serve ao cluster" de "serve a uma
# query". Uma aposta que erra e é medida vale mais que uma prudência vaga.

PROMPT_CLUSTER = """\
Você é um especialista em ajuste de índices no PostgreSQL 18, escolhendo o
índice que vai para uma bancada de medição.

O contexto é um experimento. Um conjunto de queries foi agrupado por
similaridade do texto SQL (embedding + UMAP + HDBSCAN), e a hipótese em teste
é: **um índice derivado de uma query serve às outras queries do mesmo
cluster?** Sua escolha vai ser aplicada de verdade no banco, as queries do
cluster vão ser reexecutadas com EXPLAIN (ANALYZE), e o ganho ou perda de cada
uma vai ser medido. Depois o índice é derrubado.

Você recebe:

  cluster       o id do cluster e as query_ref que caíram nele
  workload      as mesmas quatro partes do dossiê de sempre, recortadas nestas
                queries: o que cada Seq Scan filtrou, quanto descartou,
                junções, ordenações, índices já usados
  indices       o que já existe no banco, com tamanho e idx_scan
  tabelas       linhas, tamanho e atividade de escrita
  colunas       pg_stats: avg_width, n_distinct, correlação
  candidatos    índices concretos que uma heurística extraiu dos planos
                medidos, cada um com a expressão de onde saiu, quantas queries
                do cluster o tocam, quantas linhas descarta em média e quanto
                tempo custou

Escolha **um** candidato, ou proponha uma variação de um deles (trocar a ordem
das colunas, acrescentar uma coluna, usar INCLUDE, usar índice parcial com
WHERE). Não invente um índice que nenhuma evidência do dossiê sustente.

O que faz uma boa escolha aqui:

- **Alcance no cluster acima de alcance numa query.** Entre um índice que
  economiza muito numa query só e um que economiza razoavelmente em quatro, o
  segundo é a escolha certa — é a hipótese que o experimento testa. Declare em
  `queries_beneficiadas` todas que você espera que melhorem, e seja honesto:
  a medição vai conferir.
- **Ancorar a estimativa nos números.** `espaco_novo_mb` sai do avg_width das
  colunas e do bytes_por_linha dos índices que já existem naquela tabela.
  Escreva a conta em `base_da_estimativa`.
- **Dizer o que não escolheu e por quê**, em `descartados`. O experimento
  guarda os candidatos junto com a escolha, e saber o que foi recusado é
  metade do valor do registro.
- **Recusar quando não há candidato bom.** Se nenhum candidato tem evidência
  que justifique criar um índice — o cluster é pequeno demais, as queries já
  usam índice adequado, o filtro descarta pouco — devolva `escolhido: false` e
  explique. Um cluster sem índice medido é um resultado; um índice ruim medido
  é ruído no experimento.

Restrições da bancada, e elas são rígidas:

- O DDL tem que ser **uma única instrução CREATE INDEX**, sem ponto e vírgula,
  sem comentário, sem CONCURRENTLY.
- O nome do índice tem que começar com `brokk_exp_`.
- Só tabelas do schema public. Nunca pg_*.
"""


# --------------------------------------------------------------------------
# Os formatos de resposta
# --------------------------------------------------------------------------
# Structured outputs: a resposta volta validada contra o esquema, então não há
# parse de prosa nem recomendação com campo faltando.

ESQUEMA_SAIDA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "recomendacoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "acao": {
                        "type": "string",
                        "enum": ["criar", "substituir", "remover", "investigar"],
                    },
                    "prioridade": {
                        "type": "string",
                        "enum": ["alta", "media", "baixa"],
                    },
                    "relacao": {"type": "string"},
                    "ddl": {
                        "type": "string",
                        "description": "SQL executável, ou vazio quando a ação é investigar",
                    },
                    "indices_afetados": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Índices que esta mudança substitui ou remove",
                    },
                    "espaco_novo_mb": {"type": "number"},
                    "espaco_liberado_mb": {"type": "number"},
                    "base_da_estimativa": {
                        "type": "string",
                        "description": "A conta feita, com os números do dossiê",
                    },
                    "queries_beneficiadas": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "query_ref do dossiê",
                    },
                    "ganho_estimado_ms": {"type": "number"},
                    "justificativa": {"type": "string"},
                    "risco": {"type": "string"},
                    "confianca": {"type": "number"},
                },
                "required": [
                    "acao", "prioridade", "relacao", "ddl", "indices_afetados",
                    "espaco_novo_mb", "espaco_liberado_mb", "base_da_estimativa",
                    "queries_beneficiadas", "ganho_estimado_ms",
                    "justificativa", "risco", "confianca",
                ],
                "additionalProperties": False,
            },
        },
        "observacoes": {
            "type": "string",
            "description": "O que o dossiê não permitiu concluir, e o que faltou medir",
        },
    },
    "required": ["recomendacoes", "observacoes"],
    "additionalProperties": False,
}

ESQUEMA_ESCOLHA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "escolhido": {
            "type": "boolean",
            "description": "false quando nenhum candidato merece ir para a bancada",
        },
        "ddl": {
            "type": "string",
            "description": "Um CREATE INDEX só, nome começando com brokk_exp_. "
                           "Vazio quando escolhido é false.",
        },
        "relacao": {"type": "string"},
        "query_origem": {
            "type": "integer",
            "description": "query_ref de onde o candidato saiu. 0 quando escolhido é false.",
        },
        "queries_beneficiadas": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Todas as query_ref do cluster que você espera que melhorem",
        },
        "espaco_novo_mb": {"type": "number"},
        "base_da_estimativa": {"type": "string"},
        "justificativa": {"type": "string"},
        "descartados": {
            "type": "string",
            "description": "Quais candidatos foram recusados e por quê",
        },
        "risco": {"type": "string"},
        "confianca": {"type": "number"},
    },
    "required": [
        "escolhido", "ddl", "relacao", "query_origem", "queries_beneficiadas",
        "espaco_novo_mb", "base_da_estimativa", "justificativa",
        "descartados", "risco", "confianca",
    ],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# A chamada
# --------------------------------------------------------------------------

def _pedir(prompt_sistema: str, esquema: dict, pedido: str,
           verboso: bool = True) -> dict:
    """Uma chamada com saída estruturada. Devolve o JSON já validado.

    Sem `fallbacks` de recusa de propósito: este módulo grava o modelo que
    respondeu junto do experimento, e um fallback silencioso para outro modelo
    misturaria duas procedências na mesma tabela de resultados. Uma recusa
    aqui tem que virar erro visível para quem está conduzindo o experimento,
    não uma resposta de outra origem.
    """
    import anthropic

    cliente = anthropic.Anthropic(api_key=chave(CHAVE_ANTHROPIC))

    if verboso:
        print(f"  enviando {len(pedido):,} caracteres para {MODELO}...")

    # Streaming porque a entrada é grande e o pensamento adaptativo em effort
    # alto pode levar minutos — sem stream isso bate no timeout de HTTP.
    with cliente.messages.stream(
        model=MODELO,
        max_tokens=32000,
        system=prompt_sistema,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": esquema},
        },
        messages=[{"role": "user", "content": pedido}],
    ) as stream:
        resposta = stream.get_final_message()

    if resposta.stop_reason == "refusal":
        detalhe = getattr(resposta, "stop_details", None)
        sys.exit(f"o modelo recusou a análise: {detalhe}")

    texto = next(b.text for b in resposta.content if b.type == "text")
    saida = json.loads(texto)

    if verboso:
        u = resposta.usage
        print(f"  {u.input_tokens:,} tokens de entrada, "
              f"{u.output_tokens:,} de saída")
    return saida


def analisar(dossie: dict, verboso: bool = True) -> dict:
    """Manda o dossiê e devolve as recomendações já validadas pelo esquema."""
    pedido = (
        "Segue o dossiê da carga. Analise e devolva as recomendações de índice.\n\n"
        "<dossie>\n" + como_json(dossie) + "\n</dossie>"
    )
    return _pedir(PROMPT_SISTEMA, ESQUEMA_SAIDA, pedido, verboso)


def escolher(cluster_id: int, refs: Sequence[int], dossie: dict,
             candidatos: Sequence, verboso: bool = True) -> dict:
    """Escolhe um índice para o cluster. Devolve o dicionário do esquema.

    O DDL volta já passado pelo portão de candidatos.validar_ddl(): uma
    instrução, CREATE INDEX, nome com o prefixo do experimento.
    """
    from .candidatos import validar_ddl

    corpo = {
        "cluster": {"id": cluster_id, "queries": list(refs)},
        "candidatos": [c.como_dict() for c in candidatos],
        **{k: v for k, v in dossie.items() if k != "gerado_em"},
    }
    pedido = (
        f"Segue o cluster {cluster_id}, com {len(refs)} queries, o dossiê "
        f"recortado nelas e os candidatos que a heurística mediu. Escolha o "
        f"índice que vai para a bancada.\n\n"
        "<cluster>\n" + como_json(corpo) + "\n</cluster>"
    )
    saida = _pedir(PROMPT_CLUSTER, ESQUEMA_ESCOLHA, pedido, verboso)

    if saida.get("escolhido") and saida.get("ddl"):
        try:
            saida["ddl"] = validar_ddl(saida["ddl"])
        except ValueError as erro:
            # Não tenta consertar: um DDL que não passa no portão vira cluster
            # sem experimento, e isso fica registrado.
            saida["escolhido"] = False
            saida["justificativa"] = (
                f"[DDL reprovado no portão: {erro}] "
                + saida.get("justificativa", "")
            )
    return saida
