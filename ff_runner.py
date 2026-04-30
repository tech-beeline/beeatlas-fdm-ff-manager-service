"""Запуск проверки: скрипт на диске или внешний POST по fitness_function.method."""
import json
import urllib.error
import urllib.request
import uuid
from typing import Optional, Tuple

from config import settings
from db import (
    delete_outside_ff_by_call_id,
    get_fitness_function_by_code,
    insert_outside_ff_call,
)
from script_runner import run_script


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


def run_ff_check(
    code: str,
    app_mnemonic: str,
    *,
    structurizr_credentials: Optional[Tuple[str, str]] = None,
) -> Tuple[bool, str, Optional[bool]]:
    """
    Если в БД у фитнес-функции задан непустой method (URL) — POST { callId, productCode }, запись в outside_ff.
    Иначе — запуск скрипта .py как раньше (файл на диске; строка в БД не обязательна).
    Возвращает (успех, сообщение, is_check из product_ff или None для асинхронной внешней проверки).
    """
    row = get_fitness_function_by_code(code.strip())
    if row:
        method = (row.get("method") or "").strip()
        if method:
            return _invoke_external_post(method, row["id"], app_mnemonic)

    return run_script(code, app_mnemonic, structurizr_credentials=structurizr_credentials)


def _invoke_external_post(url: str, ff_id: int, app_mnemonic: str) -> Tuple[bool, str, Optional[bool]]:
    call_id = str(uuid.uuid4())
    product_code = app_mnemonic.strip()
    insert_outside_ff_call(ff_id=ff_id, product_code=product_code, call_id=call_id)
    try:
        status, text = _post_json(
            url,
            {"callId": call_id, "productCode": product_code},
            settings.external_ff_timeout_seconds,
        )
        if 200 <= status < 300:
            return (
                True,
                f"Внешняя проверка вызвана (callId={call_id}). Результат запишется через POST /api/v1/ff/webhook.",
                None,
            )
        delete_outside_ff_by_call_id(call_id)
        detail = text.strip()[:500]
        return False, f"Внешний сервис вернул HTTP {status}" + (f": {detail}" if detail else ""), None
    except (urllib.error.URLError, OSError, ValueError) as e:
        delete_outside_ff_by_call_id(call_id)
        return False, f"Ошибка HTTP-запроса к внешнему URL: {e}", None
