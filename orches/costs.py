"""Normalize OMP RPC events that describe a billed model request.

OMP reports resolved provider/model and token usage on each completed assistant
message (``message_end``) and on cache-warming refreshes that reached the provider
(``cache_warming_end`` with ``usage``). ``usage.cost.total`` is OMP's own estimate or
the server-reported amount; Orches stores it as-is and never prices tokens itself.
"""

from __future__ import annotations

import math
from typing import Any

UNKNOWN_MODEL = "Unknown model"


def _as_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        value = int(value)
    return value if value >= 0 else None


def _as_cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) and amount >= 0 else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _usage_fields(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        usage = {}
    cost = usage.get("cost")
    return {
        "input_tokens": _as_count(usage.get("input")),
        "output_tokens": _as_count(usage.get("output")),
        "cache_read_tokens": _as_count(usage.get("cacheRead")),
        "cache_write_tokens": _as_count(usage.get("cacheWrite")),
        "total_tokens": _as_count(usage.get("totalTokens")),
        "cost_usd": _as_cost(cost.get("total")) if isinstance(cost, dict) else None,
    }


def parse_cost_event(event: dict[str, Any], index: int) -> dict[str, Any] | None:
    """Return a ledger record for a billed request, or ``None`` for other events.

    ``index`` is the session-local position of the event and only keys events that
    carry no ``messageId``. Missing or malformed usage yields ``None`` counters and
    ``cost_usd=None`` (unpriced), never zero.
    """
    kind = event.get("type")
    if kind == "message_end":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        ident = _text(event.get("messageId"))
        return {
            "event_key": f"message:{ident}" if ident else f"message:{index}",
            "provider": _text(message.get("provider")),
            "model": _text(message.get("model")),
            **_usage_fields(message.get("usage")),
        }
    if kind == "cache_warming_end":
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        return {
            "event_key": f"warming:{index}",
            "provider": _text(event.get("provider")),
            "model": _text(event.get("model")),
            **_usage_fields(usage),
        }
    return None


def model_key(provider: str | None, model: str | None, selector: str | None) -> str:
    if provider and model:
        return f"{provider}/{model}"
    return selector if selector and selector.strip() else UNKNOWN_MODEL
