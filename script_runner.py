"""Обнаружение и запуск скриптов проверок. Поддержка добавления/удаления скриптов без перезапуска."""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from config import settings
from db import get_latest_check_result


def get_scripts_dir() -> Path:
    """Каталог со скриптами проверок (относительно корня проекта)."""
    root = Path(__file__).resolve().parent
    return root / settings.scripts_dir


BUNDLED_SCRIPTS_SRC = Path("/scripts-src")


def ensure_scripts_dir() -> None:
    """
    Создаёт каталог скриптов при отсутствии и копирует в него файлы из /scripts-src
    (в образе Docker скрипты дублируются туда из исходного каталога — см. Dockerfile).
    Если /scripts-src нет (локальный запуск), только гарантирует наличие каталога.
    """
    dest = get_scripts_dir()
    if BUNDLED_SCRIPTS_SRC.is_dir():
        shutil.copytree(BUNDLED_SCRIPTS_SRC, dest, dirs_exist_ok=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)


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


