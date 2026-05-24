"""
Статусная модель fitness_function: TEST, TRIAL, ADOPT.

TRIAL и ADOPT — только маркеры жизненного цикла; поведение при run / product_ff / run-all одинаковое.
Отличия в логике только у TEST.
"""

FF_STATUS_TEST = "TEST"
FF_STATUS_TRIAL = "TRIAL"
FF_STATUS_ADOPT = "ADOPT"

FF_STATUSES = frozenset({FF_STATUS_TEST, FF_STATUS_TRIAL, FF_STATUS_ADOPT})


def normalize_ff_status(value: str) -> str:
    """Проверяет и нормализует статус (верхний регистр)."""
    if value is None:
        raise ValueError("Статус не задан")
    s = str(value).strip().upper()
    if s not in FF_STATUSES:
        raise ValueError(f"Недопустимый статус '{value}'; допустимо: TEST, TRIAL, ADOPT")
    return s


def skips_product_ff_persistence(status: str) -> bool:
    """Результаты не пишутся в product_ff (только TEST)."""
    return normalize_ff_status(status) == FF_STATUS_TEST


def skips_applicability_check(status: str) -> bool:
    """При run не проверяется applicability (только TEST)."""
    return normalize_ff_status(status) == FF_STATUS_TEST


def excluded_from_run_all(status: str) -> bool:
    """Не участвует в run-all (только TEST)."""
    return normalize_ff_status(status) == FF_STATUS_TEST


def excluded_from_actual_results(status: str) -> bool:
    """Не попадает в actual-results (только TEST)."""
    return normalize_ff_status(status) == FF_STATUS_TEST


def should_clear_ff_run_data(old_status: str, new_status: str) -> bool:
    """
    Очистить product_ff и outside_ff при выходе из TEST или при возврате в TEST.
    """
    old_s = normalize_ff_status(old_status) if old_status else FF_STATUS_TEST
    new_s = normalize_ff_status(new_status)
    if old_s == FF_STATUS_TEST and new_s != FF_STATUS_TEST:
        return True
    if new_s == FF_STATUS_TEST and old_s != FF_STATUS_TEST:
        return True
    return False
