#!/bin/sh
# Выполняется внутри контейнера Agent. Конфигурация передаётся через pipe:
# токен берётся из окружения, не попадает в argv и не записывается в YAML.
set -eu

: "${AGENT_ACCESS_TOKEN:?Set AGENT_ACCESS_TOKEN}"
: "${AGENT_GLOBAL_BROKER:?Set AGENT_GLOBAL_BROKER}"
: "${AGENT_LOCAL_BROKER:?Set AGENT_LOCAL_BROKER}"
: "${AGENT_GLOBAL_CLIENT_ID:?Set AGENT_GLOBAL_CLIENT_ID}"
: "${AGENT_LOCAL_CLIENT_ID:?Set AGENT_LOCAL_CLIENT_ID}"
: "${AGENT_CONFIG_PATH:?Set AGENT_CONFIG_PATH}"

# Ограничения токена проверены по сообщению валидатора поставленного Agent.
case "$AGENT_ACCESS_TOKEN" in
    *[+#/]*) echo 'AGENT_ACCESS_TOKEN contains an unsupported MQTT character' >&2; exit 2 ;;
esac
if [ "${#AGENT_ACCESS_TOKEN}" -gt 20 ]; then
    echo 'AGENT_ACCESS_TOKEN must not exceed 20 characters' >&2
    exit 2
fi

awk '
function quoted(value, result, i, ch) {
    result = "\""
    for (i = 1; i <= length(value); i++) {
        ch = substr(value, i, 1)
        if (ch == "\\") result = result "\\\\"
        else if (ch == "\"") result = result "\\\""
        else if (ch == "\n") result = result "\\n"
        else if (ch == "\r") result = result "\\r"
        else if (ch == "\t") result = result "\\t"
        else result = result ch
    }
    return result "\""
}
/^[[:space:]]*global_broker:/ { section = "global" }
/^[[:space:]]*local_broker:/ { section = "local" }
/^[[:space:]]*(mqtt|logger|storage):/ { section = "" }
{
    key = ""
    variable = ""
    if (section == "global" && /^[[:space:]]*host:[[:space:]]/) {
        key = "host"; variable = "AGENT_GLOBAL_BROKER"
    } else if (section == "local" && /^[[:space:]]*host:[[:space:]]/) {
        key = "host"; variable = "AGENT_LOCAL_BROKER"
    } else if (section == "global" && /^[[:space:]]*client_id:[[:space:]]/) {
        key = "client_id"; variable = "AGENT_GLOBAL_CLIENT_ID"
    } else if (section == "local" && /^[[:space:]]*client_id:[[:space:]]/) {
        key = "client_id"; variable = "AGENT_LOCAL_CLIENT_ID"
    } else if (/^[[:space:]]*access_token_only_via_broker:[[:space:]]/) {
        key = "access_token_only_via_broker"; variable = "AGENT_ACCESS_TOKEN"
    }
    if (variable != "") {
        match($0, /^[[:space:]]*/)
        print substr($0, 1, RLENGTH) key ": " quoted(ENVIRON[variable])
    } else {
        print
    }
}
' "$AGENT_CONFIG_PATH" | agent -c /dev/stdin -l "${AGENT_LOG_LEVEL:-4}" &
agent_pid=$!
trap 'kill -TERM "$agent_pid" 2>/dev/null || true; wait "$agent_pid" || true; exit 0' TERM INT
wait "$agent_pid"
