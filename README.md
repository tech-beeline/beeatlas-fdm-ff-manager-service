# FF Manager — менеджер проверок

Сервис оркестрирует скрипты проверок (fitness functions) для приложений. Скрипты можно добавлять и удалять в каталоге `scripts/` без перезапуска сервиса.

## Требования

- Python 3.10+
- PostgreSQL (схема `ff` и таблицы создаются при первом старте)

## Установка

```bash
pip install -r requirements.txt
```

Опционально создайте `.env` в корне проекта (или задайте переменные окружения с префиксом `FF_`):

```
FF_DB_HOST=localhost
FF_DB_PORT=5432
FF_DB_USER=myuser
FF_DB_PASSWORD=mysecretpassword
FF_DB_NAME=mydatabase
FF_SCRIPTS_DIR=scripts
```

## Запуск

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## API

- **GET /scripts** — перечень кодов всех загруженных скриптов проверок.
- **POST /run/{code}** — запуск одного скрипта для приложения. Тело: `{"app": "DEMO"}`.
- **POST /run-all** — запуск всех скриптов для приложения. Тело: `{"app": "DEMO"}`.
- **GET /health** — проверка доступности сервиса.

Каждый скрипт в каталоге `scripts/` именуется кодом проверки (имя файла без `.py`), например `DEMOFF-1.py` → код `DEMOFF-1`. Новые `.py` файлы подхватываются при следующем запросе без перезапуска.

## База данных

При первом старте создаётся схема `ff` и таблицы:

- **ff.product** — приложения (id, name, alias). Тест: (1, "DemoApp", "DEMO").
- **ff.fitness_function** — виды проверок (id, code, description). Тесты: DEMOFF-1, DEMOFF-2.
- **ff.product_ff** — связи приложение–проверка и результаты (product_id, ff_id, is_check, create_date).

Скрипты проверок получают креды БД через переменные окружения `FF_DB_*`, устанавливаемые сервисом при запуске.
