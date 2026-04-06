"""
Общая логика для скриптов проверок:
- отправка результата в API FF Manager (POST /api/v1/product/{alias}/ff), без прямого доступа к БД.

Переменные окружения:
- FF_API_BASE_URL — базовый URL сервиса (по умолчанию http://127.0.0.1:8000).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


def run_check(
    app_code: str,
    script_code: str,
    is_check: bool = True,
    success_detail: Optional[int] = None,
    count_detail: Optional[int] = None,
    json_details: Optional[str] = None,
) -> None:
    """
    Отправляет результат проверки на API: создаётся актуальная запись product_ff.
    Параметр is_check — итог проверки.
    Параметры *_detail и json_details задаются конкретным скриптом проверки.
    """
    if not app_code:
        print("Не передан код приложения", file=sys.stderr)
        sys.exit(1)

    base = os.environ.get("FF_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    alias = app_code.strip()
    path = urllib.parse.quote(alias, safe="")
    url = f"{base}/api/v1/product/{path}/ff"

    payload = {
        "ff_code": script_code,
        "is_check": is_check,
        "json_details": json_details,
        "count_detail": count_detail,
        "success_detail": success_detail,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200:
                print(f"API вернул статус {resp.status}: {raw}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"Ошибка записи результата (HTTP {e.code}): {err_body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Не удалось обратиться к API ({url}): {e.reason}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Ошибка сети при обращении к API: {e}", file=sys.stderr)
        sys.exit(1)
