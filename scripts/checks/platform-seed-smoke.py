#!/usr/bin/env python3
"""На стенде: проверка UI/API с паролем из env или seed работающего образа.

Требуется доступ к Docker. JAR читается через stdout docker cp и разбирается
только в памяти. Пароль и токены не записываются в файлы и не печатаются.
Скрипт не меняет пароль или данные платформы: выполняется только HTTP smoke.
"""

import argparse
import io
import os
from pathlib import Path
import re
import runpy
import struct
import subprocess
import sys
import tarfile
import zipfile


USERNAME = "sysadmin@smart-platform-2026.ru"
CONTAINER = "smart-platform-sp-back-monolith-1"
JAR_PATH = "/usr/share/smart-platform/bin/smart-platform.jar"
LOADER_CLASS = (
    "ru/iot/smartplatform/server/service/install/DefaultSystemDataLoaderService"
)
CLASS_ENTRY = "BOOT-INF/classes/" + LOADER_CLASS + ".class"
AUTHORITY_CLASS = "ru/iot/smartplatform/server/common/data/security/Authority"
AUTHORITY_DESCRIPTOR = "L" + AUTHORITY_CLASS + ";"
CREATE_USER_DESCRIPTOR = (
    "(Lru/iot/smartplatform/server/common/data/security/Authority;"
    "Lru/iot/smartplatform/server/common/data/id/TenantId;"
    "Lru/iot/smartplatform/server/common/data/id/CustomerId;"
    "Ljava/lang/String;Ljava/lang/String;)"
    "Lru/iot/smartplatform/server/common/data/User;"
)
MAX_JAR_BYTES = 512 * 1024 * 1024


class ClassReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("Некорректная структура класса")
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def u1(self):
        return self.take(1)[0]

    def u2(self):
        return struct.unpack(">H", self.take(2))[0]

    def u4(self):
        return struct.unpack(">I", self.take(4))[0]


def pool_entry(pool, index, tag):
    if not 0 < index < len(pool) or pool[index] is None or pool[index][0] != tag:
        raise ValueError("Неожиданный тип constant pool")
    return pool[index]


def utf8(pool, index):
    return pool_entry(pool, index, 1)[1]


def class_name(pool, index):
    return utf8(pool, pool_entry(pool, index, 7)[1])


def string_constant(pool, index):
    return utf8(pool, pool_entry(pool, index, 8)[1])


def member_reference(pool, index, tag):
    _, owner, name_and_type = pool_entry(pool, index, tag)
    _, name, descriptor = pool_entry(pool, name_and_type, 12)
    return class_name(pool, owner), utf8(pool, name), utf8(pool, descriptor)


def constant_pool(reader):
    pool = [None] * reader.u2()
    index = 1
    while index < len(pool):
        tag = reader.u1()
        if tag == 1:
            pool[index] = (tag, reader.take(reader.u2()).decode("utf-8"))
        elif tag in (3, 4):
            reader.take(4)
            pool[index] = (tag,)
        elif tag in (5, 6):
            reader.take(8)
            pool[index] = (tag,)
            index += 1
        elif tag in (7, 8, 16, 19, 20):
            pool[index] = (tag, reader.u2())
        elif tag in (9, 10, 11, 12, 17, 18):
            pool[index] = (tag, reader.u2(), reader.u2())
        elif tag == 15:
            pool[index] = (tag, reader.u1(), reader.u2())
        else:
            raise ValueError("Неподдерживаемый constant pool")
        index += 1
    return pool


def attributes(reader, pool):
    result = []
    for _ in range(reader.u2()):
        name = utf8(pool, reader.u2())
        result.append((name, reader.take(reader.u4())))
    return result


def sysadmin_code(data):
    reader = ClassReader(data)
    if reader.take(4) != b"\xca\xfe\xba\xbe":
        raise ValueError("Ожидался JVM class")
    reader.take(4)  # minor/major version; формат проверяется при разборе pool.
    pool = constant_pool(reader)
    reader.u2()  # access flags
    if class_name(pool, reader.u2()) != LOADER_CLASS:
        raise ValueError("Неожиданный класс установщика")
    reader.u2()  # superclass
    reader.take(2 * reader.u2())  # interfaces
    for _ in range(reader.u2()):
        reader.take(6)  # flags/name/descriptor of a field
        attributes(reader, pool)
    code_attributes = []
    matched_methods = 0
    for _ in range(reader.u2()):
        reader.u2()
        name, descriptor = utf8(pool, reader.u2()), utf8(pool, reader.u2())
        method_attributes = attributes(reader, pool)
        if name == "createSysAdmin":
            matched_methods += 1
            if descriptor != "()V":
                raise ValueError("Неожиданная сигнатура createSysAdmin")
            code_attributes.extend(blob for attr, blob in method_attributes if attr == "Code")
    if matched_methods != 1 or len(code_attributes) != 1:
        raise ValueError("Не найден единственный createSysAdmin Code")
    code_reader = ClassReader(code_attributes[0])
    code_reader.take(4)  # max_stack, max_locals
    code_length = code_reader.u4()
    if not 0 < code_length <= 64:
        raise ValueError("Неожиданная длина createSysAdmin")
    code = code_reader.take(code_length)
    if code_reader.u2() != 0:
        raise ValueError("Неожиданные обработчики исключений createSysAdmin")
    return pool, ClassReader(code)


def expect_opcode(reader, opcode):
    if reader.u1() != opcode:
        raise ValueError("Неожиданная схема createSysAdmin")


def ldc_string_index(reader, pool):
    opcode = reader.u1()
    if opcode == 0x12:  # ldc
        index = reader.u1()
    elif opcode == 0x13:  # ldc_w
        index = reader.u2()
    else:
        raise ValueError("Ожидался String ldc createSysAdmin")
    pool_entry(pool, index, 8)
    return index


def install_seed_environment(jar_bytes):
    with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
        entries = [item for item in jar.infolist() if item.filename == CLASS_ENTRY]
        if len(entries) != 1 or not 0 < entries[0].file_size <= 2 * 1024 * 1024:
            raise ValueError("Не найден ожидаемый класс установщика")
        pool, code = sysadmin_code(jar.read(entries[0]))

    # Проверенная схема 2.0.0: this, Authority.SYS_ADMIN, null tenant,
    # null customer, String username, String password -> createUser(...).
    # Любое изменение bytecode отклоняется, произвольные строки pool не берутся.
    expect_opcode(code, 0x2A)  # aload_0
    expect_opcode(code, 0xB2)  # getstatic
    if member_reference(pool, code.u2(), 9) != (
        AUTHORITY_CLASS, "SYS_ADMIN", AUTHORITY_DESCRIPTOR
    ):
        raise ValueError("Неожиданная роль createSysAdmin")
    expect_opcode(code, 0x01)  # aconst_null
    expect_opcode(code, 0x01)
    login_index = ldc_string_index(code, pool)
    password_index = ldc_string_index(code, pool)
    if string_constant(pool, login_index) != USERNAME:
        raise ValueError("Неожиданная учётная запись seed")
    expect_opcode(code, 0xB6)  # invokevirtual
    if member_reference(pool, code.u2(), 10) != (
        LOADER_CLASS, "createUser", CREATE_USER_DESCRIPTOR
    ):
        raise ValueError("Неожиданный вызов createUser")
    expect_opcode(code, 0x57)  # pop
    expect_opcode(code, 0xB1)  # return
    if code.pos != len(code.data) or not string_constant(pool, password_index):
        raise ValueError("Неожиданное окончание createSysAdmin")
    os.environ["SP_ADMIN_PASSWORD"] = string_constant(pool, password_index)


def container_jar(container):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise ValueError("Некорректное имя контейнера")
    result = subprocess.run(
        ["docker", "cp", container + ":" + JAR_PATH, "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Не удалось прочитать JAR контейнера через docker cp")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:*") as archive:
        entries = [member for member in archive.getmembers() if member.isfile()]
        if (
            len(entries) != 1
            or Path(entries[0].name).name != "smart-platform.jar"
            or not 0 < entries[0].size <= MAX_JAR_BYTES
        ):
            raise ValueError("Неожиданный архив docker cp")
        stream = archive.extractfile(entries[0])
        if stream is None:
            raise ValueError("JAR отсутствует в архиве docker cp")
        return stream.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://192.168.10.107:8080")
    parser.add_argument("--container", default=CONTAINER)
    parser.add_argument("--ca-cert", help="Публичный сертификат для HTTPS")
    args = parser.parse_args()
    if not os.environ.get("SP_ADMIN_PASSWORD"):
        if os.environ.get("SP_ADMIN_USERNAME", USERNAME) != USERNAME:
            raise ValueError("Для другой учётной записи задайте пароль через окружение")
        os.environ["SP_ADMIN_USERNAME"] = USERNAME
        install_seed_environment(container_jar(args.container))

    checker = Path(__file__).with_name("platform-http.py")
    if not checker.is_file():
        raise ValueError("Рядом должен находиться platform-http.py")
    sys.argv = [str(checker), "--url", args.url]
    if args.ca_cert:
        sys.argv.extend(["--ca-cert", args.ca_cert])
    runpy.run_path(str(checker), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, struct.error, subprocess.SubprocessError,
            tarfile.TarError, zipfile.BadZipFile):
        # Не выводим exception text, stdout/stderr docker, пароль или конфигурацию.
        print("FAIL: не удалось безопасно выполнить проверку UI/API", file=sys.stderr)
        sys.exit(1)
