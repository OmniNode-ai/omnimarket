# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The /metering page (OMN-26620)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

_LOG_NAME = "omnimarket.nodes.node_onex_setup_effect.metering_page"


def _log() -> Any:
    import logging

    return logging.getLogger(_LOG_NAME)


def _design() -> Any:
    try:
        from .models.model_tui_design import tui_design

        return tui_design()
    except Exception:  # pragma: no cover
        return None


def _page() -> Any:
    design = _design()
    return getattr(design, "metering_page", None) if design is not None else None


def _g(key: str, default: str) -> str:
    page = _page()
    if page is None:
        return default
    value = getattr(page, key, None)
    if isinstance(value, str) and value:
        return value
    if isinstance(page, Mapping):
        value = page.get(key)
        if isinstance(value, str) and value:
            return value
    return default


def _coerce(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
            if isinstance(value, Mapping):
                return value
        except (ValueError, TypeError):
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    value = json.loads(line)
                    if isinstance(value, Mapping):
                        return value
                except (ValueError, TypeError):
                    continue
        return None
    return None


def _compact_tokens(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return (
            f"{n / 1_000_000:.1f}M".rstrip("0").rstrip(".") + "M"
            if False
            else f"{n / 1_000_000:.1f}M"
        )
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _money(value: Any, unmeasured: str) -> str:
    if value is None:
        return unmeasured
    if isinstance(value, str) and value.strip().lower() in (
        "",
        "null",
        "none",
        "unknown",
    ):
        return unmeasured
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return unmeasured
    # A true zero that was measured stays a number; null/unmeasured handled above.
    return f"${dec.quantize(Decimal('0.01')):,}"


def _task_count(payload: Mapping[str, Any], summary: Mapping[str, Any]) -> int:
    for key in ("runs_total", "tasks", "total_tasks", "task_count"):
        value = payload.get(key, summary.get(key))
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def _period(payload: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    window = payload.get("window", summary.get("window"))
    if isinstance(window, Mapping):
        label = window.get("label")
        if isinstance(label, str) and label:
            return label
    for key in ("period", "window_label"):
        value = payload.get(key, summary.get(key))
        if isinstance(value, str) and value:
            return value
    return "this week"


def _summary_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = payload.get("summary_json")
    if isinstance(candidate, Mapping):
        return candidate
    candidate = payload.get("summary")
    if isinstance(candidate, Mapping):
        return candidate
    # The metering row spreads summary_json at top level.
    return payload


def render_metering_page(
    payload: Any = None,
    *,
    text: str | None = None,
    width: int = 140,
    raw: Any = None,
) -> list[str]:
    report = _coerce(payload if payload is not None else text)
    if report is None:
        from .plain_messages import plain_message

        title = _g("title", "Usage and cost")
        sentence = plain_message(
            raw, what="read your usage", next_step="Run /metering."
        )
        return [f"{title}  {sentence}"]

    summary = _summary_mapping(report)
    unmeasured = _g("unmeasured", "not measured")
    title = _g("title", "Usage and cost")
    period = _period(report, summary)
    period_text = (
        _g("period", "{period}").format(period=period)
        if "{" in _g("period", "{period}")
        else period
    )
    # Fall back if format fails.
    try:
        period_text = _g("period", "{period}").format(period=period)
    except (KeyError, ValueError, IndexError):
        period_text = period

    tasks = _task_count(report, summary)
    tokens_in = report.get("tokens_in", summary.get("tokens_in", 0))
    tokens_out = report.get("tokens_out", summary.get("tokens_out", 0))
    try:
        tokens_total = int(tokens_in or 0) + int(tokens_out or 0)
    except (TypeError, ValueError):
        tokens_total = 0
    cost = _money(report.get("spend_usd", summary.get("spend_usd")), unmeasured)
    saved_amount = _money(
        report.get("savings_usd", summary.get("savings_usd")), unmeasured
    )
    saved_text = _g("saved", "Saved ≈ {amount} (modelled)").format(amount=saved_amount)

    gap = max(1, width - len(title) - len(period_text))
    lines: list[str] = [title + (" " * gap) + period_text]
    task_label = _g("tasks", "Tasks")
    token_label = _g("tokens", "Tokens")
    cost_label = _g("cost", "Cost")
    lines.append(
        f"{task_label} {tasks}    {token_label} {_compact_tokens(tokens_total)}    "
        f"{cost_label} {cost}    {saved_text}"
    )
    lines.append("")
    model_h = "Model"
    task_h = _g("tasks", "Tasks")
    token_h = _g("tokens", "Tokens")
    cost_h = _g("cost", "Cost")
    lines.append(f"  {model_h:<16} {task_h:>6} {token_h:>8} {cost_h:>10}".rstrip())
    for row in report.get("by_model", summary.get("by_model", [])) or []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("model", "")).strip()
        row_tasks = row.get("runs", row.get("tasks", 0))
        try:
            row_tasks = int(row_tasks)
        except (TypeError, ValueError):
            row_tasks = 0
        row_tokens = int(row.get("tokens_in", 0) or 0) + int(
            row.get("tokens_out", 0) or 0
        )
        row_cost = _money(row.get("spend_usd", row.get("cost")), unmeasured)
        lines.append(
            f"  {name:<16} {row_tasks:>6} {_compact_tokens(row_tokens):>8} {row_cost:>10}".rstrip()
        )
    lines.append("")
    lines.append(_g("savings_line", "Savings are modelled, not billed."))
    return lines


class MeteringPage:
    @staticmethod
    def render(
        payload: Any = None,
        *,
        text: str | None = None,
        width: int = 140,
        raw: Any = None,
    ) -> list[str]:
        return render_metering_page(payload, text=text, width=width, raw=raw)

    @staticmethod
    def parse(text: str) -> Any:
        return _coerce(text)
