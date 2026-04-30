"""
HTTP-клиент с HMAC-аутентификацией Structurizr (X-Authorization, Nonce).

Строка подписи: METHOD\\nPATH\\nMD5(body)\\nCONTENT_TYPE\\nNONCE\\n
Подпись: Base64(HMAC-SHA256(message, apiSecret)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

from config import settings


class CredentialsFetchError(Exception):
    """Не удалось получить structurizrApiKey / structurizrApiSecret из FDM API."""


def fetch_structurizr_credentials(app_code: str) -> tuple[str, str]:
    """
    GET {fdm_product_api_base_url}/api/v1/product/{app_code}
    Ожидаются поля structurizrApiKey и structurizrApiSecret в JSON.
    """
    alias = app_code.strip()
    if not alias:
        raise CredentialsFetchError("Код приложения пустой.")

    base = settings.fdm_product_api_base_url.rstrip("/")
    safe = urllib.parse.quote(alias, safe="")
    url = f"{base}/api/v1/product/{safe}"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=settings.fdm_product_api_timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise CredentialsFetchError(
            f"FDM API вернуло HTTP {e.code} при запросе ключей Structurizr: {body[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise CredentialsFetchError(f"Не удалось обратиться к FDM API для ключей Structurizr: {e}") from e
    except OSError as e:
        raise CredentialsFetchError(f"Ошибка сети при запросе ключей Structurizr: {e}") from e

    try:
        data: Mapping[str, Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialsFetchError(f"Ответ FDM API не является JSON: {e}") from e

    key = data.get("structurizrApiKey") or data.get("structurizr_api_key")
    secret = data.get("structurizrApiSecret") or data.get("structurizr_api_secret")
    if not isinstance(key, str) or not key.strip():
        raise CredentialsFetchError("В ответе FDM API отсутствует structurizrApiKey.")
    if not isinstance(secret, str) or not secret.strip():
        raise CredentialsFetchError("В ответе FDM API отсутствует structurizrApiSecret.")
    return key.strip(), secret.strip()


def _normalize_path(path: str) -> str:
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _hmac_signature(api_secret: str, message: str) -> str:
    mac = hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(mac).decode("ascii")


def build_hmac_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    content_type: str,
    api_key: str,
    api_secret: str,
) -> dict[str, str]:
    """Заголовки X-Authorization и Nonce для одного запроса."""
    m = method.upper()
    p = _normalize_path(path)
    md5_hex = hashlib.md5(body).hexdigest()
    ct = content_type or ""
    nonce = f"{int(time.time() * 1000)}-{random.randint(0, 2**31 - 1)}"
    message = f"{m}\n{p}\n{md5_hex}\n{ct}\n{nonce}\n"
    sig = _hmac_signature(api_secret, message)
    return {
        "X-Authorization": f"{api_key}:{sig}",
        "Nonce": nonce,
    }


class StructurizrHmacClient:
    """
    Исходящие запросы на базовый URL из настроек; путь — как в API Structurizr (например /workspace/1).
    Ключи передаются извне (раннер при POST /run / run-all), скрипты используют обёртку из _common.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        timeout: Optional[float] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout = timeout if timeout is not None else settings.structurizr_http_timeout_seconds

    @classmethod
    def from_env(cls) -> StructurizrHmacClient:
        """Собирает клиент из переменных окружения (выставляет раннер)."""
        import os

        base = os.environ.get("FF_STRUCTURIZR_HTTP_BASE_URL", "").strip()
        key = os.environ.get("FF_STRUCTURIZR_API_KEY", "").strip()
        secret = os.environ.get("FF_STRUCTURIZR_API_SECRET", "").strip()
        if not base or not key or not secret:
            raise RuntimeError(
                "Structurizr HMAC клиент недоступен: нет FF_STRUCTURIZR_HTTP_BASE_URL / "
                "FF_STRUCTURIZR_API_KEY / FF_STRUCTURIZR_API_SECRET "
                "(они задаются только при запуске через POST /api/v1/run или /api/v1/run-all)."
            )
        t = os.environ.get("FF_STRUCTURIZR_HTTP_TIMEOUT")
        timeout = float(t) if t and t.strip() else None
        return cls(base, key, secret, timeout=timeout)

    def _full_url(self, path: str) -> str:
        return self._base + _normalize_path(path)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        content_type: str = "",
        extra_headers: Optional[dict[str, str]] = None,
    ) -> tuple[int, bytes]:
        """
        Выполняет запрос. path — путь на стороне Structurizr (тот же, что в строке подписи).
        Для GET body=None и content_type ''.
        """
        raw = body if body is not None else b""
        m = method.upper()
        hdrs = build_hmac_headers(
            method=m,
            path=_normalize_path(path),
            body=raw,
            content_type=content_type,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        if extra_headers:
            hdrs.update(extra_headers)
        if content_type:
            hdrs["Content-Type"] = content_type
        url = self._full_url(path)
        req_data: Optional[bytes] = None if m in ("GET", "HEAD") else raw
        req = urllib.request.Request(url, data=req_data, method=m, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.getcode() or 200, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if e.fp else b""

    def get(self, path: str, *, accept: str = "*/*") -> tuple[int, bytes]:
        return self.request("GET", path, body=b"", content_type="", extra_headers={"Accept": accept})

    def post_json(self, path: str, payload: Any) -> tuple[int, bytes]:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.request(
            "POST",
            path,
            body=raw,
            content_type="application/json; charset=utf-8",
            extra_headers={"Accept": "*/*"},
        )
