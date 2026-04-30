"""Подключение к PostgreSQL и инициализация схемы ff."""
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

from config import settings

SCHEMA = "ff"


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
    Возвращает ("ok", id_новой_строки) или ("fitness_function_not_found", None).
    """
    code_key = product_code.strip()
    code = ff_code.strip()
    with get_cursor() as cur:
        cur.execute(
            f"SELECT id FROM {SCHEMA}.fitness_function WHERE code = %s",
            (code,),
        )
        row = cur.fetchone()
        if not row:
            return ("fitness_function_not_found", None)
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


def get_fitness_function_by_code(code: str) -> Optional[dict]:
    """Одна строка fitness_function по code или None."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, applicability, auxiliary_check, test, script, method
            FROM {SCHEMA}.fitness_function
            WHERE code = %s
            """,
            (code.strip(),),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None


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
        cur.execute(
            f"""
            UPDATE {SCHEMA}.outside_ff
            SET status = 'done'
            WHERE id = %s
            """,
            (row["id"],),
        )
        return "ok"


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
    test: bool = False,
    script: Optional[str] = None,
    set_script: bool = False,
    method: Optional[str] = None,
    set_method: bool = False,
    create_only: bool = False,
) -> Optional[int]:
    """
    Добавляет новую фитнес-функцию (если такой code ещё нет) и возвращает её id.
    Если запись с таким code уже существует — при create_only=False обновляет поля и возвращает id;
    при create_only=True не меняет строку и возвращает None.
    script: текст скрипта (.py) для хранения в БД.
    set_script: если True — записать/обновить колонку script; если False — при UPDATE колонку script не менять.
    method: URL внешнего POST; непустой method означает внешнюю проверку (вызов вместо скрипта).
    set_method: если True — записать/очистить колонку method (пустая строка -> NULL).
    auxiliary_check: вспомогательная проверка (не в основных результатах продукта, но в run-all).
    test: тестовая проверка (не в run-all и не в actual-results продукта).
    При обновлении: если test меняется с true на false — удаляются product_ff и outside_ff для этой функции.
    """
    method_value: Optional[str] = None
    if set_method:
        method_value = method.strip() if method and str(method).strip() else None

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, test FROM {SCHEMA}.fitness_function
            WHERE code = %s
            """,
            (code,),
        )
        row = cur.fetchone()
        if row:
            if create_only:
                return None
            ff_id = row["id"]
            old_test = row.get("test")
            if old_test is True and test is False:
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
                "test = %s",
            ]
            params: list = [description, applicability, auxiliary_check, test]
            if set_script:
                sets.append("script = %s")
                params.append(script)
            if set_method:
                sets.append("method = %s")
                params.append(method_value)
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

        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.fitness_function
                (code, description, applicability, auxiliary_check, test, script, method)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                code,
                description,
                applicability,
                auxiliary_check,
                test,
                script if set_script else None,
                method_value if set_method else None,
            ),
        )
        new_row = cur.fetchone()
        return new_row["id"]


def get_all_fitness_functions() -> list[dict]:
    """Возвращает список всех fitness_function (включая вспомогательные — для админки)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, applicability, auxiliary_check, test, script, method
            FROM {SCHEMA}.fitness_function
            ORDER BY id
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_fitness_function_codes_with_test_true() -> set[str]:
    """Коды проверок с test = true (исключаются из run-all и из actual-results продукта)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT code FROM {SCHEMA}.fitness_function
            WHERE test IS TRUE
            """
        )
        rows = cur.fetchall()
    return {r["code"] for r in rows}


def get_actual_results_by_product_code(product_code: str) -> list[dict]:
    """
    По коду продукта (внешняя мнемоника) возвращает актуальные записи product_ff для основных проверок:
    не включаются fitness_function с auxiliary_check = true или test = true.
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
                ff.description AS ff_description
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE LOWER(pf.product_code) = LOWER(%s) AND pf.is_actual = true
              AND (ff.auxiliary_check IS NOT TRUE)
              AND (ff.test IS NOT TRUE)
            ORDER BY pf.create_date
            """,
            (code,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
