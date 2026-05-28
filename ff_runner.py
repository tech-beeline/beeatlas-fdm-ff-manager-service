# Copyright (c) 2024 PJSC VimpelCom
"""Запуск проверки: скрипт на диске или внешний POST по fitness_function.method."""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional, Tuple

from config import settings
from db import (
    delete_outside_ff_by_call_id,
    get_fitness_function_by_code,
    insert_outside_ff_call,
    process_ff_webhook,
)
from ff_status import FF_STATUS_TEST, skips_product_ff_persistence
from run_result import (
    build_check_result,
    build_check_result_pending,
    check_result_from_stored_row,
    resolve_detail_fields_for_storage,
)
from script_runner import run_script

CheckResultPayload = Optional[dict[str, Any]]


def _url_with_doc_id_query(url: str, doc_id: Optional[str]) -> str:
    """Добавляет query-параметр docId, если задан (для HTTP-проверок)."""
    if not doc_id or not str(doc_id).strip():
        return url
    parsed = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    q["docId"] = str(doc_id).strip()
    new_query = urllib.parse.urlencode(q)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def _post_json(url: str, body: dict, timeout: float) -> Tuple[int, str]:
    """Синхронный POST с JSON-телом. Возвращает (status_code, response_text)."""
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.getcode() or 200, text
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, txt


def _parse_sync_http_response_json(text: str, expected_call_id: str) -> tuple[bool, Optional[str], Optional[int], Optional[int]]:
    """
    Разбирает тело ответа синхронной HTTP-проверки (как тело POST /api/v1/ff/webhook).
    Возвращает (is_check, json_details для БД, count_detail, success_detail).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Ответ не является корректным JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("Ответ должен быть JSON-объектом")
    cid = data.get("callId") or data.get("call_id")
    if cid != expected_call_id:
        raise ValueError("Поле callId в ответе должно совпадать с исходящим вызовом")
    if "isCheck" in data:
        is_check = data["isCheck"]
    elif "is_check" in data:
        is_check = data["is_check"]
    else:
        raise ValueError("В ответе отсутствует isCheck")
    if not isinstance(is_check, bool):
        raise ValueError("isCheck должен быть логическим значением (boolean)")
    raw_details = data.get("details")
    if raw_details is None and "json_details" in data:
        raw_details = data["json_details"]
    cd_raw = data.get("countDetail") if "countDetail" in data else data.get("count_detail")
    sd_raw = data.get("successDetail") if "successDetail" in data else data.get("success_detail")
    count_detail: Optional[int] = None
    success_detail: Optional[int] = None
    if isinstance(cd_raw, int):
        count_detail = cd_raw
    elif cd_raw is not None:
        try:
            count_detail = int(cd_raw)
        except (TypeError, ValueError):
            count_detail = None
    if isinstance(sd_raw, int):
        success_detail = sd_raw
    elif sd_raw is not None:
        try:
            success_detail = int(sd_raw)
        except (TypeError, ValueError):
            success_detail = None
    json_details, count_detail, success_detail = resolve_detail_fields_for_storage(
        raw_details=raw_details,
        count_detail=count_detail,
        success_detail=success_detail,
    )
    return is_check, json_details, count_detail, success_detail


def run_ff_check(
    code: str,
    app_mnemonic: str,
    *,
    structurizr_credentials: Optional[Tuple[str, str]] = None,
    data: Optional[dict[str, Any]] = None,
    doc_id: Optional[str] = None,
) -> Tuple[bool, str, CheckResultPayload]:
    """
    Если в БД у фитнес-функции задан непустой method (URL) — POST { callId, productCode }, запись в outside_ff.
    method_synchronous: ответ обрабатывается сразу (как webhook) или ожидается POST /api/v1/ff/webhook.
    doc_id: при передаче в run добавляется query-параметр docId к URL method.
    Иначе — запуск скрипта .py как раньше.
    Возвращает (успех, сообщение, check_result: объект с is_check/details или None для асинхронной внешней проверки).
    Для status=TEST результат в product_ff не сохраняется; детали отдаются в check_result.
    """
    row = get_fitness_function_by_code(code.strip())
    ff_status = (row.get("status") or FF_STATUS_TEST).strip().upper() if row else FF_STATUS_TEST
    is_test_mode = skips_product_ff_persistence(ff_status)
    if row:
        method = (row.get("method") or "").strip()
        if method:
            synchronous = bool(row.get("method_synchronous"))
            return _invoke_external_post(
                method,
                row["id"],
                app_mnemonic,
                synchronous=synchronous,
                doc_id=doc_id,
                is_test_mode=is_test_mode,
            )

    return run_script(
        code,
        app_mnemonic,
        structurizr_credentials=structurizr_credentials,
        data=data,
        is_test_mode=is_test_mode,
    )


def _invoke_external_post(
    url: str,
    ff_id: int,
    app_mnemonic: str,
    *,
    synchronous: bool,
    doc_id: Optional[str],
    is_test_mode: bool,
) -> Tuple[bool, str, CheckResultPayload]:
    call_id = str(uuid.uuid4())
    product_code = app_mnemonic.strip()
    insert_outside_ff_call(ff_id=ff_id, product_code=product_code, call_id=call_id)
    target_url = _url_with_doc_id_query(url, doc_id)
    try:
        status, text = _post_json(
            target_url,
            {"callId": call_id, "productCode": product_code},
            settings.external_ff_timeout_seconds,
        )
        if not (200 <= status < 300):
            delete_outside_ff_by_call_id(call_id)
            detail = text.strip()[:500]
            return False, f"Внешний сервис вернул HTTP {status}" + (f": {detail}" if detail else ""), None

        if not synchronous:
            if is_test_mode:
                return (
                    True,
                    (
                        f"Внешняя проверка вызвана в асинхронном режиме (method_synchronous=false, "
                        f"callId={call_id}). После POST /api/v1/ff/webhook опросите "
                        f"GET /api/v1/ff/call/{call_id}."
                    ),
                    build_check_result_pending(call_id),
                )
            return (
                True,
                f"Внешняя проверка вызвана (callId={call_id}). Результат запишется через POST /api/v1/ff/webhook.",
                None,
            )

        try:
            is_check, json_details, count_detail, success_detail = _parse_sync_http_response_json(
                text, call_id
            )
        except ValueError as e:
            delete_outside_ff_by_call_id(call_id)
            return False, str(e), None

        outcome = process_ff_webhook(
            call_id,
            is_check,
            json_details=json_details,
            count_detail=count_detail,
            success_detail=success_detail,
        )
        if outcome == "not_found":
            delete_outside_ff_by_call_id(call_id)
            return False, "Внутренняя ошибка: запись outside_ff не найдена после вызова", None

        msg = f"Синхронная внешняя проверка выполнена (callId={call_id})."
        check_payload = check_result_from_stored_row(
            is_check,
            json_details=json_details,
            count_detail=count_detail,
            success_detail=success_detail,
        )
        if is_test_mode:
            msg += " Результат не сохранён (статус TEST)."
        return True, msg, check_payload

    except (urllib.error.URLError, OSError, ValueError) as e:
        delete_outside_ff_by_call_id(call_id)
        return False, f"Ошибка HTTP-запроса к внешнему URL: {e}", None
