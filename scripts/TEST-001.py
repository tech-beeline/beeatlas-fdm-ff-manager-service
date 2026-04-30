#!/usr/bin/env python3
"""
Проверка ADR-01: в документе Structurizr должен быть хотя бы один ADR.

Ожидается, что data передается раннером FF Manager из docId:
- data["documentation"]["decisions"] -> список ADR.
"""
import os
import sys
import json
import urllib.parse
from typing import Any, Dict

SCRIPT_CODE = os.path.splitext(os.path.basename(__file__))[0]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ExecuteResult, structurizr_http_client


def _build_adr_link(share_url: str, adr_id: str, adr_title: str) -> str:
    if not share_url:
        return adr_title
    return f"<a href='{share_url}/decisions#{adr_id}' target='_blank'>{adr_title}</a>"


def _fetch_share_url(app_code: str) -> str:
    if not app_code:
        return ""

    safe_code = urllib.parse.quote(app_code, safe="")
    path = f"/product//api/v1/product/{safe_code}"
    try:
        client = structurizr_http_client()
        status, raw_bytes = client.get(path)
    except Exception:
        return ""

    if not (200 <= status < 300):
        return ""

    try:
        payload = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except ValueError:
        return ""

    if not isinstance(payload, dict):
        return ""

    value = payload.get("structurizrApiUrl")
    return value if isinstance(value, str) else ""


def execute(app_code: str, data: Dict[str, Any]) -> ExecuteResult:
    documentation = data.get("documentation", {})
    decisions = documentation.get("decisions", [])
    share_url = _fetch_share_url(app_code)

    details: list[dict[str, Any]] = []
    if isinstance(decisions, list):
        for i, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            adr_id = str(decision.get("id") or f"adr_{i}")
            adr_title = str(decision.get("title") or f"ADR {i + 1}")
            details.append(
                {
                    "check": True,
                    "adrId": adr_id,
                    "adrTitle": adr_title,
                    "adrLink": _build_adr_link(share_url, adr_id, adr_title),
                }
            )

    is_check = len(details) > 0
    if not is_check:
        details = [{"check": False, "reason": "ADR не найдены в documentation.decisions"}]

    return ExecuteResult(
        app_code=app_code,
        script_code=SCRIPT_CODE,
        is_check=is_check,
        details=details,
    )