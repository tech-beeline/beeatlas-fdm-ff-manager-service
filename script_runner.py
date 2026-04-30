"""Обнаружение и запуск скриптов проверок. Поддержка добавления/удаления скриптов без перезапуска."""
import errno
import importlib.util
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional, Tuple

from config import settings
from db import get_all_fitness_functions, get_latest_check_result
from scripts._common import coerce_execute_result, persist_execute_result

# Если целевой каталог (например смонтированный volume) только для чтения — сканируем /scripts-src из образа.
_scripts_dir_override: Optional[Path] = None

BUNDLED_SCRIPTS_SRC = Path("/scripts-src")


def _copy_bundled_into_dest(src: Path, dest: Path) -> None:
    """
    Копирует только содержимое src в dest (файлы — copy2, подкаталоги — рекурсивно).
    Не использует copytree(src, dest) целиком: иначе в конце вызывается copystat для пары
    корневых каталогов, что на части окружений даёт EPERM «Operation not permitted».
    """
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            _copy_bundled_into_dest(item, target)
        else:
            shutil.copy2(item, target)


def _configured_scripts_dir() -> Path:
    """Каталог скриптов из настроек, относительно корня приложения."""
    root = Path(__file__).resolve().parent
    sub = (settings.scripts_dir or "scripts").strip()
    if not sub or sub == "/":
        sub = "scripts"
    # Если в env задать абсолютный путь (например FF_SCRIPTS_DIR=/scripts), то
    # root / "/scripts" в pathlib превращается в /scripts — корень ФС, часто без прав записи.
    rel = sub.lstrip("/")
    return (root / rel) if rel else root / "scripts"


def get_scripts_dir() -> Path:
    """Каталог со скриптами проверок (после startup может указывать на /scripts-src при RO volume)."""
    if _scripts_dir_override is not None:
        return _scripts_dir_override
    return _configured_scripts_dir()


def ensure_scripts_dir() -> None:
    """
    Создаёт каталог скриптов при отсутствии и копирует в него файлы из /scripts-src
    (в образе Docker скрипты дублируются туда из исходного каталога — см. Dockerfile).
    Если запись в целевой каталог невозможна (только чтение) — используется /scripts-src без копирования.
    Если /scripts-src нет (локальный запуск), только гарантирует наличие каталога.
    """
    global _scripts_dir_override

    dest = _configured_scripts_dir()
    if not BUNDLED_SCRIPTS_SRC.is_dir():
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return

    def use_bundled() -> None:
        global _scripts_dir_override
        _scripts_dir_override = BUNDLED_SCRIPTS_SRC

    def should_use_bundled_exc(err: object) -> bool:
        if isinstance(err, OSError) and err.errno in (errno.EROFS, errno.EPERM):
            return True
        if isinstance(err, str) and (
            "Read-only file system" in err or "Operation not permitted" in err
        ):
            return True
        return False

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if should_use_bundled_exc(e):
            use_bundled()
            return
        raise

    probe = dest / ".ff_manager_write_probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        if should_use_bundled_exc(e):
            use_bundled()
            return
        raise

    try:
        _copy_bundled_into_dest(BUNDLED_SCRIPTS_SRC, dest)
    except OSError as e:
        if should_use_bundled_exc(e):
            use_bundled()
            return
        raise
    except shutil.Error as e:
        for item in e.args[0] if e.args else ():
            if len(item) >= 3:
                why = item[2]
                if isinstance(why, str) and should_use_bundled_exc(why):
                    use_bundled()
                    return
                if isinstance(why, OSError) and should_use_bundled_exc(why):
                    use_bundled()
                    return
        raise


def materialize_missing_scripts_from_db() -> None:
    """
    Для строк fitness_function с непустым script в БД создаёт файл {code}.py в каталоге скриптов,
    если такого файла ещё нет. Не перезаписывает существующие файлы.
    """
    scripts_dir = get_scripts_dir()
    try:
        scripts_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    rows = get_all_fitness_functions()
    for row in rows:
        code = (row.get("code") or "").strip()
        script = row.get("script")
        if not code:
            continue
        if script is None or not str(script).strip():
            continue
        path = scripts_dir / f"{code}.py"
        if path.exists():
            continue
        try:
            path.write_text(str(script), encoding="utf-8")
        except OSError:
            continue


def list_scripts() -> list[str]:
    """
    Перечень кодов всех доступных скриптов проверок.
    Сканирует каталог при каждом вызове — добавление/удаление файлов учитывается без перезапуска.
    """
    scripts_dir = get_scripts_dir()
    if not scripts_dir.is_dir():
        return []
    codes = []
    for path in scripts_dir.iterdir():
        # Только скрипты проверок: .py, не __init__, не вспомогательные модули (_common и т.п.)
        if (
            path.suffix.lower() == ".py"
            and path.name != "__init__.py"
            and not path.stem.startswith("_")
        ):
            codes.append(path.stem)
    return sorted(codes)


def _script_path(code: str) -> Optional[Path]:
    scripts_dir = get_scripts_dir()
    path = scripts_dir / f"{code}.py"
    return path if path.is_file() else None


def run_script(code: str, app_mnemonic: str) -> Tuple[bool, str, Optional[bool]]:
    """
    Запуск одного скрипта проверки для приложения.
    :param code: Код проверки (имя скрипта без .py), например SEQ01 или DEMOFF-1.
    :param app_mnemonic: Мнемоника приложения (строка).
    :return: (успех, вывод скрипта или сообщение об ошибке, результат проверки is_check или None).
    """
    path = _script_path(code)
    if not path:
        return False, f"Скрипт с кодом '{code}' не найден.", None

    env = os.environ.copy()
    env["FF_DB_HOST"] = settings.db_host
    env["FF_DB_PORT"] = str(settings.db_port)
    env["FF_DB_USER"] = settings.db_user
    env["FF_DB_PASSWORD"] = settings.db_password
    env["FF_DB_NAME"] = settings.db_name
    env["FF_API_BASE_URL"] = settings.api_base_url

    try:
        ok, out, done, check_from_execute = _run_script_module(path, code, app_mnemonic, env)
        if done:
            if not ok:
                return False, out, None
            return True, out or "OK", check_from_execute

        result = subprocess.run(
            [sys.executable, str(path), app_mnemonic],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        out = (result.stdout or "").strip() or (result.stderr or "").strip()
        if result.returncode != 0:
            return False, out or f"Код возврата: {result.returncode}", None
        check_result = get_latest_check_result(app_mnemonic, code)
        return True, out or "OK", check_result
    except subprocess.TimeoutExpired:
        return False, "Таймаут выполнения скрипта.", None
    except Exception as e:
        return False, str(e), None


def _run_script_module(
    path: Path, code: str, app_mnemonic: str, env: dict
) -> Tuple[bool, str, bool, Optional[bool]]:
    """
    Пытается выполнить скрипт как модуль через функцию execute(app_code).
    Возвращает:
      - ok: успешность выполнения execute
      - output: stdout/stderr функции
      - done: True, если запуск через execute был выполнен;
              False, если execute отсутствует и нужен fallback на subprocess.
      - is_check: результат проверки после сохранения через API, либо None если execute не вызывался.
    """
    spec = importlib.util.spec_from_file_location(f"ff_script_{code}", str(path))
    if spec is None or spec.loader is None:
        return False, "Не удалось подготовить импорт скрипта.", True, None

    module = importlib.util.module_from_spec(spec)
    old_env = dict(os.environ)
    capture = io.StringIO()
    try:
        os.environ.update(env)
        spec.loader.exec_module(module)
        execute = getattr(module, "execute", None)
        if not callable(execute):
            return True, "", False, None
        with redirect_stdout(capture), redirect_stderr(capture):
            raw = execute(app_mnemonic)
        result = coerce_execute_result(raw, app_mnemonic, code)
        persist_execute_result(
            product_code=app_mnemonic.strip(),
            ff_code=code.strip(),
            is_check=result["is_check"],
            details=result["details"],
        )
        return True, capture.getvalue().strip(), True, result["is_check"]
    except Exception as e:
        output = capture.getvalue().strip()
        msg = f"{output}\n{e}".strip() if output else str(e)
        return False, msg, True, None
    finally:
        os.environ.clear()
        os.environ.update(old_env)

