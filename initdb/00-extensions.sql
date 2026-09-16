-- Roda automaticamente na primeira subida do container.
-- pg_stat_statements: fonte estruturada dos gargalos (Fase 1 do BrokkPG).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- pgvector já deixado pronto para a camada RAG (Fase 2).
-- A imagem oficial postgres:18 NÃO inclui pgvector por padrão; se der erro,
-- veja a nota no final do passo-a-passo sobre como habilitar.
-- CREATE EXTENSION IF NOT EXISTS vector;