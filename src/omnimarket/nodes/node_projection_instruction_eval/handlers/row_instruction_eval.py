# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Instruction-eval snapshot row builder — single source of truth.

OMN-12998: maps an onex.evt.omnimarket.instruction-eval-result.v1 event
(emitted by the instruction-eval runner / scorer lineage) to one
instruction_eval_aggregate_snapshots row keyed on the canonical
(model, task, context_mode) unique constraint declared in the migration.

Honesty invariant: pass_rate is stored as None when the field is absent from
the event, never as a fake zero. Callers (dashboard panel cells) must render
an em-dash for None rather than "0%".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Ordered tuple of instruction_eval_aggregate_snapshots columns this node writes.
# Matches the INSERT-able column set of
# 0001_create_instruction_eval_aggregate_snapshots.sql (excluding DB-defaulted
# id / created_at / updated_at).
INSTRUCTION_EVAL_COLUMNS: tuple[str, ...] = (
    "model",
    "task",
    "context_mode",
    "pass_rate",
    "output_tokens",
    "runs",
)

# Unique constraint columns from the migration:
#   uq_instruction_eval_aggregate_model_task_mode (model, task, context_mode)
INSTRUCTION_EVAL_CONFLICT_COLUMNS: tuple[str, ...] = (
    "model",
    "task",
    "context_mode",
)

# Allowed context_mode values (mirrors EvalContextMode in omnidash).
VALID_CONTEXT_MODES = frozenset({"baseline", "chunk", "full-claude-md"})


def _safe_str(value: Any, max_len: int = 256) -> str:
    """Return a non-empty string or raise ValueError."""
    if value is None:
        raise ValueError("required string field is missing")
    s = str(value).strip()
    if not s:
        raise ValueError("required string field is empty")
    return s[:max_len]


def _safe_optional_float(value: Any) -> float | None:
    """Return float in [0, 1] or None (never a fake zero)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None
    if f < 0.0 or f > 1.0:
        # Out-of-range pass_rate is a malformed upstream signal. Store None so
        # the panel renders an honest em-dash rather than a clamped/wrong value.
        return None
    return f


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return max(0, int(float(value)))
    except (ValueError, TypeError):
        return default


def build_instruction_eval_row(data: Mapping[str, Any]) -> dict[str, Any]:
    """Map an instruction-eval-result event to an aggregate snapshot row.

    The returned dict has exactly the keys in INSTRUCTION_EVAL_COLUMNS.

    Raises ValueError for missing required fields (model / task / context_mode).
    """
    model = _safe_str(data.get("model"), max_len=256)
    task = _safe_str(data.get("task"), max_len=256)
    context_mode = _safe_str(data.get("context_mode"), max_len=64)

    pass_rate = _safe_optional_float(data.get("pass_rate"))
    output_tokens = _safe_int(data.get("output_tokens"))
    runs = _safe_int(data.get("runs"))

    return {
        "model": model,
        "task": task,
        "context_mode": context_mode,
        "pass_rate": pass_rate,
        "output_tokens": output_tokens,
        "runs": runs,
    }


__all__ = [
    "INSTRUCTION_EVAL_COLUMNS",
    "INSTRUCTION_EVAL_CONFLICT_COLUMNS",
    "VALID_CONTEXT_MODES",
    "build_instruction_eval_row",
]
