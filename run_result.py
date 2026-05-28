# Copyright (c) 2024 PJSC VimpelCom
"""Формирование check_result для ответов POST /api/v1/run/{code}."""
import json
from typing import Any, Mapping, Optional

def detail_row_passes(row: Mapping[str, Any]) -> bool:
    """Элемент details успешен, если check истинен (bool True или строка \"true\" и т.п.)."""
    v = row.get("check")
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def coerce_details_list(details: Any) -> Optional[list[dict[str, Any]]]:
    """Нормализует details в список dict или None."""
    if details is None:
        return None
    raw = details
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
    return out


def detail_counts_from_raw(details: Any) -> tuple[Optional[list[dict[str, Any]]], int, int]:
    """
    Возвращает (список details, count_detail, success_detail).
    count_detail = len(details); success_detail = число элементов с check=true.
    """
    lst = coerce_details_list(details)
    if lst is None:
        return None, 0, 0
    success = sum(1 for row in lst if detail_row_passes(row))
    return lst, len(lst), success


def resolve_detail_fields_for_storage(
    *,
    json_details: Optional[str] = None,
    raw_details: Any = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> tuple[Optional[str], int, int]:
    """Подготовка полей для product_ff / outside_ff: json_details и счётчики из массива details."""
    source = raw_details if raw_details is not None else json_details
    lst, auto_count, auto_success = detail_counts_from_raw(source)
    if count_detail is None:
        count_detail = auto_count
    if success_detail is None:
        success_detail = auto_success
    cd = int(count_detail if count_detail is not None else auto_count)
    sd = int(success_detail if success_detail is not None else auto_success)
    if lst is not None:
        stored = json.dumps(lst, ensure_ascii=False) if lst else None
        return stored, cd, sd
    if isinstance(json_details, str) and json_details.strip():
        return json_details.strip(), cd, sd
    return None, cd, sd


def build_check_result(
    is_check: bool,
    *,
    details: Any = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> dict[str, Any]:
    """Объект check_result: is_check, details, countDetail, successDetail."""
    lst, auto_count, auto_success = detail_counts_from_raw(details)
    out: dict[str, Any] = {"is_check": is_check}
    if lst is not None:
        out["details"] = lst
        out["countDetail"] = auto_count
        out["successDetail"] = auto_success
    elif count_detail is not None or success_detail is not None:
        if count_detail is not None:
            out["countDetail"] = count_detail
        if success_detail is not None:
            out["successDetail"] = success_detail
    return out


def build_check_result_pending(call_id: str) -> dict[str, Any]:
    """Ожидание webhook для асинхронной тестовой проверки; опрос — GET /api/v1/ff/call/{callId}."""
    return {"pending": True, "callId": call_id.strip()}


def check_result_from_stored_row(
    is_check: bool,
    json_details: Optional[str] = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> dict[str, Any]:
    """check_result из сохранённых полей (product_ff / outside_ff); счётчики пересчитываются из details."""
    details_out: Any = json_details
    if json_details is not None:
        try:
            details_out = json.loads(json_details)
        except (TypeError, json.JSONDecodeError):
            details_out = json_details
    return build_check_result(
        bool(is_check),
        details=details_out,
        count_detail=count_detail,
        success_detail=success_detail,
    )


def build_check_result_from_details_list(
    is_check: bool,
    details: list[dict[str, Any]],
) -> dict[str, Any]:
    return build_check_result(is_check, details=details)


def check_result_from_outside_ff_row(row: dict) -> Optional[dict[str, Any]]:
    """check_result из строки outside_ff после webhook (только если is_check задан)."""
    if row.get("is_check") is None:
        return None
    return check_result_from_stored_row(
        bool(row["is_check"]),
        json_details=row.get("json_details"),
        count_detail=row.get("count_detail"),
        success_detail=row.get("success_detail"),
    )
