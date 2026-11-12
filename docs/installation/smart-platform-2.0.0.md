# Смарт Платформа 2.0.0: монолит на стенде

Целевой ПК: `192.168.10.107`, Ubuntu 26.04.1 LTS, `linux/amd64`. Подготовка Docker и восстановленная история его установки описаны в [отдельной инструкции](ubuntu-26-docker.md): для репозиториев Ubuntu использованы пакеты `docker.io` и `docker-compose-v2`.

Конфигурация [monolith.yml](../../deploy/compose/monolith.yml) содержит четыре постоянно работающих сервиса: PostgreSQL, Mosquitto, ядро `sp-back-monolith` и `sp-web-ui`. Одноразовый `sp-install` включается отдельным профилем. Очередь — `in-memory`, JS — локальный, кеш — `caffeine`. Kafka, Redis, отдельные JS executor и MQTT transport в этом варианте не запускаются.

Имя проекта Compose во всех командах — **`smart-platform`**; менять его при повторном запуске нельзя. Секреты передаются только через окружение, без `.env` и значений паролей в командной строке. Исключение для закрытого TLS-ключа разрешено только на стенде `192.168.10.107`: единственное место хранения — `/opt/smart-platform/certs/server.key`, режим `0600`, владелец `100:101`.

## 1. Передать поставку и конфигурацию

Архив четырёх vendor-образов уже был передан и загружен на стенд. Его SHA-256 и Image ID приведены в [описании поставки](../../inventory/releases/smart-platform-2.0.0.md). Монолит использует из него только `sp-node:2.0.0` и `sp-web-ui:2.0.0`; остальные два образа остаются сохранёнными.

Для новой установки передайте файлы. Место выполнения: **рабочий компьютер, корень этого проекта**, с настроенным SSH-доступом.

```bash
ssh engineer@192.168.10.107 'mkdir -p /home/engineer/sp-install-input'
scp input/images/sp-images-2.0.0.tar deploy/compose/monolith.yml \
  deploy/config/postgres/10-schema.sql deploy/config/mosquitto/mosquitto.conf \
  scripts/checks/platform-http.py scripts/checks/platform-seed-smoke.py input/ca.crt \
  engineer@192.168.10.107:/home/engineer/sp-install-input/
```

Место выполнения: **стенд, SSH-сеанс `engineer`**. Откройте root-сеанс Bash; остальные блоки для стенда выполняются в нём. Не включайте трассировку команд и не записывайте значения секретов в историю.

```bash
sudo -i
set +x
set +o history
umask 077
dc() { docker compose --project-name smart-platform --file /opt/smart-platform/compose/monolith.yml "$@"; }
```

Место выполнения: **стенд, root-сеанс**. Установка файлов не изменяет оригиналы в `input/` рабочего компьютера.

```bash
install -d -m 0755 /opt/smart-platform/{compose,config/postgres,config/mosquitto,certs,checks}
install -m 0644 /home/engineer/sp-install-input/monolith.yml /opt/smart-platform/compose/monolith.yml
install -m 0644 /home/engineer/sp-install-input/10-schema.sql /opt/smart-platform/config/postgres/10-schema.sql
install -m 0644 /home/engineer/sp-install-input/mosquitto.conf /opt/smart-platform/config/mosquitto/mosquitto.conf
install -m 0644 /home/engineer/sp-install-input/platform-http.py /opt/smart-platform/checks/platform-http.py
install -m 0644 /home/engineer/sp-install-input/platform-seed-smoke.py /opt/smart-platform/checks/platform-seed-smoke.py
install -m 0644 /home/engineer/sp-install-input/ca.crt /opt/smart-platform/certs/andromeda-ca.crt
sha256sum /home/engineer/sp-install-input/sp-images-2.0.0.tar
docker load --quiet --input /home/engineer/sp-install-input/sp-images-2.0.0.tar
docker pull postgres:16
docker pull eclipse-mosquitto:2
docker image inspect --format '{{json .RepoDigests}}' postgres:16 eclipse-mosquitto:2
```

Продолжайте только после успешного завершения команд и сверки архива/Image ID с описанием поставки. На стенде зависимости загружены с кодом `0`, обе для `linux/amd64`:

| Образ | Полученный digest |
| --- | --- |
| `postgres:16` | `sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657` |
| `eclipse-mosquitto:2` | `sha256:75d6662dee52c947dc9dd9c29f1d8d7ffef78ddaf38001caa09755681e8bbc98` |

Теги зависимостей могут измениться. При другом digest остановитесь и проверьте новую поставку. Compose использует `pull_policy: never`: запуск не загружает недостающие образы автоматически.

## 2. Подготовить доверенные CA и MQTT TLS

`input/ca.crt` — публичный Andromeda Root CA, `CA=true`, срок действия 2024–2034. Проверка `openssl verify` успешна. SHA-256 сертификата: `0039310b89a8464abc053660c275f31f0b8b33663ac19ee0f609a98ec72bff31`.

Место выполнения: **стенд, root-сеанс, однократно перед первым запуском**. Создайте Java truststore из стандартного хранилища образа и добавьте Andromeda CA. Это хранилище доверенных публичных сертификатов; закрытого ключа в нём нет.

```bash
openssl verify -CAfile /opt/smart-platform/certs/andromeda-ca.crt /opt/smart-platform/certs/andromeda-ca.crt
export SP_TRUSTSTORE_PASSWORD="$(openssl rand -hex 32)"
SP_NODE_IMAGE=registry.gitlab.andromeda.lan/iot/ide/sbercity/sp-node:2.0.0
printf '\n' | docker run --rm -i --user 0:0 --env SP_TRUSTSTORE_PASSWORD \
  -v /opt/smart-platform/certs:/certs --entrypoint keytool "$SP_NODE_IMAGE" \
  -importkeystore -srckeystore /docker-java-home/lib/security/cacerts \
  -destkeystore /certs/cacerts -deststoretype JKS \
  -deststorepass:env SP_TRUSTSTORE_PASSWORD -noprompt
docker run --rm --user 0:0 --env SP_TRUSTSTORE_PASSWORD \
  -v /opt/smart-platform/certs:/certs --entrypoint keytool "$SP_NODE_IMAGE" \
  -importcert -noprompt -alias andromeda-root -file /certs/andromeda-ca.crt \
  -keystore /certs/cacerts -storetype JKS -storepass:env SP_TRUSTSTORE_PASSWORD
chmod 0644 /opt/smart-platform/certs/cacerts
unset SP_TRUSTSTORE_PASSWORD
```

Импортированы 148 стандартных CA и Andromeda CA. Java `TrustManagerFactory` с `-Djavax.net.ssl.trustStore=/certs/cacerts` без JVM-параметра пароля нашёл Andromeda CA и 149 доверенных издателей; проверка завершилась с кодом `0`. Поэтому пароль truststore не добавляется в `JAVA_OPTS`.

Место выполнения: **стенд, root-сеанс, только при отсутствии готового ключа и сертификата**. Отдельный самоподписанный сертификат используется MQTT, а не веб-интерфейсом. Не повторяйте генерацию при обычном перезапуске.

```bash
test ! -e /opt/smart-platform/certs/server.key || { printf 'Ключ уже существует; генерация остановлена.\n' >&2; exit 1; }
openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes \
  -keyout /opt/smart-platform/certs/server.key -out /opt/smart-platform/certs/server.crt \
  -subj '/CN=192.168.10.107' \
  -addext 'subjectAltName=IP:192.168.10.107,IP:127.0.0.1' \
  -addext 'extendedKeyUsage=serverAuth' -addext 'basicConstraints=critical,CA:FALSE'
chown 100:101 /opt/smart-platform/certs/server.key
chmod 0600 /opt/smart-platform/certs/server.key
chmod 0644 /opt/smart-platform/certs/server.crt
openssl verify -CAfile /opt/smart-platform/certs/server.crt /opt/smart-platform/certs/server.crt
```

На стенде создан такой сертификат с началом действия `2026-11-12 11:56:59 UTC` и окончанием `2027-11-12`; проверка успешна. Владелец ключа `100:101` соответствует пользователю ядра из образа. Других копий закрытого ключа не создавали. Веб-интерфейс в данной конфигурации работает по HTTP.

## 3. Создать БД и выполнить одноразовый установщик

Этот этап предназначен **только для новой БД**. Если том `sp-postgres-data` уже существует, не удаляйте его и не повторяйте установщик автоматически: сначала выясните состояние установки. Для работающей системы используйте раздел перезапуска ниже.

Место выполнения: **стенд, root-сеанс**. При ошибке любой команды остановитесь и разберите её перед продолжением.

```bash
if docker volume inspect sp-postgres-data >/dev/null 2>&1; then
  printf 'Том БД уже существует; первичная установка остановлена.\n' >&2
  exit 1
fi
export SP_DB_PASSWORD="$(openssl rand -hex 32)"
dc config --quiet
dc up -d --wait --wait-timeout 120 postgres mosquitto
dc exec -T postgres psql -U smartplatform -d smartplatform \
  -c "SELECT schema_name, schema_owner FROM information_schema.schemata WHERE schema_name='smartplatform';"
dc exec -T postgres psql -U smartplatform -d smartplatform \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='smartplatform';"
```

До установщика ожидаются схема `smartplatform`, владелец `smartplatform`, число таблиц в ней — `0`. Они создаются через `POSTGRES_DB`, `POSTGRES_USER` и `config/postgres/10-schema.sql` при первом создании кластера. Изменение этих переменных не переинициализирует существующий том.

Место выполнения: **стенд, тот же root-сеанс с `SP_DB_PASSWORD`**, только после подтверждения пустой схемы.

```bash
dc --profile install run --rm --no-deps sp-install
```

На стенде `dc config --quiet` и запуск PostgreSQL/Mosquitto завершились с кодом `0`; перед установщиком схема и владелец совпали, таблиц было `0`. Установщик с `LOAD_DEMO=true` завершился с кодом `0`, маркер: `Installation finished successfully!`. Демоданные включены явно в Compose.

В выводе установщика было предупреждение Fontconfig о недоступном каталоге кеша. `fc-cache -f` с `XDG_CACHE_HOME=/usr/share/smart-platform/.cache` выполнился без ошибок; эта переменная добавлена в общее окружение сервисов Compose.

## 4. Запустить сервисы и проверить интерфейс

Место выполнения: **стенд, тот же root-сеанс**.

```bash
dc up -d --wait --wait-timeout 300 postgres mosquitto sp-back-monolith sp-web-ui
dc ps
```

Публикуемые порты из Compose:

| Порт стенда | Назначение |
| --- | --- |
| TCP `8080` | Веб-интерфейс HTTP, с прокси API к ядру |
| TCP `9090` | HTTP ядра, внутренний порт контейнера `8080` |
| TCP `1883`, `8883` | MQTT устройств, MQTT с TLS |
| TCP `1884` | Mosquitto внешней интеграции; в текущем конфиге `allow_anonymous true` |
| TCP `5432` | PostgreSQL |
| TCP `7070` | Edge |
| UDP `5683–5688` | Транспортные порты ядра |

Healthcheck ядра проверяет только открытый TCP-порт. Для получения SPA-страницы `/login` обязателен заголовок `Accept: text/html`; он задан и в healthcheck UI в Compose, и в HTTP-скрипте проверки. Healthcheck UI ожидает HTTP `200`. Эти проверки не подтверждают авторизацию пользователя или обмен данными с устройствами.

Дефолтные учётные данные для проверки первого входа предоставлены владельцем:

| Учётная запись | Логин | Пароль |
| --- | --- | --- |
| Администратор тенанта (`USER_LOCAL`, `PASSWORD_LOCAL`) | `tenant@smart-platform-2026.ru` | `tenant` |
| Системный администратор (`SYSADMIN_USER_LOCAL`, `SYSADMIN_PASSWORD_LOCAL`) | `sysadmin@smart-platform-2026.ru` | `sysadmin` |

Учётная запись тенанта создаётся при `LOAD_DEMO=true`; эта настройка включена в данном Compose. В браузере проверьте вход обеими учётными записями и отображение соответствующей роли. Для API-проверки ниже используется системный администратор. После смены начальных паролей используйте актуальные значения.

Для первичной проверки страницы, локальных JS/CSS и API-авторизации используйте `platform-seed-smoke.py`. Он выполнил эти проверки на стенде: при отсутствии `SP_ADMIN_PASSWORD` пароль системного администратора извлекается из установочного seed работающего образа только в память и окружение процесса, без сохранения или печати. Скрипт не меняет пароль и данные платформы.

Место выполнения: **стенд, root-сеанс**.

```bash
python3 /opt/smart-platform/checks/platform-seed-smoke.py --url http://192.168.10.107:8080
```

Явно заданный `SP_ADMIN_PASSWORD` всегда имеет приоритет. После смены начального пароля передавайте новый пароль через окружение. Для явной проверки учётной записи используйте `platform-http.py`. Место выполнения: **стенд, root-сеанс**; пароль введите скрыто, не помещая его в команду или файл.

```bash
read -rp 'Учётная запись системного администратора: ' SP_ADMIN_USERNAME
read -rsp 'Пароль администратора: ' SP_ADMIN_PASSWORD
printf '\n'
export SP_ADMIN_USERNAME SP_ADMIN_PASSWORD
python3 /opt/smart-platform/checks/platform-http.py --url http://192.168.10.107:8080
unset SP_ADMIN_PASSWORD
```

Место выполнения: **рабочий компьютер в той же LAN, браузер**. Откройте `http://192.168.10.107:8080/login` и проверьте вход в интерфейс. Сертификат MQTT для этой HTTP-страницы не требуется.

## 5. Остановка и повторный запуск без переустановки

В прежнем root-сеансе `SP_DB_PASSWORD` уже есть. В новом root-сеансе повторите отключение трассировки/истории и определение функции `dc` из раздела 1. Затем восстановите пароль из окружения существующего PostgreSQL-контейнера **без его вывода**. Это возможно и для остановленного контейнера; после удаления контейнеров данный способ недоступен.

Место выполнения: **стенд, новый root-сеанс**.

```bash
SP_DB_PASSWORD="$(python3 - <<'PY'
import json, subprocess, sys
ids = subprocess.check_output(['docker', 'ps', '-aq',
    '--filter', 'label=com.docker.compose.project=smart-platform',
    '--filter', 'label=com.docker.compose.service=postgres'], text=True).split()
if len(ids) != 1:
    sys.exit('Нужен ровно один существующий PostgreSQL-контейнер проекта.')
env = json.loads(subprocess.check_output(
    ['docker', 'inspect', '--format', '{{json .Config.Env}}', ids[0]], text=True))
password = next((v.split('=', 1)[1] for v in env if v.startswith('POSTGRES_PASSWORD=')), '')
if not password:
    sys.exit('Пароль в окружении контейнера не найден.')
sys.stdout.write(password)
PY
)" && export SP_DB_PASSWORD
```

Продолжайте только при успешном восстановлении. Не сохраняйте и не публикуйте полный `docker inspect`: окружение контейнеров содержит секреты.

На стенде переменную `SP_DB_PASSWORD` удалили из окружения текущей сессии, затем восстановили из окружения существующего контейнера без печати пароля. Схема восстановления выше проверена.

Место выполнения: **стенд, root-сеанс с восстановленным паролем**. Остановка сохраняет контейнеры и тома; повторный запуск не вызывает установщик.

```bash
dc stop --timeout 30
dc up -d --wait --wait-timeout 180 postgres mosquitto sp-back-monolith sp-web-ui
dc ps
```

## Фактическая проверка результата

2026-11-12 на стенде запущены все четыре runtime-сервиса. Первый `up --wait` завершился с кодом `1` из-за отсутствия `Accept: text/html` в проверке UI. После исправления healthcheck в Compose повторный `up --wait` завершился с кодом `0`.

С рабочего компьютера в браузере по `http://192.168.10.107:8080/login` видна форма входа. HTTP-проверка получила HTML страницы и три локальных ресурса JS/CSS; `POST /api/auth/login` и `GET /api/auth/user` выполнились успешно, проверены `id`, email и роль `SYS_ADMIN`. Проверка `platform-seed-smoke.py` также успешна. В БД после установки — 73 базовые таблицы.

На MQTT TLS-порту `8883` установлено соединение TLS 1.3 с проверкой сертификата через `server.crt` и имени `192.168.10.107`. SHA-256 полученного серверного сертификата: `c41cd8da71eadfab7cd0ffc4c4c2a531c1d6e589febfa7b67f98f56023c3e19d`. С demo access token выполнены MQTT CONNECT на `1883` и `8883`, оба ответа — `CONNACK 0`, затем отключение. Токен получен из PostgreSQL только в память и не печатался. Журнал Mosquitto подтвердил соединение клиента `sp-monolith`.

Заключительная проверка выполнена `2026-11-12 12:28:38 UTC` (`15:28:38` по Москве). После удаления `SP_DB_PASSWORD` из текущего окружения и восстановления из существующего контейнера выполнены `stop --timeout 30` и `up -d --wait --wait-timeout 180`, обе команды с кодом `0`. Все четыре сервиса снова `running`; PostgreSQL, ядро и UI — `healthy`, Mosquitto работает без healthcheck. У всех контейнеров `RestartCount=0` и политика `unless-stopped`.

До и после перезапуска совпало количество следующих сущностей:

| Таблица | До | После |
| --- | --- | --- |
| `tenant` | 1 | 1 |
| `sp_user` | 6 | 6 |
| `device` | 9 | 9 |
| `dashboard` | 4 | 4 |
| `rule_chain` | 3 | 3 |

После перезапуска HTTP/API-проверка повторно завершилась с кодом `0`: HTML `/login`, три JS/CSS-ресурса, API login и получение авторизованного пользователя. На порту `8883` повторно установлено TLS 1.3-соединение.

Сохранение количества перечисленных сущностей подтверждено; полная целостность всех данных и восстановление из резервных копий не проверялись. ПК не перезагружался. Проверки не охватывают все функции IoT, Edge и AI, сквозную доставку телеметрии или работу промышленных устройств. Отдельно стоит проверить вход в нужную роль через браузер и рабочие сценарии подключаемых устройств: API-авторизация и MQTT CONNECT подтверждают только проверенные операции.
