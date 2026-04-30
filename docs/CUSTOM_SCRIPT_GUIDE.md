# Инструкция: как написать собственный FF-скрипт (актуальный вариант)

Эта инструкция описывает минимальный и рекомендуемый способ написания скрипта проверки для FF Manager.

## 1. Контракт `execute(...)`

Раннер поддерживает оба варианта:

```python
def execute(app_code: str) -> ExecuteResult:
    ...
```

или:

```python
def execute(app_code: str, data: Dict[str, Any]) -> ExecuteResult:
    ...
```

- `app_code` — код продукта (alias), для которого выполняется проверка.
- `data` — словарь с документом, загруженным перед запуском по query-параметру `docId`.
- Если `docId` не передан, `data` будет пустым словарём `{}`.
- Вместо аргумента функцию можно оставить с одним параметром: тогда `data` доступна как глобальная переменная модуля.

## 2. Что делает `execute`

Внутри `execute` нужно:

1. Выполнить бизнес-проверку (получить данные, вычислить итог).
2. Сформировать список `details`.
3. Вернуть результат в формате `ExecuteResult` (сохранение в API выполняет раннер).

## 3. Правило для `details`

`details` — это список объектов (словарей), например:

```python
details = [
    {"check": True, "name": "container-a"},
    {"check": False, "name": "container-b", "reason": "not found"},
]
```

Требование:

- каждый объект в `details` **должен содержать атрибут `check` типа `bool`**;
- остальные атрибуты свободные (любой состав полей под вашу проверку).

## 4. Минимальный шаблон скрипта (рекомендуемый)

```python
#!/usr/bin/env python3
import os
import sys
from typing import Any, Dict

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ExecuteResult


def execute(app_code: str, data: Dict[str, Any]) -> ExecuteResult:
    # 1) Ваша логика проверки
    # data приходит из POST /api/v1/run/{code}?docId=... или /api/v1/run-all?docId=...
    document_name = data.get("name")
    details = [
        {"check": bool(document_name), "documentName": document_name},
    ]

    # 2) Итог
    return ExecuteResult(
        app_code=app_code,
        script_code=SCRIPT_CODE,
        is_check=all(d.get("check") is True for d in details),
        details=details,
    )
```

## 5. Готовый HTTP-клиент с HMAC

Для запросов к внешним сервисам через Structurizr HMAC используйте helper из `_common`:

```python
from _common import structurizr_http_client

client = structurizr_http_client()
status, raw_bytes = client.get("/product/api/v1/product/DEMO/container")
```

- Ключи и базовый URL прокидываются раннером автоматически при запуске из API (`POST /api/v1/run/{code}` и `POST /api/v1/run-all`).
- В скрипте не нужно вручную собирать HMAC-подпись.

## 6. Рекомендации

- Держите тяжёлую логику внутри `execute` и вспомогательных функций.
- Не пишите напрямую в БД из скрипта — возвращайте `ExecuteResult`, запись сделает раннер.
- Для внешних HTTP-вызовов через HMAC-клиент обрабатывайте не-2xx статусы и ошибки сети.
