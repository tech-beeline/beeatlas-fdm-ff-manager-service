#!/usr/bin/env python3
"""
Скрипт проверки DEMOFF-1.
Новая логика:
- по коду продукта (cmdb) вызывает внешний сервис FDM;
- если для продукта есть хотя бы один контейнер, is_check = true;
- если контейнеров нет — is_check = false.
Результат возвращается из execute(); сохранение в API выполняет раннер.
"""
import json
import os
import sys
import urllib.parse

# Код проверки = имя файла без расширения
SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

# Добавляем родительский каталог в path для импорта _common
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ExecuteResult, structurizr_http_client


def _fetch_containers(cmdb_code: str):
    """Возвращает список контейнеров от FDM или None при ошибке."""
    if not cmdb_code:
        return None

    safe_code = urllib.parse.quote(cmdb_code, safe="")
    path = f"/product/api/v1/product/{safe_code}/container"
    try:
        client = structurizr_http_client()
        status, raw_bytes = client.get(path)
    except Exception as exc:
        print(f"Ошибка вызова HMAC-сервиса: {exc}", file=sys.stderr)
        return None

    if not (200 <= status < 300):
        print(f"HMAC-сервис вернул HTTP {status}", file=sys.stderr)
        return None

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except ValueError:
        print("Некорректный JSON от FDM-сервиса", file=sys.stderr)
        return None

    if not isinstance(data, list):
        print("Неожиданный формат ответа FDM (ожидался список контейнеров)", file=sys.stderr)
        return None

    return data


def execute(app_code: str) -> ExecuteResult:
    containers = _fetch_containers(app_code)
    details: list[dict] = []
    if containers:
        for c in containers:
            if not isinstance(c, dict):
                continue
            details.append(
                {
                    "check": True,
                    "containerName": c.get("name"),
                    "containerCode": c.get("code"),
                }
            )
    return ExecuteResult(
        app_code=app_code,
        script_code=SCRIPT_CODE,
        is_check=len(details) > 0,
        details=details,
    )
