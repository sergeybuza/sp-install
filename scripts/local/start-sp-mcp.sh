#!/usr/bin/env bash
# Выполняется на рабочем компьютере с Docker. Секрет поступает только в окружение.
# Создаёт один контейнер sp-mcp, без томов, с HTTP-портом только на loopback.
set -euo pipefail

if [[ -z "${SP_MCP_URL:-}" ]]; then
  printf 'Задайте SP_MCP_URL в окружении процесса.\n' >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  printf 'Команда docker не найдена.\n' >&2
  exit 1
fi
docker image inspect smartplatform/sp-mcp:1.0.0 >/dev/null

if docker container inspect sp-mcp >/dev/null 2>&1; then
  printf 'Контейнер sp-mcp уже существует. Сначала проверьте его состояние.\n' >&2
  exit 1
fi

if [[ -z "${SP_MCP_API_KEY:-}" ]]; then
  if [[ ! -t 0 ]]; then
    printf 'Задайте SP_MCP_API_KEY в окружении или запустите скрипт в терминале.\n' >&2
    exit 1
  fi
  read -r -s -p 'API-ключ платформы (ввод скрыт): ' SP_MCP_API_KEY
  printf '\n'
fi
if [[ -z "${SP_MCP_API_KEY:-}" ]]; then
  printf 'API-ключ не задан.\n' >&2
  exit 1
fi
export SP_MCP_URL SP_MCP_API_KEY
trap 'unset SP_MCP_API_KEY' EXIT

docker run --pull=never -d --name sp-mcp --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e SP_MCP_URL -e SP_MCP_API_KEY \
  -e SP_MCP_MODE=facade -e SP_MCP_WRITE=false \
  -e SPRING_AI_MCP_SERVER_STDIO=false \
  -e SPRING_WEB_APPLICATION_TYPE=servlet \
  -e SPRING_AI_MCP_SERVER_PROTOCOL=STREAMABLE \
  smartplatform/sp-mcp:1.0.0

printf 'Контейнер создан. После запуска JVM проверьте MCP: http://127.0.0.1:8000/mcp\n'
