#!/usr/bin/env python3
"""
Скрипт проверки DEMOFF-2.
Вызывается тот же метод FDM, что и в DEMOFF-1.
Проверка пройдена, если у каждого объекта в массиве interfaces каждого контейнера
есть непустой массив operations; иначе — не пройдена.
Дополнительно заполняются success_detail/count_detail/json_details.
"""
import json
import os
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

FDM_BASE_URL = "https://fdm-products-dev-eafdmmart.apps.yd-m6-kt22.vimpelcom.ru"

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


def compute_interface_stats(cmdb_code: str):
    """
    Возвращает (is_check, success_detail, count_detail, json_details) по интерфейсам.
    """
    containers = _fetch_containers(cmdb_code)
    if containers is None:
        return False, 0, 0, None

    count_detail = 0
    success_detail = 0
    details = []

    for container in containers:
        interfaces = container.get("interfaces") if isinstance(container, dict) else None
        if not isinstance(interfaces, list):
            continue
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            count_detail += 1
            operations = iface.get("operations")
            ok = isinstance(operations, list) and len(operations) > 0
            if ok:
                success_detail += 1
            details.append(
                {
                    "check": "true" if ok else "false",
                    "interfaceName": iface.get("name"),
                    "interfaceCode": iface.get("code"),
                }
            )

    if count_detail == 0:
        return False, 0, 0, None

    is_check = success_detail == count_detail
    json_details = json.dumps(details, ensure_ascii=False)
    return is_check, success_detail, count_detail, json_details


def execute(app_code: str) -> None:
    is_check, success_detail, count_detail, json_details = compute_interface_stats(app_code)
    run_check(
        app_code,
        SCRIPT_CODE,
        is_check=is_check,
        success_detail=success_detail,
        count_detail=count_detail,
        json_details=json_details,
    )
