"""Сервис — менеджер проверок (fitness functions). API: uvicorn main:app — см. GET /docs."""
import ast
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exception_handlers import (
    http_exception_handler as default_http_exception_handler,
    request_validation_exception_handler as default_request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from db import (
    init_schema,
    get_actual_results_by_product_code,
    get_all_fitness_functions,
    get_fitness_function_applicabilities,
    get_fitness_function_code_to_id,
    get_fitness_function_codes_excluded_from_run_all,
    json_details_value_to_db_str,
    process_ff_webhook,
    product_has_actual_ff_pass,
    add_fitness_function,
    get_fitness_function_by_code,
    fitness_function_status_from_row,
    get_outside_ff_call,
    set_fitness_function_status,
    save_product_ff_result,
)
from ff_runner import run_ff_check
from ff_status import FF_STATUS_TEST, normalize_ff_status, skips_applicability_check
from run_result import check_result_from_outside_ff_row
from config import settings
from structurizr_hmac import CredentialsFetchError, fetch_structurizr_credentials
from script_runner import (
    ensure_scripts_dir,
    list_scripts,
    get_scripts_dir,
    materialize_missing_fitness_functions_from_scripts,
    materialize_missing_scripts_from_db,
)

app = FastAPI(title="FF Manager", description="Менеджер проверок архитектуры")

_PATHS_WITHOUT_CUSTOM_4XX = frozenset({"/health", "/api/v1", "/api/v1/"})


def _read_tech_version() -> str:
    """Читает техническую версию сборки из файла TECH_VERSION."""
    path = os.path.join(os.path.dirname(__file__), "TECH_VERSION")
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
    except OSError:
        return "unknown"
    return value or "unknown"


def _http_exception_detail_to_ru(detail: Any) -> str:
    if isinstance(detail, list):
        parts = [_http_exception_detail_to_ru(x) for x in detail]
        return "; ".join(p for p in parts if p)
    text = str(detail).strip()
    if text == "Not Found":
        return "Ресурс не найден."
    if text == "Method Not Allowed":
        return "Метод не разрешён для данного ресурса."
    return text


def _request_validation_error_message_ru(exc: RequestValidationError) -> str:
    fragments: list[str] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        skip = {"body", "query", "path", "header", "cookie"}
        name_bits = [str(x) for x in loc if x not in skip]
        field = ".".join(name_bits) if name_bits else "запрос"
        et = err.get("type", "")
        ctx = err.get("ctx") or {}
        if et == "missing":
            fragments.append(f"Отсутствует обязательное поле «{field}».")
        elif et == "string_too_short":
            min_len = ctx.get("min_length", "?")
            fragments.append(f"Поле «{field}» должно содержать не менее {min_len} символов.")
        elif et in ("string_type", "int_type", "bool_type", "float_type"):
            fragments.append(f"Поле «{field}» имеет неверный тип.")
        elif et == "bool_parsing":
            fragments.append(f"Поле «{field}» должно быть логическим значением (true/false).")
        elif et == "int_parsing":
            fragments.append(f"Поле «{field}» должно быть целым числом.")
        elif et == "float_parsing":
            fragments.append(f"Поле «{field}» должно быть числом.")
        elif et == "json_invalid":
            fragments.append("Тело запроса не является корректным JSON.")
        elif et in ("value_error",):
            fragments.append(f"Поле «{field}»: некорректное значение.")
        elif et.startswith("type_error"):
            fragments.append(f"Поле «{field}»: некорректный тип данных.")
        else:
            fragments.append(f"Поле «{field}»: не удалось проверить данные.")
    if not fragments:
        return "Ошибка проверки данных запроса."
    return " ".join(fragments)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code < 400 or exc.status_code >= 500:
        return await default_http_exception_handler(request, exc)
    if exc.status_code != 405 and request.url.path in _PATHS_WITHOUT_CUSTOM_4XX:
        return await default_http_exception_handler(request, exc)
    msg = _http_exception_detail_to_ru(exc.detail)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"errorMessage": msg},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def _request_validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path in _PATHS_WITHOUT_CUSTOM_4XX:
        return await default_request_validation_exception_handler(request, exc)
    msg = _request_validation_error_message_ru(exc)
    return JSONResponse(status_code=422, content={"errorMessage": msg})


class ApiClientErrorBody(BaseModel):
    """Ответ при ошибке клиента (4xx) на маршрутах /api/v1/*, кроме корня GET /api/v1/."""

    errorMessage: str = Field(..., description="Текст ошибки на русском языке")


def _openapi_client_errors(*status_codes: int) -> dict[int, dict[str, Any]]:
    descriptions: dict[int, str] = {
        400: "Некорректный запрос: пустые поля, нарушение applicability, ошибка запуска проверки и т.п.",
        404: "Ресурс не найден (например, неизвестный callId или код проверки).",
        405: "Для этого URL указан неверный HTTP-метод.",
        409: "Конфликт с текущим состоянием (например, проверка с таким кодом уже существует).",
        422: "Ошибка валидации тела запроса или параметров (формат JSON, обязательные поля и т.д.).",
    }
    return {
        code: {
            "model": ApiClientErrorBody,
            "description": descriptions.get(code, "Ошибка клиента."),
        }
        for code in status_codes
    }


class FitnessFunctionItem(BaseModel):
    """Элемент списка проверок (строка fitness_function)."""

    id: int
    code: Optional[str] = None
    description: Optional[str] = None
    applicability: Optional[str] = None
    auxiliary_check: bool = Field(..., description="Вспомогательная проверка")
    status: str = Field(..., description="Статус: TEST, TRIAL, ADOPT")
    script: Optional[str] = Field(None, description="Текст скрипта (.py) в БД")
    method: Optional[str] = Field(None, description="URL внешнего POST для внешней проверки")
    method_synchronous: bool = Field(
        False,
        description="Если true и задан method — ответ HTTP обрабатывается сразу; иначе ожидается webhook",
    )


class FitnessFunctionUpsertResponse(BaseModel):
    """Ответ POST/PUT /api/v1/fitness-function."""

    id: int
    code: str
    description: str
    applicability: Optional[str] = None
    auxiliary_check: bool
    status: str = Field(..., description="Статус: TEST, TRIAL, ADOPT")
    script_stored: bool
    script_attached: bool
    method: Optional[str] = None
    method_synchronous: bool = False


class ActualResultRow(BaseModel):
    """Одна актуальная запись результата проверки (product_ff + данные проверки)."""

    id: int = Field(..., description="Идентификатор строки product_ff")
    product_code: str
    ff_id: int
    ff_code: str
    ff_description: str
    status: str = Field(..., description="Статус проверки: TEST, TRIAL, ADOPT")
    is_check: bool
    create_date: datetime
    details: Optional[Any] = Field(
        default=None,
        description="Распарсенный json_details",
        json_schema_extra={
            "anyOf": [
                {"type": "null"},
                {"type": "array", "items": {"type": "object"}},
                {"type": "object"},
            ],
        },
    )
    countDetail: Optional[int] = None
    successDetail: Optional[int] = None


class ProductActualResultsBody(BaseModel):
    """Тело ответа со списком актуальных результатов проверок по продукту."""

    product_code: str
    results: list[ActualResultRow]


@app.on_event("startup")
def startup():
    ensure_scripts_dir(reset=True)
    init_schema()
    materialize_missing_fitness_functions_from_scripts()
    materialize_missing_scripts_from_db()


@app.get("/api/v1/")
def root():
    """Минимальная служебная точка; интерактив — в Swagger UI."""
    return {
        "service": "FF Manager",
        "tech_version": _read_tech_version(),
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }


@app.get(
    "/api/v1/scripts",
    response_model=list[str],
    responses=_openapi_client_errors(405),
)
def get_scripts():
    """Просмотр перечня всех скриптов проверок (коды)."""
    return list_scripts()


@app.get(
    "/api/v1/fitness-functions",
    response_model=list[FitnessFunctionItem],
    responses=_openapi_client_errors(405),
)
def api_get_fitness_functions():
    """Список всех fitness_function."""
    return get_all_fitness_functions()


class RunRequest(BaseModel):
    app: str


class RunCheckResult(BaseModel):
    """Результат одного запуска проверки в ответе POST /api/v1/run/{code}."""

    is_check: bool
    details: Optional[Any] = Field(
        default=None,
        description="Детали расчёта (для тестовых проверок — всегда при успешном execute/синхронном HTTP)",
    )
    countDetail: Optional[int] = None
    successDetail: Optional[int] = None


def _fetch_document_data(document_id: str) -> dict[str, Any]:
    base = (settings.documents_api_base_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=500,
            detail="Не задан FF_DOCUMENTS_API_BASE_URL для загрузки docId",
        )

    safe_id = urllib.parse.quote(str(document_id).strip(), safe="")
    url = f"{base}/api/v1/documents/{safe_id}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            raw = resp.read()
            payload = json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise HTTPException(status_code=e.code, detail=detail) from e
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к Documents API: {e}") from e
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Некорректный JSON от Documents API: {e}") from e

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Documents API вернул не JSON-объект")
    return payload


class ProductFfResultBody(BaseModel):
    """Тело POST /api/v1/product/{code}/ff — результат проверки для записи в product_ff (code — внешний код продукта)."""

    ff_code: str = Field(..., min_length=1)
    is_check: bool
    json_details: Optional[Union[str, list, dict]] = None
    count_detail: Optional[int] = None
    success_detail: Optional[int] = None


class FfWebhookBody(BaseModel):
    """Тело POST /api/v1/ff/webhook — колбэк внешней проверки (после вызова URL из fitness_function.method)."""

    model_config = ConfigDict(populate_by_name=True)

    call_id: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("callId", "call_id"),
    )
    is_check: bool = Field(
        ...,
        validation_alias=AliasChoices("isCheck", "is_check"),
    )
    details: Optional[Union[str, list, dict]] = Field(
        default=None,
        validation_alias=AliasChoices("details", "json_details"),
    )
    count_detail: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("countDetail", "count_detail"),
    )
    success_detail: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("successDetail", "success_detail"),
    )


def _json_details_for_db(value: Any) -> Optional[str]:
    return json_details_value_to_db_str(value)


def _normalize_script_text_input(raw_script: str) -> str:
    """
    Нормализует текст скрипта из multipart-поля `script`.
    Если клиент передал JSON-строку (с обрамляющими кавычками и \\n),
    пытаемся декодировать её в обычный многострочный Python-код.
    """
    text = raw_script
    candidate = raw_script.strip()
    if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
        try:
            decoded = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, str):
            text = decoded
    return text


def _validate_python_script_text(script_text: str, *, field_name: str) -> None:
    """Базовая проверка синтаксиса Python-кода перед сохранением скрипта."""
    try:
        tree = ast.parse(script_text, filename=f"<{field_name}>", mode="exec")
    except SyntaxError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Скрипт в поле '{field_name}' содержит синтаксическую ошибку: {e.msg}",
        ) from e

    has_execute = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute"
        for node in tree.body
    )
    if not has_execute:
        raise HTTPException(
            status_code=422,
            detail=f"Скрипт в поле '{field_name}' должен содержать функцию execute(...)",
        )


def _form_bool_optional(value: Optional[str]) -> bool:
    """Распознаёт true из multipart/form (чекбокс: true/on/1/yes)."""
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on")


async def _perform_fitness_function_upsert(
    code: str,
    description: str,
    applicability: Optional[str],
    aux: bool,
    ff_status: str,
    method: Optional[str],
    set_method: bool,
    method_synchronous: bool,
    set_method_synchronous: bool,
    script_file: Optional[StarletteUploadFile],
    script: Optional[str],
    *,
    create_only: bool,
) -> Optional[dict]:
    """Создание или обновление проверки: INSERT/UPDATE в БД, затем запись .py на диск."""
    set_script = False
    script_text: Optional[str] = None
    binary_payload: Optional[bytes] = None
    attached = False
    scripts_dir = get_scripts_dir()
    os.makedirs(scripts_dir, exist_ok=True)
    script_path = scripts_dir / f"{code}.py"

    if script_file is not None:
        content_bytes = await script_file.read()
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = None

        if content is not None:
            _validate_python_script_text(content, field_name="script_file")
            script_text = content
            set_script = True
        else:
            binary_payload = content_bytes

        attached = True
    elif script is not None and script.strip() != "":
        normalized_script = _normalize_script_text_input(script)
        _validate_python_script_text(normalized_script, field_name="script")
        script_text = normalized_script
        set_script = True
        attached = True
    else:
        attached = code in list_scripts()

    ff_id = add_fitness_function(
        code,
        description,
        applicability,
        aux,
        ff_status,
        script=script_text,
        set_script=set_script,
        method=method,
        set_method=set_method,
        method_synchronous=method_synchronous,
        set_method_synchronous=set_method_synchronous,
        create_only=create_only,
    )

    if ff_id is None:
        return None

    if script_file is not None:
        if binary_payload is not None:
            with open(script_path, "wb") as f:
                f.write(binary_payload)
        else:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_text or "")
    elif set_script and script_text is not None:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_text)

    row = get_fitness_function_by_code(code)
    method_stored = None
    method_sync_stored = False
    if row is not None:
        m = (row.get("method") or "").strip()
        method_stored = m if m else None
        method_sync_stored = bool(row.get("method_synchronous"))

    return {
        "id": ff_id,
        "code": code,
        "description": description,
        "applicability": applicability,
        "auxiliary_check": aux,
        "status": fitness_function_status_from_row(row) if row else normalize_ff_status(ff_status),
        "script_stored": set_script,
        "script_attached": attached,
        "method": method_stored,
        "method_synchronous": method_sync_stored,
    }


@app.post(
    "/api/v1/fitness-function",
    summary="Создать проверку (multipart)",
    response_model=FitnessFunctionUpsertResponse,
    responses=_openapi_client_errors(405, 409, 422),
)
async def create_fitness_function(
    code: str = Form(..., description="Код проверки"),
    description: str = Form(..., description="Описание"),
    applicability: Optional[str] = Form(None),
    auxiliary_check: Optional[str] = Form(None),
    script: Optional[str] = Form(None),
    script_file: UploadFile = File(None),
    method: Optional[str] = Form(
        None,
        description="URL внешнего POST (пустой или не передавать — без внешнего вызова)",
    ),
    method_synchronous: Optional[str] = Form(
        None,
        description="Если method задан: true — синхронный HTTP (ответ как webhook); иначе асинхронный колбэк",
    ),
):
    """
    **multipart/form-data** — только создание новой проверки. Если код уже есть — **409 Conflict**.
    Новая проверка всегда создаётся со статусом **TEST**.
    Смена статуса — **POST /api/v1/fitness-function/{code}/status** (TRIAL, ADOPT).
    Поля `script` и/или файл `script_file`; колонка **method** задаётся из поля `method` (пусто → NULL).
    """
    aux = _form_bool_optional(auxiliary_check)
    msync = _form_bool_optional(method_synchronous)
    method_for_db = None
    if method is not None:
        s = str(method).strip()
        method_for_db = s if s else None

    applicability_s = str(applicability).strip() if applicability is not None else None
    if applicability_s == "":
        applicability_s = None

    c = code.strip()
    result = await _perform_fitness_function_upsert(
        code=c,
        description=description,
        applicability=applicability_s,
        aux=aux,
        ff_status=FF_STATUS_TEST,
        method=method_for_db,
        set_method=True,
        method_synchronous=msync,
        set_method_synchronous=True,
        script_file=script_file,
        script=script,
        create_only=True,
    )
    if result is None:
        raise HTTPException(
            status_code=409,
            detail=f"Проверка с кодом '{c}' уже существует",
        )
    return result


@app.put(
    "/api/v1/fitness-function/{code}",
    summary="Обновить проверку (multipart)",
    response_model=FitnessFunctionUpsertResponse,
    responses=_openapi_client_errors(404, 405, 422),
)
async def update_fitness_function(
    code: str,
    description: str = Form(..., description="Описание"),
    applicability: Optional[str] = Form(None),
    auxiliary_check: Optional[str] = Form(None),
    script: Optional[str] = Form(None),
    script_file: UploadFile = File(None),
    method: Optional[str] = Form(
        None,
        description="URL внешнего POST (пустой или не передавать — без внешнего вызова)",
    ),
    method_synchronous: Optional[str] = Form(
        None,
        description="Если method задан: true — синхронный HTTP (ответ как webhook); иначе асинхронный колбэк",
    ),
):
    """
    **multipart/form-data** — обновление существующей проверки. Если код не найден — **404**.
    После обновления статус снова **TEST**. Смена на TRIAL/ADOPT — **POST .../status**.
    Поле **method_synchronous** меняется только если передано в форме (иначе сохраняется прежнее значение).
    """
    if not get_fitness_function_by_code(code.strip()):
        raise HTTPException(
            status_code=404,
            detail=f"Проверка с кодом '{code.strip()}' не найдена",
        )

    aux = _form_bool_optional(auxiliary_check)
    set_msync = method_synchronous is not None
    msync = _form_bool_optional(method_synchronous) if set_msync else False
    method_for_db = None
    set_method = method is not None
    if set_method:
        s = str(method).strip()
        method_for_db = s if s else None

    applicability_s = str(applicability).strip() if applicability is not None else None
    if applicability_s == "":
        applicability_s = None

    c = code.strip()

    result = await _perform_fitness_function_upsert(
        code=c,
        description=description,
        applicability=applicability_s,
        aux=aux,
        ff_status=FF_STATUS_TEST,
        method=method_for_db,
        set_method=set_method,
        method_synchronous=msync,
        set_method_synchronous=set_msync,
        script_file=script_file,
        script=script,
        create_only=False,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Проверка с кодом '{c}' не найдена",
        )
    return result


def _fitness_function_response_from_row(row: dict) -> dict:
    method_stored = None
    m = (row.get("method") or "").strip()
    if m:
        method_stored = m
    return {
        "id": row["id"],
        "code": row.get("code"),
        "description": row.get("description"),
        "applicability": row.get("applicability"),
        "auxiliary_check": bool(row.get("auxiliary_check")),
        "status": fitness_function_status_from_row(row),
        "method": method_stored,
        "method_synchronous": bool(row.get("method_synchronous")),
    }


@app.post(
    "/api/v1/fitness-function/{code}/status",
    summary="Установить статус проверки",
    responses=_openapi_client_errors(404, 405, 422),
)
def change_fitness_function_status(
    code: str,
    status: str = Form(
        ...,
        description="Статус: TEST, TRIAL или ADOPT",
    ),
):
    """
  Устанавливает статус жизненного цикла проверки:

    - **TEST** — отладка: без **product_ff**, без **applicability**, не в **run-all** и **actual-results**;
    - **TRIAL** и **ADOPT** — маркеры этапа жизненного цикла, **поведение одинаковое** (боевой режим).

    При переходе с/на **TEST** удаляются **product_ff** и **outside_ff** для этой проверки.
    Создание и **PUT** всегда выставляют **TEST**; этот метод задаёт **TRIAL** или **ADOPT**.
    """
    c = code.strip()
    if not get_fitness_function_by_code(c):
        raise HTTPException(status_code=404, detail=f"Проверка с кодом '{c}' не найдена")
    try:
        status_norm = normalize_ff_status(status)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    row = set_fitness_function_status(c, status_norm)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Проверка с кодом '{c}' не найдена")
    return _fitness_function_response_from_row(row)


def _applicability_empty(applicability) -> bool:
    """True, если applicability не задана (NULL в БД или пустая строка)."""
    return applicability is None or (isinstance(applicability, str) and not applicability.strip())


def _prerequisites_satisfied(
    product_code: str,
    applicability: Optional[str],
    code_to_id: dict[str, int],
) -> bool:
    """
    Проверка по product_ff: для каждого кода в applicability (через запятую)
    должна быть актуальная запись с is_check = true.
    Пустая applicability — всегда True.
    """
    if _applicability_empty(applicability):
        return True
    key = product_code.strip()
    for raw in applicability.split(","):
        prereq_code = raw.strip()
        if not prereq_code:
            continue
        ff_id = code_to_id.get(prereq_code)
        if ff_id is None:
            return False
        if not product_has_actual_ff_pass(key, ff_id):
            return False
    return True


def _runnable_ff_codes_ordered() -> list[str]:
    """Коды проверок для run-all: внешние (method в БД), затем скрипты .py (в т.ч. только на диске); без test."""
    rows = get_all_fitness_functions()
    disk = list_scripts()
    disk_set = set(disk)
    excluded_run_all = get_fitness_function_codes_excluded_from_run_all()
    chosen: list[str] = []
    seen: set[str] = set()
    for r in sorted(rows, key=lambda x: x["id"]):
        c = r.get("code")
        if not c or c in excluded_run_all:
            continue
        m = (r.get("method") or "").strip()
        if m:
            chosen.append(c)
            seen.add(c)
        elif c in disk_set:
            chosen.append(c)
            seen.add(c)
    for c in disk:
        if c in excluded_run_all or c in seen:
            continue
        chosen.append(c)
        seen.add(c)
    return chosen


@app.post(
    "/api/v1/run/{code}",
    responses=_openapi_client_errors(400, 405, 422),
)
def run_one(
    code: str,
    body: RunRequest,
    docId: Optional[str] = Query(
        default=None,
        description="Идентификатор документа для загрузки в data перед запуском проверки",
    ),
):
    """
    Запуск проверки для продукта: скрипт .py или POST на URL из fitness_function.method.
    В теле запроса передаётся мнемоника приложения (поле app).
    Для статуса **TEST** applicability не проверяется; результат не сохраняется в product_ff.
    Для **TRIAL** и **ADOPT** проверяется applicability.
    детали расчёта возвращаются в **check_result** (is_check, details, countDetail, successDetail).
    Для тестовой асинхронной HTTP-проверки в check_result — `{ "pending": true, "callId": "..." }`;
    после webhook опросите **GET /api/v1/ff/call/{callId}**. Боевая асинхронная проверка — check_result null до webhook (результат в product_ff).
    """
    app_code = body.app.strip()
    if not app_code:
        raise HTTPException(status_code=400, detail="Поле app не может быть пустым")

    ff_row = get_fitness_function_by_code(code)
    if ff_row and not skips_applicability_check(fitness_function_status_from_row(ff_row)):
        ff_app_map = get_fitness_function_applicabilities()
        code_to_id = get_fitness_function_code_to_id()
        applicability = ff_app_map.get(code)
        if not _prerequisites_satisfied(app_code, applicability, code_to_id):
            raise HTTPException(
                status_code=400,
                detail=f"Правило проверки '{code}' неприменимо для продукта '{body.app}' "
                f"(не выполнены условия applicability в product_ff)",
            )

    try:
        sz_creds = fetch_structurizr_credentials(app_code)
    except CredentialsFetchError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    doc_id_param: Optional[str] = None
    data: dict[str, Any] = {}
    if docId is not None:
        raw_doc_id = str(docId).strip()
        if not raw_doc_id:
            raise HTTPException(status_code=400, detail="Параметр docId не может быть пустым")
        doc_id_param = raw_doc_id
        data = _fetch_document_data(raw_doc_id)

    success, message, check_result = run_ff_check(
        code,
        body.app,
        structurizr_credentials=sz_creds,
        data=data,
        doc_id=doc_id_param,
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)
    out: dict[str, Any] = {
        "code": code,
        "app": body.app,
        "success": True,
        "message": message,
        "check_result": check_result,
    }
    if ff_row:
        out["status"] = fitness_function_status_from_row(ff_row)
        if (ff_row.get("method") or "").strip():
            out["method_synchronous"] = bool(ff_row.get("method_synchronous"))
    return out


@app.post(
    "/api/v1/run-all",
    responses=_openapi_client_errors(400, 405, 422),
)
def run_all(
    body: RunRequest,
    docId: Optional[str] = Query(
        default=None,
        description="Идентификатор документа для загрузки в data перед запуском проверок",
    ),
):
    """
    Запуск проверок для приложения: скрипты .py и внешние POST (fitness_function.method).
    Проверки с fitness_function.test = true не запускаются.
    Сначала — с applicability = NULL, затем остальные по мере выполнения предусловий в product_ff.
    """
    app_code = body.app.strip()
    if not app_code:
        raise HTTPException(status_code=400, detail="Поле app не может быть пустым")

    ff_app_map = get_fitness_function_applicabilities()
    code_to_id = get_fitness_function_code_to_id()
    excluded_run_all = get_fitness_function_codes_excluded_from_run_all()
    all_codes = _runnable_ff_codes_ordered()

    if not all_codes:
        return {
            "app": body.app,
            "results": {},
            "skipped": [],
            "message": "Нет доступных проверок: ни .py в каталоге scripts, ни внешнего URL (method) в БД (с учётом test)",
        }

    try:
        sz_creds = fetch_structurizr_credentials(app_code)
    except CredentialsFetchError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    doc_id_param: Optional[str] = None
    data: dict[str, Any] = {}
    if docId is not None:
        raw_doc_id = str(docId).strip()
        if not raw_doc_id:
            raise HTTPException(status_code=400, detail="Параметр docId не может быть пустым")
        doc_id_param = raw_doc_id
        data = _fetch_document_data(raw_doc_id)

    results: dict = {}
    ran: set[str] = set()
    ff_status_by_code = {
        (r.get("code") or ""): fitness_function_status_from_row(r)
        for r in get_all_fitness_functions()
        if r.get("code")
    }

    # 1) Сначала все с applicability NULL / пусто
    batch_null = [c for c in all_codes if _applicability_empty(ff_app_map.get(c))]
    for code in batch_null:
        ok, msg, check_result = run_ff_check(
            code,
            body.app,
            structurizr_credentials=sz_creds,
            data=data,
            doc_id=doc_id_param,
        )
        results[code] = {
            "success": ok,
            "message": msg,
            "check_result": check_result,
            "status": ff_status_by_code.get(code, FF_STATUS_TEST),
        }
        ran.add(code)

    # 2) Затем циклически — с непустой applicability, когда предусловия выполнены
    while True:
        batch = [
            c
            for c in all_codes
            if c not in ran
            and not _applicability_empty(ff_app_map.get(c))
            and _prerequisites_satisfied(app_code, ff_app_map.get(c), code_to_id)
        ]
        if not batch:
            break
        for code in batch:
            ok, msg, check_result = run_ff_check(
                code,
                body.app,
                structurizr_credentials=sz_creds,
                data=data,
                doc_id=doc_id_param,
            )
            results[code] = {
                "success": ok,
                "message": msg,
                "check_result": check_result,
                "status": ff_status_by_code.get(code, FF_STATUS_TEST),
            }
            ran.add(code)

    skipped = [
        {"code": c, "reason": "условия applicability не выполнены (нет актуальных product_ff с is_check=true)"}
        for c in all_codes
        if c not in ran
    ]
    skipped.extend(
        {"code": c, "reason": "проверка со статусом TEST не входит в run-all"}
        for c in sorted(excluded_run_all)
    )

    if not results and skipped:
        return {
            "app": body.app,
            "results": {},
            "skipped": skipped,
            "message": "Ни одна проверка не запущена: не выполнены предусловия applicability или все со статусом TEST",
        }

    return {
        "app": body.app,
        "results": results,
        "skipped": skipped,
    }


@app.get(
    "/api/v1/ff/call/{call_id}",
    responses=_openapi_client_errors(404, 405),
)
def get_ff_call_result(call_id: str):
    """
    Статус асинхронного внешнего вызова по **callId** (из ответа POST /api/v1/run/{code} или исходящего POST на method).

    - **pending** — webhook ещё не пришёл;
    - **call_status** — `pending` / `done` (состояние вызова outside_ff);
    - **status** — статус проверки TEST / TRIAL / ADOPT;
    - при **done** и TEST — **check_result** с деталями (без product_ff).
    """
    row = get_outside_ff_call(call_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Вызов с callId не найден (outside_ff)",
        )
    base = {
        "callId": row["call_id"],
        "ff_code": row.get("ff_code"),
        "product_code": row.get("product_code"),
        "status": row.get("ff_status") or FF_STATUS_TEST,
    }
    call_state = row.get("status")
    if call_state != "done":
        return {**base, "call_status": "pending", "check_result": None}
    check_result = check_result_from_outside_ff_row(row)
    if check_result is not None:
        return {**base, "call_status": "done", "check_result": check_result}
    return {
        **base,
        "call_status": "done",
        "check_result": None,
        "message": "Результат записан в product_ff (боевой режим). См. GET /api/v1/product/{code}/actual-results",
    }


@app.post(
    "/api/v1/ff/webhook",
    responses=_openapi_client_errors(404, 405, 422),
)
def ff_webhook(body: FfWebhookBody):
    """
    Колбэк внешней проверки: по callId находит запись outside_ff, пишет результат в product_ff (как POST /api/v1/product/.../ff).
    Для проверок с test=true в product_ff не пишется; результат хранится в outside_ff (опрос GET /api/v1/ff/call/{callId}).
    Повторный вызов с тем же callId при status=done даёт 200 без изменений.
    """
    json_details = _json_details_for_db(body.details)
    outcome = process_ff_webhook(
        body.call_id,
        body.is_check,
        json_details=json_details,
        count_detail=body.count_detail,
        success_detail=body.success_detail,
    )
    if outcome == "not_found":
        raise HTTPException(
            status_code=404,
            detail=f"Вызов с callId не найден (outside_ff)",
        )
    call_row = get_outside_ff_call(body.call_id)
    ff_st = fitness_function_status_from_row(
        {"status": call_row.get("ff_status")} if call_row else None
    )
    if outcome == "already_done":
        return {"result": "already_processed", "status": ff_st}
    if outcome == "test_ok":
        check_result = check_result_from_outside_ff_row(call_row) if call_row else None
        return {
            "result": "ok",
            "saved_to_product_ff": False,
            "status": ff_st,
            "check_result": check_result,
        }
    return {"result": "ok", "saved_to_product_ff": True, "status": ff_st}


@app.post(
    "/api/v1/product/{code}/ff",
    responses=_openapi_client_errors(404, 405, 422),
)
def post_product_ff_result(code: str, body: ProductFfResultBody):
    """
    Запись результата проверки в product_ff для продукта code (мнемоника из каталога продуктов) и проверки body.ff_code.
    Скрипты проверок вызывают этот метод вместо прямого доступа к БД.
    """
    json_details = _json_details_for_db(body.json_details)
    status, row_id = save_product_ff_result(
        code,
        body.ff_code,
        body.is_check,
        json_details=json_details,
        count_detail=body.count_detail,
        success_detail=body.success_detail,
    )
    if status == "fitness_function_not_found":
        raise HTTPException(
            status_code=404,
            detail=f"Проверка с кодом '{body.ff_code}' не найдена в fitness_function",
        )
    ff_row = get_fitness_function_by_code(body.ff_code)
    ff_st = fitness_function_status_from_row(ff_row)
    if status == "test_mode":
        return {
            "saved": False,
            "reason": "test",
            "product_code": code.strip(),
            "ff_code": body.ff_code.strip(),
            "status": ff_st,
        }
    return {
        "id": row_id,
        "product_code": code.strip(),
        "ff_code": body.ff_code.strip(),
        "saved": True,
        "status": ff_st,
    }


@app.get(
    "/api/v1/product/{code}/actual-results",
    response_model=ProductActualResultsBody,
    responses=_openapi_client_errors(405),
)
def get_product_actual_results(code: str):
    """
    По коду продукта возвращает актуальные результаты основных проверок (is_actual == true),
    без вспомогательных (auxiliary_check) и без тестовых (test). Пустой список, если записей ещё нет.
    Код продукта в пути сравнивается без учёта регистра.
    В ответе:
    - details — json_details из product_ff,
    - countDetail — count_detail,
    - successDetail — success_detail.
    """
    raw_results = get_actual_results_by_product_code(code)

    results = []
    for r in raw_results:
        raw_details = r.get("json_details")
        if raw_details is None:
            details = None
        else:
            try:
                details = json.loads(raw_details)
            except (TypeError, json.JSONDecodeError):
                details = None
        results.append(
            {
                "id": r["id"],
                "product_code": r["product_code"],
                "ff_id": r["ff_id"],
                "ff_code": r["ff_code"],
                "ff_description": r["ff_description"],
                "status": r.get("ff_status") or FF_STATUS_ADOPT,
                "is_check": r["is_check"],
                "create_date": r["create_date"],
                "details": details,
                "countDetail": r.get("count_detail"),
                "successDetail": r.get("success_detail"),
            }
        )

    return ProductActualResultsBody(product_code=code, results=results)


@app.get("/health")
def health():
    return {"status": "ok"}
