# Установочный стенд

Данные предоставлены владельцем проекта 2026-11-12. Окружение проверено по SSH в тот же день. Образы 2.0.0 загружены в Docker после сверки архива; сервисы Смарт Платформы ещё не запускались.

| Параметр | Значение |
| --- | --- |
| Адрес в локальной сети | `192.168.10.107` |
| Учётная запись | `engineer` |
| Переменная окружения для адреса | `SP_STAND_HOST` |
| Переменная окружения для учётной записи | `SP_STAND_USER` |
| Переменная окружения для пароля | `SP_STAND_PASSWORD` |
| Способ подключения и порт | [ПРОВЕРЕНО] SSH, TCP 22; вход engineer с паролем, повышение прав через sudo |
| ОС | [ПРОВЕРЕНО] Ubuntu 26.04.1 LTS, Resolute Raccoon, `resolute` |
| Архитектура и CPU | [ПРОВЕРЕНО] x86_64 / linux/amd64, 4 доступных CPU |
| Память | [ПРОВЕРЕНО] `free -h`: 14 GiB RAM, 4 GiB swap |
| Корневая ФС | [ПРОВЕРЕНО] 98 GiB всего, 86 GiB свободно при проверке |
| Docker Engine | [ПРОВЕРЕНО] 29.1.3; пакет `docker.io` 29.1.3-0ubuntu4.1 |
| Docker Compose | [ПРОВЕРЕНО] 2.40.3+ds1-0ubuntu1; пакет `docker-compose-v2` |
| containerd / runc | [ПРОВЕРЕНО] пакеты 2.2.2-0ubuntu1.2 / 1.4.0-0ubuntu1 |
| Docker daemon | [ПРОВЕРЕНО] active, enabled; overlayfs, cgroup systemd; `/var/lib/docker` |
| Доступ к Docker | [ПРОВЕРЕНО] через sudo; engineer не входит в группу docker |
| Поставка в Docker | [ПРОВЕРЕНО] загружены sp-node, sp-web-ui, sp-js-executor, sp-mqtt-transport версии 2.0.0, linux/amd64; ID в [инвентаре поставки](../../releases/smart-platform-2.0.0.md) |
| Запущенная платформа | Контейнеры ещё не запускались |
| Путь платформы и данных | Не создан; зависит от фактической поставки |
| Часовой пояс стенда | [ПРОВЕРЕНО] Etc/UTC; время в пользовательских отчётах приводится с указанием зоны |

Шаблон имён переменных и несекретных значений: [.env.example](.env.example). Значение пароля в шаблоне отсутствует; его нужно передать в окружение процесса перед подключением.

[ПРОВЕРЕНО] Источники: `/etc/os-release`, `id`, `nproc`, `free -h`, `df -h /`, `dpkg-query`, `sudo docker version`, `sudo docker compose version`, `sudo docker info`, `systemctl is-active/is-enabled docker`, `sudo docker ps -a`, `sudo docker image ls`. Проверки завершились успешно, кроме ожидаемого отказа Docker без sudo. Локальный отчёт запуска сохранён в `reports/stand-192.168.10.107-20261112T105906Z-environment.md` и исключён из Git.

Фактическая установка Docker восстановлена по журналам sudo и APT: [подготовка Ubuntu 26.04](../../../docs/installation/ubuntu-26-docker.md). Пакеты и настройки Docker стенда в ходе проверки не менялись. Позднее создан `/home/engineer/sp-install-input`, передан архив и выполнен `docker load`; локальный отчёт — `reports/stand-192.168.10.107-20261112-image-load.md`. Наличие рабочего Docker и образов не подтверждает установку платформы: Compose, миграции, TLS и веб-интерфейс ещё не проверены.
