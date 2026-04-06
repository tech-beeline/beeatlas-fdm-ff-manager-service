# Как писать скрипты проверок (fitness functions)

Скрипт — это обычный Python-файл в каталоге `scripts/`, который по коду продукта выполняет проверку и **отправляет результат в API** FF Manager. Прямого подключения к PostgreSQL в скриптах быть не должно.

## Имя файла и код проверки

- Файл: `scripts/<КОД>.py`, например `scripts/MYCHK-1.py`.
- **Код проверки** = имя файла **без** `.py` (`MYCHK-1`).
- Этот же код должен быть зарегистрирован в таблице `fitness_function` (через UI или `POST /fitness-function`). Иначе API при записи результата вернёт 404.
- При регистрации текст скрипта (UTF-8) сохраняется в поле **`fitness_function.script`** — из загруженного файла или из поля формы **`script`**.

Игнорируются при автообнаружении:

- `__init__.py`
- файлы, чьё имя начинается с `_` (например `_common.py`)

## Аргументы командной строки

Менеджер запускает процесс так:

```bash
python /path/to/scripts/<КОД>.py <alias_продукта>
```

- **Первый аргумент (`sys.argv[1]`)** — код продукта (**alias** из таблицы `product`), тот же, что пользователь указывает в UI при запуске проверки.

Пример чтения:

```python
import sys

app_code = sys.argv[1] if len(sys.argv) > 1 else ""
```

## Запись результата: `run_check` из `_common`

Импортируйте общую функцию (добавьте каталог скриптов в `sys.path`, как в примерах `DEMOFF-*.py`):

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import run_check

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]
```

`run_check` отправляет **POST** на `{FF_API_BASE_URL}/product/{alias}/ff` и создаёт актуальную строку в `product_ff` (предыдущие актуальные результаты этой пары продукт + проверка помечаются неактуальными на стороне сервера).

### Сигнатура

```python
run_check(
    app_code: str,           # код продукта (тот же, что argv[1])
    script_code: str,       # обычно SCRIPT_CODE — совпадает с именем файла
    is_check: bool = True,  # итог проверки: True = условие выполнено
    success_detail: int | None = None,
    count_detail: int | None = None,
    json_details: str | None = None,  # JSON-строка (например список деталей)
) -> None
```

- При ошибке API или пустом `app_code` скрипт завершится с **кодом 1** (`sys.exit(1)`).
- Для «проверка не пройдена, но скрипт отработал штатно» передавайте `is_check=False` и завершайте процесс с **кодом 0** (не смешивайте с сетевыми/HTTP ошибками).

### Детали в `json_details`

В БД хранится текст. Удобно сериализовать список словарей:

```python
import json

details = [{"check": "true", "item": "a"}, {"check": "false", "item": "b"}]
run_check(
    app_code,
    SCRIPT_CODE,
    is_check=False,
    count_detail=len(details),
    success_detail=1,
    json_details=json.dumps(details, ensure_ascii=False),
)
```

Поля `count_detail` / `success_detail` используйте по смыслу вашей проверки (сколько элементов проверено / сколько успешно).

## Переменные окружения

При запуске через FF Manager в процесс передаётся как минимум:

| Переменная           | Назначение |
|----------------------|------------|
| `FF_API_BASE_URL`    | Базовый URL API (например `http://127.0.0.1:8000`). Именно сюда уходит запись результата. |
| `FF_DB_*`            | Параметры БД (для самого сервиса; скриптам для записи результата **не нужны**). |

При **ручном** запуске скрипта поднимите сервис FF Manager и при необходимости задайте:

```bash
export FF_API_BASE_URL=http://127.0.0.1:8000
python scripts/MYCHK-1.py DEMO
```

## Минимальный шаблон

```python
#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import run_check

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

if __name__ == "__main__":
    app_code = sys.argv[1] if len(sys.argv) > 1 else ""
    ok = bool(app_code)  # ваша логика
    run_check(
        app_code,
        SCRIPT_CODE,
        is_check=ok,
        success_detail=1 if ok else 0,
        count_detail=1,
        json_details=None,
    )
```

## Поведение в продукте (кратко)

- **`run-all`** запускает только скрипты из каталога, для которых в БД **не** выставлен флаг `test`, с учётом `applicability` и порядка (сначала без предусловий).
- Проверки с **`test = true`** и **`auxiliary_check = true`** не попадают в выдачу «основных» результатов продукта в API, но логика отличается: вспомогательные **участвуют** в `run-all`, тестовые — **нет**.

Уточняйте флаги при создании проверки через `POST /fitness-function` (`multipart/form-data`; повтор того же кода — 409).

## Рекомендации

1. Держите в скрипте только бизнес-логику проверки и один вызов `run_check` (или несколько при составной сценарии — обычно достаточно одного итогового).
2. Не добавляйте зависимость от `psycopg2` в скрипты — запись только через API.
3. Ограничивайте таймауты внешних HTTP-вызовов, обрабатывайте ошибки сети: при невозможности выполнить проверку можно вызвать `run_check(..., is_check=False, ...)` или завершить с ненулевым кодом, если считаете запуск невалидным.
4. Сверяйтесь с рабочими примерами: `scripts/DEMOFF-1.py` … `DEMOFF-4.py`.

## Связанные эндпоинты API

- **`POST /product/{code}/ff`** — тело: `ff_code`, `is_check`, опционально `json_details`, `count_detail`, `success_detail` (используется `run_check` внутри `_common.py`).
- **`POST /fitness-function`** — `multipart/form-data`: только создание новой проверки (при занятом коде — 409); метаданные и текст/файл скрипта.
