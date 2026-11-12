# Поставка Смарт Платформы 2.0.0

Дата получения и проверки: 2026-11-12. Источник — четыре образа, ранее загруженные владельцем в Docker рабочего компьютера. Реестр из имён образов: `registry.gitlab.andromeda.lan/iot/ide/sbercity`. Во время экспорта и передачи к этому реестру не обращались.

## Переданные образы

Все образы имеют тег `2.0.0` и платформу `linux/amd64`. Полное имя составляется из указанного выше префикса и имени в таблице. Image ID подтверждены через `docker image inspect` на рабочем компьютере и повторно после `docker load` на стенде `192.168.10.107`.

| Образ | Image ID |
| --- | --- |
| `sp-web-ui` | `sha256:79e0160b9ea09f3568404c9ccbb49f66fd6bbcda537dc94195d3a7e24394a571` |
| `sp-js-executor` | `sha256:beeec95129a869a65e5a97fce3fab706ec32d263c0094679990a7feaaa260f31` |
| `sp-node` | `sha256:e9a0166a6462e69a27fddee0428fa4fb73ded2db0a3225c2b3c5ecc3aaa26d7c` |
| `sp-mqtt-transport` | `sha256:139e77ea0743a135d7c40dec4fdb0f0114eae2404b7fa3caa6b18b0c28faeed0` |

Эти же SHA указаны в RepoDigests локального Docker с соответствующими именами репозитория. Наличие тега не заменяет проверку ID при повторной установке.

## Архив и доставка

Архив создан командой `docker image save --output input/images/sp-images-2.0.0.tar` с перечислением всех четырёх полных имён образов; команда завершилась кодом `0`. Это архив Docker, а не экспорт файловой системы контейнера.

- Локальный файл: `input/images/sp-images-2.0.0.tar`.
- Размер: `1330901504` байта.
- SHA-256: `27c697e0aa51a706101b2eb14d26577a8ae16a9c9378f00d1db054246ce9a341`.
- Файл на стенде: `/home/engineer/sp-install-input/sp-images-2.0.0.tar`.
- Передача SCP завершилась кодом `0`; SHA-256 на стенде совпал с локальным.
- `sudo docker load --quiet --input /home/engineer/sp-install-input/sp-images-2.0.0.tar` завершился кодом `0`, загружены все четыре образа.

Архив в Git не включается.

## Подтверждённые особенности конфигурации

Ни один из четырёх образов не объявляет порты или тома в Docker metadata. Настройки получены из startup-скриптов, YAML/JS и ресурсов JAR. Ниже описаны все четыре образа; совместный запуск проверен только для монолитного состава из следующего раздела.

- `sp-node`: `start-sp-node.sh`, пользователь `smart-platform`, UID/GID `100:101`; одноразовый установщик включается `INSTALL_SP=true`, демоданные — `LOAD_DEMO=true`. По умолчанию БД в JDBC URL называется `smart-platform`; ручная схема из короткой инструкции не подтверждена.
- `sp-web-ui`: `node server.js`, HTTP `8090`; для отдельного ядра нужны `SP_ENABLE_PROXY=true`, `SP_HOST` и `SP_PORT`. Собственный TLS в проверенном сервере не предусмотрен.
- `sp-js-executor`: Kafka, HTTP `8888`, путь `/livenessProbe`; `in-memory` в проверенном коде не поддерживается.
- `sp-mqtt-transport`: MQTT `1883`, TLS `8883`; startup требует каталог `/config`, отсутствующий в образе. TLS ожидает пути к PEM-сертификату и ключу.

## Фактически развёрнутый монолит

Позднее владелец предоставил `input/images/docker-compose_monolith.yml`; SHA-256 оригинала `719317aaa0a9608ec060de797c143cb6396aa2fef1d975db0d26a522c25809e8`. По нему подготовлен [monolith.yml](../../deploy/compose/monolith.yml). Оригинал не изменён и не включён в Git, поскольку содержит значения настроек доступа.

Постоянно работают `postgres`, `mosquitto`, `sp-back-monolith` и `sp-web-ui`. Ядро использует `monolith`, `in-memory`, локальный JS и `caffeine`; отдельные `sp-js-executor` и `sp-mqtt-transport` из архива не запускаются. Kafka, Redis и TLS-терминатор для выбранного варианта не используются. UI работает по HTTP, MQTT устройств — также по TLS.

| Зависимость | Проверенная версия | Digest загруженного образа |
| --- | --- | --- |
| `postgres:16` | PostgreSQL 16.15 | `sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657` |
| `eclipse-mosquitto:2` | Mosquitto 2.1.2 | `sha256:75d6662dee52c947dc9dd9c29f1d8d7ffef78ddaf38001caa09755681e8bbc98` |

Зависимости получены с Docker Hub с разрешения владельца, `linux/amd64`. Compose использует `pull_policy: never`; перед повторной установкой необходимо сверять фактические ID/digest, а не только теги.

Рабочий каталог — `/opt/smart-platform`, проект Compose — `smart-platform`. БД/роль/схема — `smartplatform`. Данные и логи используют именованные тома `sp-postgres-data`, `sp-mosquitto-data`, `sp-node-logs`; все четыре сервиса имеют `restart: unless-stopped`.

Andromeda Root CA получен как `input/ca.crt`; SHA-256 файла `9de28076ed832f154c25881cdade2f0a1ccbbc360b6d215adec19ca48094c520`, fingerprint сертификата `0039310b89a8464abc053660c275f31f0b8b33663ac19ee0f609a98ec72bff31`. Он импортирован в Java truststore вместе со стандартными CA образа; Java JSSE подтвердил 149 доверенных издателей и наличие Andromeda CA. Закрытых ключей в truststore нет.

Для MQTT выпущен отдельный самоподписанный RSA3072/SHA256 сертификат с SAN IP `192.168.10.107` и `127.0.0.1`, срок действия до 2027-11-12. Fingerprint `c41cd8da71eadfab7cd0ffc4c4c2a531c1d6e589febfa7b67f98f56023c3e19d`; ключ остаётся только на стенде в `/opt/smart-platform/certs/server.key` по явному разрешению владельца, mode0600, UID/GID100:101.

`sp-install` с `INSTALL_SP=true` и `LOAD_DEMO=true` завершился кодом0 и маркером успеха; в БД 73 базовые таблицы. После штатного запуска и повторного stop/up прошли проверки HTML, трёх ресурсов UI, API login/user и MQTT TLS. До перезапуска подтверждён MQTT CONNECT с CONNACK0 на1883 и8883 с учётными данными демоустройства. Количество tenants/users/devices/dashboards/rule chains после перезапуска совпало; сбойных перезапусков контейнеров 0.

Фактический SHA-256 Compose на стенде совпадает с файлом проекта: `40ec35f47bc8736ce64377d9be1e1d15b0363cb8437c85bef1ad4153681d0117`. Полная [инструкция и границы проверки](../../docs/installation/smart-platform-2.0.0.md) описаны отдельно.
