-- Выполняется PostgreSQL entrypoint только при создании нового кластера.
-- Роль smartplatform создаётся через POSTGRES_USER до выполнения этого файла.
CREATE SCHEMA IF NOT EXISTS smartplatform AUTHORIZATION smartplatform;
