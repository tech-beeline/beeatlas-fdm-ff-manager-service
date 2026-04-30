"""
Общая логика для скриптов проверок:
- отправка результата в API FF Manager (POST /api/v1/product/{alias}/ff), без прямого доступа к БД;
- тип результата execute() и сохранение через persist_execute_result (вызывается раннером).

Переменные окружения:
- FF_API_BASE_URL — базовый URL сервиса (по умолчанию http://127.0.0.1:8000).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional, TypedDict, cast


class ExecuteResult(TypedDict):
    """Единый возврат функции execute() из скрипта проверки."""

    app_code: str
    script_code: str
    is_check: bool
    details: list[dict[str, Any]]


def detail_row_passes(row: Mapping[str, Any]) -> bool:
    """Элемент details считается успешным, если check истинен (bool True или строка \"true\" и т.п.)."""
    v = row.get("check")
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def coerce_execute_result(raw: object, app_mnemonic: str, script_file_code: str) -> ExecuteResult:
    """
    Проверяет и нормализует возврат execute(): dict или кортеж
    (app_code, script_code, is_check, details).
    """
    if isinstance(raw, tuple) and len(raw) == 4:
        app_code, script_code, is_check, details = raw
    elif isinstance(raw, dict):
        app_code = raw.get("app_code")
        script_code = raw.get("script_code")
        is_check = raw.get("is_check")
        details = raw.get("details")
    else:
        raise TypeError(
            "execute() должен вернуть dict с ключами app_code, script_code, is_check, details "
            f"или кортеж из 4 элементов; получено: {type(raw).__name__}"
        )

    if not isinstance(app_code, str) or not isinstance(script_code, str):
        raise TypeError("app_code и script_code должны быть строками")
    if not isinstance(is_check, bool):
        raise TypeError("is_check должен быть bool")
    if not isinstance(details, list):
        raise TypeError("details должен быть списком")
    for i, item in enumerate(details):
        if not isinstance(item, dict):
            raise TypeError(f"details[{i}] должен быть dict")

    if app_code.strip() != app_mnemonic.strip():
        print(
            f"Предупреждение: app_code из execute ({app_code!r}) не совпадает с аргументом ({app_mnemonic!r}); "
            "для API используется переданный код продукта.",
            file=sys.stderr,
        )
    if script_code.strip() != script_file_code.strip():
        print(
            f"Предупреждение: script_code из execute ({script_code!r}) не совпадает с кодом скрипта ({script_file_code!r}); "
            "для записи FF используется код файла.",
            file=sys.stderr,
        )

    return ExecuteResult(
        app_code=app_mnemonic.strip(),
        script_code=script_file_code.strip(),
        is_check=is_check,
        details=cast(list[dict[str, Any]], details),
    )


def persist_execute_result(
    *,
    product_code: str,
    ff_code: str,
    is_check: bool,
    details: list[dict[str, Any]],
) -> None:
    """
    Сохраняет результат проверки: count_detail = len(details), success_detail — число элементов с check=true.
    """
    count_detail = len(details)
    success_detail = sum(1 for row in details if detail_row_passes(row))
    json_details: Optional[str]
    if details:
        json_details = json.dumps(details, ensure_ascii=False)
    else:
        json_details = None
    run_check(
        product_code,
        ff_code,
        is_check=is_check,
        success_detail=success_detail,
        count_detail=count_detail,
        json_details=json_details,
    )


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


def run_check_from_details(
    app_code: str,
    script_code: str,
    details: list[dict[str, Any]],
) -> None:
    """
    Устаревший helper: итог проверки = список непустой (как раньше).
    Предпочтительно возвращать данные из execute() и позволить раннеру вызвать persist_execute_result.
    """
    persist_execute_result(
        product_code=app_code,
        ff_code=script_code,
        is_check=len(details) > 0,
        details=details,
    )
