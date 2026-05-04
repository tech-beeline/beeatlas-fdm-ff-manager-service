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
    json_details_value_to_db_str,
    process_ff_webhook,
)
from script_runner import run_script


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
    json_details = json_details_value_to_db_str(raw_details)
    cd = data.get("countDetail") if "countDetail" in data else data.get("count_detail")
    sd = data.get("successDetail") if "successDetail" in data else data.get("success_detail")
    count_detail: Optional[int]
    success_detail: Optional[int]
    if cd is None:
        count_detail = None
    elif isinstance(cd, int):
        count_detail = cd
    else:
        try:
            count_detail = int(cd)
        except (TypeError, ValueError):
            count_detail = None
    if sd is None:
        success_detail = None
    elif isinstance(sd, int):
        success_detail = sd
    else:
        try:
            success_detail = int(sd)
        except (TypeError, ValueError):
            success_detail = None
    return is_check, json_details, count_detail, success_detail


def run_ff_check(
    code: str,
    app_mnemonic: str,
    *,
    structurizr_credentials: Optional[Tuple[str, str]] = None,
    data: Optional[dict[str, Any]] = None,
    doc_id: Optional[str] = None,
) -> Tuple[bool, str, Optional[bool]]:
    """
    Если в БД у фитнес-функции задан непустой method (URL) — POST { callId, productCode }, запись в outside_ff.
    method_synchronous: ответ обрабатывается сразу (как webhook) или ожидается POST /api/v1/ff/webhook.
    doc_id: при передаче в run добавляется query-параметр docId к URL method.
    Иначе — запуск скрипта .py как раньше.
    Возвращает (успех, сообщение, is_check из product_ff или None для асинхронной внешней проверки).
    """
    row = get_fitness_function_by_code(code.strip())
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
            )

    return run_script(
        code,
        app_mnemonic,
        structurizr_credentials=structurizr_credentials,
        data=data,
    )


def _invoke_external_post(
    url: str,
    ff_id: int,
    app_mnemonic: str,
    *,
    synchronous: bool,
    doc_id: Optional[str],
) -> Tuple[bool, str, Optional[bool]]:
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
        return True, msg, is_check

    except (urllib.error.URLError, OSError, ValueError) as e:
        delete_outside_ff_by_call_id(call_id)
        return False, f"Ошибка HTTP-запроса к внешнему URL: {e}", None
