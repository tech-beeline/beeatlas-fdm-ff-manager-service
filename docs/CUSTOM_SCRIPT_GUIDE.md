# Инструкция: как написать собственный FF-скрипт (простой вариант)

Эта инструкция описывает минимальный и рекомендуемый способ написания скрипта проверки для FF Manager.

## 1. Обязательный контракт

В скрипте должна быть функция:

```python
def execute(app_code: str) -> None:
    ...
```

- `app_code` — код продукта (alias), для которого выполняется проверка.
- Именно `execute(...)` вызывается раннером.

## 2. Что делает `execute`

Внутри `execute` нужно:

1. Выполнить бизнес-проверку (получить данные, вычислить итог).
2. Сформировать список `details`.
3. Отправить результат проверки через `_common.run_check(...)`.

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

## 4. Минимальный шаблон скрипта

```python
#!/usr/bin/env python3
import json
import os
import sys

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import run_check


def execute(app_code: str) -> None:
    # 1) Ваша логика проверки
    details = [
        {"check": True, "item": "example-1"},
        {"check": True, "item": "example-2"},
    ]

    # 2) Итог
    count_detail = len(details)
    success_detail = sum(1 for d in details if d.get("check") is True)
    is_check = count_detail > 0 and success_detail == count_detail

    # 3) Отправка результата в FF Manager
    run_check(
        app_code,
        SCRIPT_CODE,
        is_check=is_check,
        success_detail=success_detail,
        count_detail=count_detail,
        json_details=json.dumps(details, ensure_ascii=False),
    )
```

## 5. Рекомендации

- Держите тяжёлую логику внутри `execute` и вспомогательных функций.
- Не пишите напрямую в БД из скрипта — используйте `run_check`.
- Для внешних HTTP-вызовов всегда задавайте таймауты и обрабатывайте ошибки.
