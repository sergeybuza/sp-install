#!/usr/bin/env python3
"""На целевом ПК: однократная инициализация новой БД Смарт Платформы 2.0.0.

Требует Docker через root/sudo и SP_DB_PASSWORD в окружении. Проверяет пустую
схему перед установкой. Вывод установщика хранится только в памяти и не
печатается: он может содержать секреты. При ошибке автоматического повтора нет.
"""

import os
from pathlib import Path
import subprocess
import sys


COMPOSE_FILE = Path("/opt/smart-platform/compose/monolith.yml")
COMPOSE = [
    "docker", "compose", "--project-name", "smart-platform",
    "--file", str(COMPOSE_FILE),
]


def run(arguments, timeout):
    return subprocess.run(
        COMPOSE + arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def main():
    if not os.environ.get("SP_DB_PASSWORD"):
        sys.exit("STOP: задайте SP_DB_PASSWORD через окружение.")
    if not COMPOSE_FILE.is_file():
        sys.exit("STOP: файл Compose установки не найден.")

    query = (
        "SELECT schema_owner FROM information_schema.schemata "
        "WHERE schema_name='smartplatform'; "
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='smartplatform';"
    )
    result = run([
        "exec", "-T", "postgres", "psql", "--no-psqlrc", "--tuples-only",
        "--no-align", "--set", "ON_ERROR_STOP=1", "-U", "smartplatform",
        "-d", "smartplatform", "-c", query,
    ], timeout=30)
    if result.returncode != 0:
        sys.exit("STOP: не удалось проверить состояние БД; установщик не запущен.")
    if result.stdout.decode("utf-8").splitlines() != ["smartplatform", "0"]:
        sys.exit(
            "STOP: схема отсутствует, имеет другого владельца или непуста; "
            "установщик не запущен."
        )
    print("OK: схема smartplatform, владелец smartplatform, таблиц 0", flush=True)

    result = run([
        "--profile", "install", "run", "--rm", "--no-deps", "sp-install",
    ], timeout=900)
    success = b"Installation finished successfully!" in result.stdout + result.stderr
    print(f"Установщик: код {result.returncode}, маркер успеха {'есть' if success else 'нет'}")
    if result.returncode != 0 or not success:
        sys.exit(
            "STOP: установка не подтверждена. Проверьте контейнеры и состояние "
            "БД перед дальнейшими действиями; автоматический повтор запрещён."
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        sys.exit(
            "STOP: тайм-аут. Установщик мог продолжить работу в контейнере; "
            "проверьте его состояние и БД, не повторяйте запуск автоматически."
        )
    except (OSError, UnicodeError):
        sys.exit("STOP: ошибка запуска или чтения результата; проверьте состояние установки.")
