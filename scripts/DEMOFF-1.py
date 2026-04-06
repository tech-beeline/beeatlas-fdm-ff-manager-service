#!/usr/bin/env python3
"""
Скрипт проверки DEMOFF-1.
Новая логика:
- по коду продукта (cmdb) вызывает внешний сервис FDM;
- если для продукта есть хотя бы один контейнер, через API пишется is_check = true;
- если контейнеров нет — is_check = false.
Дополнительно заполняются success_detail/count_detail/json_details.
"""
import json
import os
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Код проверки = имя файла без расширения
SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

FDM_BASE_URL = "https://fdm-products-dev-eafdmmart.apps.yd-m6-kt22.vimpelcom.ru"

# Добавляем родительский каталог в path для импорта _common
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import run_check


def _fetch_containers(cmdb_code: str):
    """Возвращает список контейнеров от FDM или None при ошибке."""
    if not cmdb_code:
        return None

    url = f"{FDM_BASE_URL}/api/v1/product/{cmdb_code}/container"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
    except (URLError, HTTPError, OSError) as exc:
        print(f"Ошибка вызова FDM-сервиса: {exc}", file=sys.stderr)
        return None

    try:
        data = json.loads(raw)
    except ValueError:
        print("Некорректный JSON от FDM-сервиса", file=sys.stderr)
        return None

    if not isinstance(data, list):
        print("Неожиданный формат ответа FDM (ожидался список контейнеров)", file=sys.stderr)
        return None

    return data


if __name__ == "__main__":
    app_code = sys.argv[1] if len(sys.argv) > 1 else ""
    containers = _fetch_containers(app_code)
    if not containers:
        run_check(
            app_code,
            SCRIPT_CODE,
            is_check=False,
            success_detail=0,
            count_detail=0,
            json_details=None,
        )
    else:
        count = len(containers)
        details = []
        for c in containers:
            if not isinstance(c, dict):
                continue
            details.append(
                {
                    "check": "true",
                    "containerName": c.get("name"),
                    "containerCode": c.get("code"),
                }
            )
        json_details = json.dumps(details, ensure_ascii=False)
        run_check(
            app_code,
            SCRIPT_CODE,
            is_check=True,
            success_detail=count,
            count_detail=count,
            json_details=json_details,
        )
