"""Подключение к PostgreSQL и инициализация схемы ff."""
import json
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

from config import settings
from ff_status import (
    FF_STATUS_ADOPT,
    FF_STATUS_TEST,
    FF_STATUS_TRIAL,
    excluded_from_run_all,
    normalize_ff_status,
    should_clear_ff_run_data,
    skips_product_ff_persistence,
)

SCHEMA = "ff"


def _row_ff_status(row: dict) -> str:
    """Статус из строки БД (колонка status)."""
    raw = row.get("status")
    if raw is not None and str(raw).strip():
        return normalize_ff_status(str(raw))
    return FF_STATUS_TEST


def fitness_function_status_from_row(row: Optional[dict]) -> str:
    """Публичная обёртка для нормализации status в ответах API."""
    if row is None:
        return FF_STATUS_TEST
    return _row_ff_status(row)


def get_connection():
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        dbname=settings.db_name,
    )


@contextmanager
def get_cursor():
    conn = get_connection()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_latest_check_result(product_code: str, ff_code: str) -> Optional[bool]:
    """
    Возвращает is_check последней актуальной записи product_ff для пары (код продукта, фитнес-функция по code).
    Если записи нет — None.
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT pf.is_check
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE pf.product_code = %s AND ff.code = %s AND pf.is_actual = true
            ORDER BY pf.create_date DESC
            LIMIT 1
            """,
            (product_code.strip(), ff_code),
        )
        row = cur.fetchone()
        return row["is_check"] if row is not None else None


def get_fitness_function_code_to_id() -> dict[str, int]:
    """Словарь code -> id для всех fitness_function."""
    with get_cursor() as cur:
        cur.execute(f"SELECT id, code FROM {SCHEMA}.fitness_function")
        rows = cur.fetchall()
    return {r["code"]: r["id"] for r in rows}


def get_fitness_function_status(*, ff_id: Optional[int] = None, ff_code: Optional[str] = None) -> Optional[str]:
    """Статус проверки (TEST / TRIAL / ADOPT) или None, если не найдена."""
    if ff_id is None and ff_code is None:
        return None
    with get_cursor() as cur:
        if ff_id is not None:
            cur.execute(
                f"SELECT status FROM {SCHEMA}.fitness_function WHERE id = %s",
                (ff_id,),
            )
        else:
            cur.execute(
                f"SELECT status FROM {SCHEMA}.fitness_function WHERE code = %s",
                (ff_code.strip(),),
            )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_ff_status(dict(row))


def save_product_ff_result(
    product_code: str,
    ff_code: str,
    is_check: bool,
    json_details: Optional[str] = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> tuple[str, Optional[int]]:
    """
    Добавляет актуальную запись product_ff для продукта (внешний код) и проверки (ff code).
    Предыдущие актуальные записи для этой пары помечаются is_actual = false.
    Возвращает ("ok", id), ("fitness_function_not_found", None) или ("test_mode", None) для status=TEST.
    """
    code_key = product_code.strip()
    code = ff_code.strip()
    with get_cursor() as cur:
        cur.execute(
            f"SELECT id, status FROM {SCHEMA}.fitness_function WHERE code = %s",
            (code,),
        )
        row = cur.fetchone()
        if not row:
            return ("fitness_function_not_found", None)
        if skips_product_ff_persistence(_row_ff_status(dict(row))):
            return ("test_mode", None)
        ff_id = row["id"]

        cur.execute(
            f"""
            UPDATE {SCHEMA}.product_ff
            SET is_actual = false
            WHERE LOWER(product_code) = LOWER(%s) AND ff_id = %s AND is_actual = true
            """,
            (code_key, ff_id),
        )
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.product_ff (
                product_code,
                ff_id,
                is_check,
                is_actual,
                json_details,
                count_detail,
                success_detail,
                create_date
            )
            VALUES (%s, %s, %s, true, %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id
            """,
            (code_key, ff_id, is_check, json_details, count_detail, success_detail),
        )
        new_row = cur.fetchone()
        return ("ok", new_row["id"])


def set_fitness_function_status(code: str, new_status: str) -> Optional[dict]:
    """
    Устанавливает статус TEST / TRIAL / ADOPT.
    При смене с/на TEST очищает product_ff и outside_ff для проверки.
    """
    key = code.strip()
    status_norm = normalize_ff_status(new_status)
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, status FROM {SCHEMA}.fitness_function
            WHERE code = %s
            """,
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        ff_id = row["id"]
        old_status = _row_ff_status(dict(row))
        if should_clear_ff_run_data(old_status, status_norm):
            cur.execute(
                f"DELETE FROM {SCHEMA}.product_ff WHERE ff_id = %s",
                (ff_id,),
            )
            cur.execute(
                f"DELETE FROM {SCHEMA}.outside_ff WHERE ff_id = %s",
                (ff_id,),
            )
        cur.execute(
            f"""
            UPDATE {SCHEMA}.fitness_function
            SET status = %s
            WHERE id = %s
            """,
            (status_norm, ff_id),
        )
    return get_fitness_function_by_code(key)


def get_fitness_function_by_code(code: str) -> Optional[dict]:
    """Одна строка fitness_function по code или None."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, applicability, auxiliary_check, status, script, method,
                   method_synchronous
            FROM {SCHEMA}.fitness_function
            WHERE code = %s
            """,
            (code.strip(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["status"] = _row_ff_status(d)
        return d


def insert_outside_ff_call(ff_id: int, product_code: str, call_id: str) -> int:
    """Статус начального вызова — 'call'. Возвращает id строки outside_ff."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.outside_ff (ff_id, product_code, call_id, status)
            VALUES (%s, %s, %s, 'call')
            RETURNING id
            """,
            (ff_id, product_code.strip(), call_id.strip()),
        )
        return cur.fetchone()["id"]


def delete_outside_ff_by_call_id(call_id: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            f"DELETE FROM {SCHEMA}.outside_ff WHERE call_id = %s",
            (call_id.strip(),),
        )


def json_details_value_to_db_str(value: Any) -> Optional[str]:
    """Нормализация поля details из JSON тела (webhook / синхронный HTTP) в строку для product_ff."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def process_ff_webhook(
    call_id: str,
    is_check: bool,
    json_details: Optional[str] = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> str:
    """
    Ищет outside_ff по call_id.
    Возвращает: not_found | already_done | ok
    """
    cid = call_id.strip()
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, ff_id, product_code, status
            FROM {SCHEMA}.outside_ff
            WHERE call_id = %s
            FOR UPDATE
            """,
            (cid,),
        )
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if row["status"] == "done":
            return "already_done"

        ff_id = row["ff_id"]
        pc = row["product_code"]

        cur.execute(
            f"SELECT status FROM {SCHEMA}.fitness_function WHERE id = %s",
            (ff_id,),
        )
        ff_row = cur.fetchone()
        ff_status = _row_ff_status(dict(ff_row)) if ff_row else FF_STATUS_ADOPT
        is_test_mode = skips_product_ff_persistence(ff_status)

        if not is_test_mode:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.product_ff
                SET is_actual = false
                WHERE product_code = %s AND ff_id = %s AND is_actual = true
                """,
                (pc, ff_id),
            )
            cur.execute(
                f"""
                INSERT INTO {SCHEMA}.product_ff (
                    product_code,
                    ff_id,
                    is_check,
                    is_actual,
                    json_details,
                    count_detail,
                    success_detail,
                    create_date
                )
                VALUES (%s, %s, %s, true, %s, %s, %s, CURRENT_TIMESTAMP)
                """,
                (pc, ff_id, is_check, json_details, count_detail, success_detail),
            )
        if is_test_mode:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.outside_ff
                SET status = 'done',
                    is_check = %s,
                    json_details = %s,
                    count_detail = %s,
                    success_detail = %s
                WHERE id = %s
                """,
                (is_check, json_details, count_detail, success_detail, row["id"]),
            )
        else:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.outside_ff
                SET status = 'done'
                WHERE id = %s
                """,
                (row["id"],),
            )
        return "test_ok" if is_test_mode else "ok"


def get_outside_ff_call(call_id: str) -> Optional[dict]:
    """Запись outside_ff с кодом проверки и статусом ФФ (для опроса асинхронного тестового вызова)."""
    cid = call_id.strip()
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT o.id, o.ff_id, o.product_code, o.call_id, o.status,
                   o.is_check, o.json_details, o.count_detail, o.success_detail,
                   ff.code AS ff_code, ff.status AS ff_status
            FROM {SCHEMA}.outside_ff o
            JOIN {SCHEMA}.fitness_function ff ON ff.id = o.ff_id
            WHERE o.call_id = %s
            """,
            (cid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        raw = d.get("ff_status")
        d["ff_status"] = _row_ff_status({"status": raw})
        return d


def product_has_actual_ff_pass(product_code: str, ff_id: int) -> bool:
    """
    True, если для пары (product_code, ff_id) есть актуальная запись product_ff с is_check = true.
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT 1
            FROM {SCHEMA}.product_ff
            WHERE product_code = %s AND ff_id = %s AND is_actual = true AND is_check = true
            LIMIT 1
            """,
            (product_code.strip(), ff_id),
        )
        return cur.fetchone() is not None


def get_fitness_function_applicabilities() -> dict:
    """
    Возвращает словарь {code: applicability} для всех fitness_function.
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT code, applicability
            FROM {SCHEMA}.fitness_function
            """
        )
        rows = cur.fetchall()
    return {r["code"]: r.get("applicability") for r in rows}


def add_fitness_function(
    code: str,
    description: str,
    applicability: Optional[str],
    auxiliary_check: bool = False,
    status: str = FF_STATUS_TEST,
    script: Optional[str] = None,
    set_script: bool = False,
    method: Optional[str] = None,
    set_method: bool = False,
    method_synchronous: bool = False,
    set_method_synchronous: bool = False,
    create_only: bool = False,
) -> Optional[int]:
    """
    Добавляет новую фитнес-функцию (если такой code ещё нет) и возвращает её id.
    Если запись с таким code уже существует — при create_only=False обновляет поля и возвращает id;
    при create_only=True не меняет строку и возвращает None.
    status: TEST | TRIAL | ADOPT.
    При смене статуса с/на TEST удаляются product_ff и outside_ff для этой функции.
    """
    status_norm = normalize_ff_status(status)
    method_value: Optional[str] = None
    if set_method:
        method_value = method.strip() if method and str(method).strip() else None

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, status FROM {SCHEMA}.fitness_function
            WHERE code = %s
            """,
            (code,),
        )
        row = cur.fetchone()
        if row:
            if create_only:
                return None
            ff_id = row["id"]
            old_status = _row_ff_status(dict(row))
            if should_clear_ff_run_data(old_status, status_norm):
                cur.execute(
                    f"DELETE FROM {SCHEMA}.product_ff WHERE ff_id = %s",
                    (ff_id,),
                )
                cur.execute(
                    f"DELETE FROM {SCHEMA}.outside_ff WHERE ff_id = %s",
                    (ff_id,),
                )
            sets = [
                "description = %s",
                "applicability = %s",
                "auxiliary_check = %s",
                "status = %s",
            ]
            params: list = [description, applicability, auxiliary_check, status_norm]
            if set_script:
                sets.append("script = %s")
                params.append(script)
            if set_method:
                sets.append("method = %s")
                params.append(method_value)
            if set_method_synchronous:
                sets.append("method_synchronous = %s")
                params.append(method_synchronous)
            params.append(ff_id)
            cur.execute(
                f"""
                UPDATE {SCHEMA}.fitness_function
                SET {", ".join(sets)}
                WHERE id = %s
                """,
                params,
            )
            return ff_id

        ms_val = method_synchronous if set_method_synchronous else False
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.fitness_function
                (code, description, applicability, auxiliary_check, status, script, method,
                 method_synchronous)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                code,
                description,
                applicability,
                auxiliary_check,
                status_norm,
                script if set_script else None,
                method_value if set_method else None,
                ms_val,
            ),
        )
        new_row = cur.fetchone()
        return new_row["id"]


def get_all_fitness_functions() -> list[dict]:
    """Возвращает список всех fitness_function (включая вспомогательные — для админки)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, applicability, auxiliary_check, status, script, method,
                   method_synchronous
            FROM {SCHEMA}.fitness_function
            ORDER BY id
            """
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _row_ff_status(d)
        out.append(d)
    return out


def get_fitness_function_codes_excluded_from_run_all() -> set[str]:
    """Коды проверок со статусом TEST (не входят в run-all)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT code, status FROM {SCHEMA}.fitness_function
            """
        )
        rows = cur.fetchall()
    return {
        r["code"]
        for r in rows
        if r.get("code") and excluded_from_run_all(_row_ff_status(dict(r)))
    }


def get_actual_results_by_product_code(product_code: str) -> list[dict]:
    """
    По коду продукта (внешняя мнемоника) возвращает актуальные записи product_ff для основных проверок:
    не включаются fitness_function с auxiliary_check = true или status = TEST.
    Поля: id, product_code, ff_id, ff_code, ff_description, is_check, create_date,
    json_details, count_detail, success_detail.
    Если записей нет — пустой список.
    Сравнение кода продукта без учёта регистра.
    """
    code = product_code.strip()
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                pf.id,
                pf.product_code,
                pf.ff_id,
                pf.is_check,
                pf.create_date,
                pf.json_details,
                pf.count_detail,
                pf.success_detail,
                ff.code AS ff_code,
                ff.description AS ff_description,
                ff.status AS ff_status
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE LOWER(pf.product_code) = LOWER(%s) AND pf.is_actual = true
              AND (ff.auxiliary_check IS NOT TRUE)
              AND (ff.status IN (%s, %s))
            ORDER BY pf.create_date
            """,
            (code, FF_STATUS_TRIAL, FF_STATUS_ADOPT),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ff_status"] = _row_ff_status({"status": d.get("ff_status")})
        out.append(d)
    return out


def init_schema():
    """Создание схемы ff, таблиц и тестовых данных при первом старте."""
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.fitness_function (
                id SERIAL PRIMARY KEY,
                code TEXT,
                description TEXT,
                applicability TEXT,
                auxiliary_check BOOLEAN NOT NULL DEFAULT false,
                test BOOLEAN NOT NULL DEFAULT false,
                script TEXT,
                method TEXT
            );
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.outside_ff (
                id SERIAL PRIMARY KEY,
                ff_id INT NOT NULL REFERENCES {SCHEMA}.fitness_function(id) ON DELETE CASCADE,
                product_code TEXT NOT NULL,
                call_id TEXT NOT NULL,
                status TEXT NOT NULL,
                CONSTRAINT outside_ff_call_id_unique UNIQUE (call_id)
            );
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.product_ff (
                id SERIAL PRIMARY KEY,
                product_code TEXT NOT NULL,
                ff_id INT NOT NULL REFERENCES {SCHEMA}.fitness_function(id),
                is_check BOOLEAN NOT NULL,
                is_actual BOOLEAN NOT NULL DEFAULT false,
                json_details TEXT,
                count_detail INT,
                success_detail INT,
                create_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS is_actual BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS json_details TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS count_detail INT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS success_detail INT;
        """)

        cur.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = '{SCHEMA}' AND table_name = 'product_ff' AND column_name = 'product_id'
              ) THEN
                ALTER TABLE {SCHEMA}.product_ff ADD COLUMN IF NOT EXISTS product_code TEXT;
                UPDATE {SCHEMA}.product_ff pf
                SET product_code = COALESCE(
                  (SELECT p.alias FROM {SCHEMA}.product p WHERE p.id = pf.product_id),
                  'LEGACY-' || pf.product_id::text
                )
                WHERE pf.product_code IS NULL;
                ALTER TABLE {SCHEMA}.product_ff DROP CONSTRAINT IF EXISTS product_ff_product_id_fkey;
                ALTER TABLE {SCHEMA}.product_ff DROP COLUMN product_id;
                ALTER TABLE {SCHEMA}.product_ff ALTER COLUMN product_code SET NOT NULL;
              END IF;
            END $$;
        """)

        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS applicability TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS auxiliary_check BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS script TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS method TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS method_synchronous BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS is_check BOOLEAN;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS json_details TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS count_detail INT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS success_detail INT;
        """)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_meta (
                key TEXT PRIMARY KEY
            );
            """
        )
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS status TEXT;
        """)
        cur.execute(
            f"""
            WITH ins AS (
                INSERT INTO {SCHEMA}.schema_meta (key)
                SELECT 'fitness_function_status_from_test'
                WHERE NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.schema_meta
                    WHERE key = 'fitness_function_status_from_test'
                )
                RETURNING 1
            )
            UPDATE {SCHEMA}.fitness_function ff
            SET status = CASE WHEN ff.test IS TRUE THEN 'TEST' ELSE 'ADOPT' END
            WHERE ff.status IS NULL
              AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = '{SCHEMA}'
                  AND table_name = 'fitness_function'
                  AND column_name = 'test'
              )
              AND EXISTS (SELECT 1 FROM ins);
            """
        )
        cur.execute(
            f"""
            UPDATE {SCHEMA}.fitness_function
            SET status = 'TEST'
            WHERE status IS NULL;
            """
        )
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ALTER COLUMN status SET DEFAULT 'TEST';
        """)
        cur.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM {SCHEMA}.schema_meta
                WHERE key = 'fitness_function_drop_test_column'
              ) THEN
                ALTER TABLE {SCHEMA}.fitness_function DROP COLUMN IF EXISTS test;
                INSERT INTO {SCHEMA}.schema_meta (key)
                VALUES ('fitness_function_drop_test_column');
              END IF;
            END $$;
            """
        )
        # Одноразово: для уже существовавших проверок с непустым method признак синхронности = false (асинхронный webhook).
        cur.execute(
            f"""
            WITH ins AS (
                INSERT INTO {SCHEMA}.schema_meta (key)
                SELECT 'fitness_function_method_synchronous_async_backfill'
                WHERE NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.schema_meta
                    WHERE key = 'fitness_function_method_synchronous_async_backfill'
                )
                RETURNING 1
            )
            UPDATE {SCHEMA}.fitness_function
            SET method_synchronous = false
            WHERE method IS NOT NULL
              AND BTRIM(method) <> ''
              AND EXISTS (SELECT 1 FROM ins);
            """
        )

        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.product;")

        cur.execute(f"SELECT 1 FROM {SCHEMA}.fitness_function WHERE id = 1;")
        if cur.fetchone() is None:
            cur.execute(f"""
                INSERT INTO {SCHEMA}.fitness_function (id, code, description)
                VALUES (1, 'DEMOFF-1', 'Тестовая фитнес-функция');
            """)
        cur.execute(f"SELECT 1 FROM {SCHEMA}.fitness_function WHERE id = 2;")
        if cur.fetchone() is None:
            cur.execute(f"""
                INSERT INTO {SCHEMA}.fitness_function (id, code, description)
                VALUES (2, 'DEMOFF-2', 'Тестовая фитнес-функция 2');
            """)

        cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {SCHEMA}.fitness_function;")
        max_ff_id = cur.fetchone()[0]
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{SCHEMA}.fitness_function', 'id'), %s);",
            (max_ff_id,),
        )

    finally:
        cur.close()
        conn.close()
