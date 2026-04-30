#!/usr/bin/env python3
"""
Скрипт проверки DEMOFF-3.
Вызывается тот же метод FDM, что и в DEMOFF-1/DEMOFF-2.
Проверка пройдена, если у каждого элемента в массивах operations есть sla
с атрибутами rps, errorRate, latency: ни один не null, rps и latency не равны 0.
Результат возвращается из execute(); сохранение и подсчёт detail выполняет раннер.
"""
import os
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

FDM_BASE_URL = "https://fdm-products-dev-eafdmmart.apps.yd-m6-kt22.vimpelcom.ru"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ExecuteResult, detail_row_passes


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


def _sla_valid(sla) -> bool:
    """sla должен быть объектом с rps, errorRate, latency; ни один не null; rps и latency не 0."""
    if not isinstance(sla, dict):
        return False
    rps = sla.get("rps")
    error_rate = sla.get("errorRate")
    latency = sla.get("latency")
    if rps is None or error_rate is None or latency is None:
        return False
    if rps == 0 or latency == 0:
        return False
    return True


def compute_operation_stats(cmdb_code: str):
    """
    Возвращает (is_check, details) по операциям (каждая операция — элемент details).
    """
    containers = _fetch_containers(cmdb_code)
    if containers is None:
        return False, []

    details = []

    for container in containers:
        if not isinstance(container, dict):
            continue
        interfaces = container.get("interfaces")
        if not isinstance(interfaces, list):
            continue
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            operations = iface.get("operations")
            if not isinstance(operations, list):
                continue
            for op in operations:
                if not isinstance(op, dict):
                    continue
                sla = op.get("sla")
                ok = _sla_valid(sla)
                op_name = op.get("name") or ""
                op_type = op.get("type") or ""
                details.append(
                    {
                        "check": ok,
                        "operationName": f"{op_name} {op_type}".strip(),
                    }
                )

    if len(details) == 0:
        return False, []

    is_check = all(detail_row_passes(d) for d in details)
    return is_check, details


def execute(app_code: str) -> ExecuteResult:
    is_check, details = compute_operation_stats(app_code)
    return ExecuteResult(
        app_code=app_code,
        script_code=SCRIPT_CODE,
        is_check=is_check,
        details=details,
    )
