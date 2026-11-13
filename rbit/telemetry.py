"""Opt-in, anonymous usage pings. Strictly disabled unless the user configures it."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

logger = logging.getLogger("rbit.telemetry")


def _anonymous_id(workspace: str) -> str:
    return hashlib.sha256(workspace.encode("utf-8", errors="replace")).hexdigest()[:16]


def track_event(cfg: dict[str, Any], name: str, props: dict[str, Any] | None = None) -> None:
    """Fire-and-forget ping; a no-op unless cfg.telemetry enables it with an endpoint."""
    tel = cfg.get("telemetry")
    if not isinstance(tel, dict) or not tel.get("enabled"):
        return
    endpoint = str(tel.get("endpoint") or "").strip()
    if not endpoint:
        return
    payload = {
        "event": name,
        "anonymous_id": _anonymous_id(str(cfg.get("_workspace", ""))),
        "props": props or {},
    }
    try:
        httpx.post(
            endpoint,
            json=payload,
            timeout=2.0,
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        logger.debug("telemetry ping failed", exc_info=True)
