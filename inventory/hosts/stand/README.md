# Установочный стенд

Данные предоставлены владельцем проекта 2026-11-12. В тот же день на стенде развёрнута Смарт Платформа 2.0.0 по монолитной схеме. Итоговая проверка после перезапуска: 15:28:38 по Москве (12:28:38 UTC).

| Параметр | Значение |
| --- | --- |
| Адрес в локальной сети | `192.168.10.107` |
| Учётная запись | `engineer` |
| Переменная окружения для адреса | `SP_STAND_HOST` |
| Переменная окружения для учётной записи | `SP_STAND_USER` |
| Переменная окружения для пароля | `SP_STAND_PASSWORD` |
| Способ подключения и порт | SSH, TCP 22; вход engineer с паролем, повышение прав через sudo |
| ОС | Ubuntu 26.04.1 LTS, Resolute Raccoon, `resolute` |
| Архитектура и CPU | x86_64 / linux/amd64, 4 доступных CPU |
| Память | `free -h`: 14 GiB RAM, 4 GiB swap |
| Корневая ФС | 98 GiB всего, 86 GiB свободно при проверке |
| Docker Engine | 29.1.3; пакет `docker.io` 29.1.3-0ubuntu4.1 |
| Docker Compose | 2.40.3+ds1-0ubuntu1; пакет `docker-compose-v2` |
| containerd / runc | пакеты 2.2.2-0ubuntu1.2 / 1.4.0-0ubuntu1 |
| Docker daemon | active, enabled; overlayfs, cgroup systemd; `/var/lib/docker` |
| Доступ к Docker | через sudo; engineer не входит в группу docker |
| Поставка в Docker | загружены sp-node, sp-web-ui, sp-js-executor, sp-mqtt-transport версии 2.0.0, linux/amd64; ID в [инвентаре поставки](../../releases/smart-platform-2.0.0.md) |
| Зависимости | PostgreSQL 16.15, Mosquitto 2.1.2; digest в инвентаре поставки |
| Запущенная платформа | postgres, mosquitto, sp-back-monolith, sp-web-ui; все running, PG/core/UI healthy; перезапусков вследствие сбоя 0 |
| Веб-интерфейс | http://192.168.10.107:8080/login, форма входа в браузере и авторизация API |
| Путь конфигурации | `/opt/smart-platform/compose/monolith.yml`, `/opt/smart-platform/config/` |
| Данные | Именованные тома `sp-postgres-data`, `sp-mosquitto-data`, `sp-node-logs` |
| БД | `smartplatform`, роль и схема `smartplatform`; 73 базовые таблицы после установки |
| TLS и CA | Andromeda Root CA в `/opt/smart-platform/certs/cacerts`; MQTT TLS с самоподписанным сертификатом и ключом только в `/opt/smart-platform/certs/server.key`, mode0600, UID/GID100:101 |
| Запуск при загрузке | Docker enabled, политики контейнеров unless-stopped; сама перезагрузка ПК не выполнялась |
| Часовой пояс стенда | Etc/UTC; время в пользовательских отчётах приводится с указанием зоны |

Шаблон имён переменных и несекретных значений: [.env.example](.env.example). Значение пароля в шаблоне отсутствует; его нужно передать в окружение процесса перед подключением.

Источники: `/etc/os-release`, `id`, `nproc`, `free -h`, `df -h /`, `dpkg-query`, `sudo docker version`, `sudo docker compose version`, `sudo docker info`, `systemctl is-active/is-enabled docker`, `sudo docker ps -a`, `sudo docker image ls`. Проверки завершились успешно, кроме ожидаемого отказа Docker без sudo. Локальный отчёт запуска сохранён в `reports/stand-192.168.10.107-20261112T105906Z-environment.md` и исключён из Git.

Фактическая установка Docker восстановлена по журналам sudo и APT: [подготовка Ubuntu 26.04](../../../docs/installation/ubuntu-26-docker.md). Пакеты и настройки Docker стенда в ходе работы не менялись. Созданы каталог входной поставки, рабочая конфигурация, сертификаты и Docker-ресурсы платформы. Результат описан в [инструкции](../../../docs/installation/smart-platform-2.0.0.md); локальный отчёт запуска — `reports/stand-192.168.10.107-20261112T122838Z-monolith.md`, исключён из Git.
