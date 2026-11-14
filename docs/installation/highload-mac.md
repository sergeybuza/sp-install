# Highload на Mac с платформой на отдельном стенде

Проверено 2026-11-14 на macOS 26.5.2, arm64: Docker Desktop 4.49.0,
Engine 28.5.1 (linux/arm64), Compose v2.40.3-desktop.1. Docker VM имеет
8 CPU и около 7,65 GiB RAM; сам Mac — 8 CPU и 16 GiB RAM.
Платформа находится на [установочном стенде](../../inventory/hosts/stand/README.md).

Это пробный поток стандартного сценария: 10 устройств, 100 сигналов,
период 1000 мс, без ограничения длительности. Около 100 сообщений/с —
частота публикации, заданная в Excel. Agent применяет апертуру и пакетную
отправку, поэтому это не измерение скорости записи в БД и не проверка
предельной нагрузки платформы.

[Состав и контрольные суммы поставки](../../inventory/releases/highload-2026-11-14.md).
[Результат сквозной проверки](../review/2026-11-14-highload-mac.md).

## Размещение и изоляция

Все команды ниже выполняются **на Mac, из корня этой рабочей копии проекта**.
Используется [highload-mac.yml](../../deploy/compose/highload-mac.yml):

- Compose-проект `sp-highload-mac`, собственная сеть `sp-highload-mac_default`;
- локальный Mosquitto, Agent и PubMQTT; публикации портов Mac нет;
- Agent подключается к брокеру платформы по `tcp://192.168.10.107:1884`;
- контейнеры и сеть MCP не используются; общего управления Docker Desktop нет;
- исходные конфигурации и Excel монтируются только для чтения;
- буфер SQLite Agent находится в tmpfs `/app/db` и теряется при остановке;
- политика перезапуска трёх контейнеров — `unless-stopped`.

Скачивание образов отключено (`pull_policy: never`): все три образа должны
уже находиться в локальном Docker. В выполненном запуске Mosquitto был
доступен локально, Agent и PubMQTT загружены из поставки.

## Подготовка поставки

В `input/highload/` должны находиться оригиналы `DEPLOY.md` и
`sp-emulation-bundle.tar.gz`. Этот каталог исключён из Git. Оригиналы не
изменяются. Перед распаковкой сравните SHA-256 с инвентарём:

```bash
shasum -a 256 input/highload/DEPLOY.md input/highload/sp-emulation-bundle.tar.gz
```

Если `input/highload/sp-emulation/` ещё отсутствует, распакуйте архив,
пропустив поставочный `.env`. Проверка путей выполняется до извлечения:

```bash
python3 - <<'PY'
from pathlib import Path
import tarfile

target = Path('input/highload')
if (target / 'sp-emulation').exists():
    raise SystemExit('Каталог уже существует; повторная распаковка остановлена')
with tarfile.open(target / 'sp-emulation-bundle.tar.gz', 'r:gz') as archive:
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if (path.is_absolute() or '..' in path.parts
                or path.parts[0] != 'sp-emulation'
                or not (member.isfile() or member.isdir())):
            raise SystemExit('Небезопасный путь или тип в архиве')
    archive.extractall(target, members=[m for m in members
        if m.name != 'sp-emulation/.env'], filter='data')
PY
docker load -i input/highload/sp-emulation/images/sp-emulation-images.tar.gz
docker image inspect sp-agent:local sp-pubmqtt:local eclipse-mosquitto:2 \
  --format '{{.Id}} {{.Os}}/{{.Architecture}}'
```

На проверенном Mac все три образа имеют архитектуру `linux/arm64`.
Запуск на Intel Mac и эмуляция другой архитектуры не проверялись.

## Шлюз и запуск

Нужен отдельный шлюз в нужном тенанте платформы и его access token.
В запуске 2026-11-14 через API создан `highload-mac-20261114`, профиль
`default`, `additionalInfo.gateway=true`. Признак шлюза подтверждён по
модулям UI самой поставки, API — по OpenAPI стенда. Поле `isGateway`
не использовалось. Создание шлюза через форму браузера не проверялось.

До начала публикации сравните имена устройств сценария с существующими
устройствами тенанта. Стандартный файл создаёт
`b1_b1_1k_n1_test` … `b1_b1_1k_n10_test`. В проверенном запуске совпадений
не было; уже существующие устройства не перенастраивались.

Токен вводится скрыто в переменную окружения; `.env` и файлы с секретами
не создаются. Для ручного запуска в zsh на Mac:

```bash
read -r -s AGENT_ACCESS_TOKEN
export AGENT_ACCESS_TOKEN
docker compose -f deploy/compose/highload-mac.yml config --quiet
docker compose -f deploy/compose/highload-mac.yml up --wait --wait-timeout 60 --pull never
unset AGENT_ACCESS_TOKEN
```

Agent принимает токен длиной не более 20 символов без `+`, `#`, `/`.
Это ограничение подтверждено сообщением валидатора поставленного бинарника;
entrypoint проверяет его без вывода значения токена.

Поставочный entrypoint записывает токен в `/run/agent/agent.yaml` и передаёт
его `awk` аргументом. Вместо него используется
[highload-agent-entrypoint.sh](../../scripts/local/highload-agent-entrypoint.sh):
он читает токен из окружения `awk`, формирует YAML в pipe и запускает Agent
с `-c /dev/stdin`. Корневая ФС Agent доступна только для чтения. Docker-логи
Agent отключены, поскольку сообщения могут содержать токен в имени топика.

## Проверка и остановка

Для `ps` и `stop` заглушка нужна только для разбора обязательной переменной
Compose. Эти команды используют существующие контейнеры:

```bash
AGENT_ACCESS_TOKEN=unused docker compose -f deploy/compose/highload-mac.yml ps
AGENT_ACCESS_TOKEN=unused docker compose -f deploy/compose/highload-mac.yml stop --timeout 30
```

`stop` останавливает только этот Compose-проект. Сохранённая телеметрия и
созданные устройства на стенде остаются. Команда остановки приведена для
оператора; рабочий поток после сквозной проверки оставлен включённым.

Статус `running` Agent не подтверждает доставку. Проверка должна охватывать:

1. Сигналы PubMQTT в локальном брокере — через `mosquitto_sub` внутри
   `mosquitto-local`, топик `/devices/+/controls/+`.
2. Сообщения Agent в брокере стенда, топик `to-server/` с токеном шлюза.
   Подписчик должен получать токен из окружения; не передавайте такой топик
   аргументом CLI и не сохраняйте сырой MQTT-вывод.
3. Последние значения устройств через
   `GET /api/plugins/telemetry/DEVICE/{id}/values/timeseries`.
   В этой поставке значения находятся в `timeseries`, рядом с `metadata`.
4. Обновление временных меток в двух снимках и наличие нескольких точек
   истории через тот же путь с суффиксом `/range`.

В этом запуске проверены 100 сигналов и 100 полей статус-кодов на 10
дочерних устройствах. На самом шлюзе таблица телеметрии пуста; сигналы
сохраняются на дочерних устройствах.

Перезапуск Docker Desktop, перезагрузка Mac, работа после сна и максимальная
нагрузка не проверялись. При повторном запуске проверьте свежесть телеметрии
и состояние сети до увеличения сценария.
