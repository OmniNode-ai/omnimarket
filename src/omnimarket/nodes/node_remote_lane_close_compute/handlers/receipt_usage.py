# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reduce native engine events without emitting text, inputs, or tool outputs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..models.model_remote_lane_receipt_usage import (
    ModelRemoteLaneReceiptUsage,
    UsageCount,
)


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: object) -> UsageCount:
    return value if type(value) is int and value >= 0 else "UNKNOWN"


def _number(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def brief_identity(brief: str) -> dict[str, str]:
    """Read unique, unfenced identity lines; refuse ambiguous or unsafe ledger cells."""
    cells: dict[str, str] = {}
    fence: str | None = None
    for raw in brief.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        for label, key in (("Contract:", "contract_id"), ("Attempt:", "attempt_id")):
            if line.startswith(label):
                value = line[len(label) :].strip()
                if key in cells or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value
                ):
                    raise ValueError(f"ambiguous or invalid {label} line")
                cells[key] = value
    return cells


@dataclass
class _Counters:
    """Started/completed IDs count once; only failure followed by the same call retries."""

    turns: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    identities: dict[str, str] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    retries: int = 0
    observed: bool = False
    retry_identity_missing: bool = False

    def call(self, call_id: str, identity: object | None) -> None:
        self.observed = True
        if call_id in self.calls:
            return
        self.calls.add(call_id)
        if identity is None:
            self.retry_identity_missing = True
            return
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        self.identities[call_id] = fingerprint
        if fingerprint in self.failed:
            self.retries += 1
            self.failed.remove(fingerprint)

    def complete(self, call_id: str, failed: bool) -> None:
        identity = self.identities.get(call_id)
        if identity is not None:
            if failed:
                self.failed.add(identity)
            else:
                self.failed.discard(identity)


def _claude_event(event: dict[str, Any], index: int, counters: _Counters) -> None:
    message = _object(event.get("message"))
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    if event.get("type") == "assistant":
        counters.observed = True
        counters.turns.add(str(message.get("id") or f"assistant-{index}"))
        for block in blocks:
            block = _object(block)
            if block.get("type") == "tool_use":
                identity = (
                    (block.get("name"), block["input"]) if "input" in block else None
                )
                counters.call(
                    str(block.get("id") or f"tool-{index}-{len(counters.calls)}"),
                    identity,
                )
    elif event.get("type") == "user":
        for block in blocks:
            block = _object(block)
            if block.get("type") == "tool_result":
                counters.complete(
                    str(block.get("tool_use_id")), block.get("is_error") is True
                )


def _codex_event(event: dict[str, Any], index: int, counters: _Counters) -> None:
    kind = event.get("type")
    if kind == "thread.started":
        counters.observed = True
    if kind not in ("item.started", "item.completed"):
        return
    item = _object(event.get("item"))
    item_type = item.get("type")
    item_id = str(item.get("id") or f"item-{index}")
    if item_type == "agent_message" and kind == "item.completed":
        counters.observed = True
        counters.turns.add(item_id)
    elif item_type in (
        "command_execution",
        "mcp_tool_call",
        "web_search",
        "file_change",
    ):
        identity: object | None
        if item_type == "command_execution":
            identity = (item_type, item["command"]) if "command" in item else None
        elif item_type == "mcp_tool_call":
            identity = (
                (item_type, item.get("server"), item.get("tool"), item["arguments"])
                if "arguments" in item
                else None
            )
        elif item_type == "web_search":
            identity = (item_type, item["query"]) if "query" in item else None
        else:
            identity = (item_type, item["changes"]) if "changes" in item else None
        counters.call(item_id, identity)
        if kind == "item.completed":
            code = item.get("exit_code")
            failed = (
                item.get("status") == "failed"
                or item.get("error") is not None
                or (type(code) is int and code != 0)
            )
            counters.complete(item_id, failed)


def _sum_usage(usages: list[dict[str, Any]], key: str) -> UsageCount:
    values = [_count(usage.get(key)) for usage in usages]
    if not values or any(value == "UNKNOWN" for value in values):
        return "UNKNOWN"
    return sum(value for value in values if isinstance(value, int))


def receipt_usage(
    engine: str,
    events: tuple[dict[str, Any], ...] | None,
    brief: str,
    wall_seconds: float | None,
) -> ModelRemoteLaneReceiptUsage:
    """Use Claude's aggregate result or sum Codex per-turn usage, never defaulting to 0.

    The caller supplies the complete native JSON event sequence for this attempt.
    A summary-only result can supply reported counters, but cannot prove retries.
    Configured model names and legacy receipt cost defaults are not measurements.
    """
    identity = brief_identity(brief)
    values: dict[str, Any] = {"engine": engine, **identity}
    if wall_seconds is not None:
        values["wall_seconds"] = wall_seconds
    claude = engine == "claude" or engine.startswith("claude_")
    codex = engine == "codex"
    if not (claude or codex) or events is None:
        return ModelRemoteLaneReceiptUsage.model_validate(values)
    counters = _Counters()
    usages: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    models: set[str] = set()
    for index, event in enumerate(events):
        if claude:
            _claude_event(event, index, counters)
            message = _object(event.get("message"))
            model = (
                message.get("model")
                if event.get("type") == "assistant"
                else event.get("model")
            )
            if isinstance(model, str) and model:
                models.add(model)
            if event.get("type") == "result":
                result = event
                usages = [_object(event.get("usage"))]
        else:
            _codex_event(event, index, counters)
            if event.get("type") == "turn.completed":
                usages.append(_object(event.get("usage")))
            model = event.get("model")
            if isinstance(model, str) and model:
                models.add(model)
    if claude and not models:
        models.update(_object(result.get("modelUsage")))
    if len(models) == 1:
        values["model"] = next(iter(models))
    keys = {
        "tokens_in": "input_tokens",
        "tokens_out": "output_tokens",
        "cache_write": "cache_creation_input_tokens"
        if claude
        else "cache_write_input_tokens",
        "cache_read": "cache_read_input_tokens" if claude else "cached_input_tokens",
    }
    for target, source in keys.items():
        values[target] = _sum_usage(usages, source)
    values["tokens_cached"] = values["cache_read"]
    values["tool_calls"] = _sum_usage(usages, "tool_calls")
    if counters.observed:
        values["turns"] = len(counters.turns)
        if values["tool_calls"] == "UNKNOWN":
            values["tool_calls"] = len(counters.calls)
        values["retries"] = (
            "UNKNOWN" if counters.retry_identity_missing else counters.retries
        )
    elif claude:
        values["turns"] = _count(result.get("num_turns"))
    if claude:
        cost = _number(result.get("total_cost_usd"))
        if cost is not None:
            values.update(total_cost_usd=cost, cost_basis="MEASURED")
        duration = _number(result.get("duration_ms"))
        if wall_seconds is None and duration is not None:
            values["wall_seconds"] = duration / 1000
    return ModelRemoteLaneReceiptUsage.model_validate(values)
