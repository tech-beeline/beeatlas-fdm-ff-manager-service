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


def build_check_result(
    is_check: bool,
    *,
    details: Any = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> dict[str, Any]:
    """Объект check_result: is_check и опционально details, countDetail, successDetail."""
    out: dict[str, Any] = {"is_check": is_check}
    if details is not None:
        if isinstance(details, str):
            try:
                out["details"] = json.loads(details)
            except (TypeError, json.JSONDecodeError):
                out["details"] = details
        else:
            out["details"] = details
    if count_detail is not None:
        out["countDetail"] = count_detail
    if success_detail is not None:
        out["successDetail"] = success_detail
    return out


def build_check_result_pending(call_id: str) -> dict[str, Any]:
    """Ожидание webhook для асинхронной тестовой проверки; опрос — GET /api/v1/ff/call/{callId}."""
    return {"pending": True, "callId": call_id.strip()}


def check_result_from_outside_ff_row(row: dict) -> Optional[dict[str, Any]]:
    """check_result из строки outside_ff после webhook (только если is_check задан)."""
    if row.get("is_check") is None:
        return None
    details = None
    raw = row.get("json_details")
    if raw is not None:
        if isinstance(raw, str):
            try:
                details = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                details = raw
        else:
            details = raw
    return build_check_result(
        bool(row["is_check"]),
        details=details,
        count_detail=row.get("count_detail"),
        success_detail=row.get("success_detail"),
    )


def build_check_result_from_details_list(
    is_check: bool,
    details: list[dict[str, Any]],
) -> dict[str, Any]:
    count_detail = len(details)
    success_detail = sum(1 for row in details if detail_row_passes(row))
    details_out: Optional[list] = details if details else None
    return build_check_result(
        is_check,
        details=details_out,
        count_detail=count_detail,
        success_detail=success_detail,
    )
