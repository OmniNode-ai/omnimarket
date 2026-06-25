# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cost-by-repo snapshot row builder — single source of truth.

OMN-13077: node_projection_cost_by_repo gained a projection_api over
``cost_by_repo_snapshots`` but had NO writer — the handler returned a pure dict
and nothing was persisted, so the table stayed empty. This module is the single
write authority that maps a LIVE ``onex.evt.omnibase-infra.delegation-completed.v1``
event (the topic carrying real metered cost) to one ``cost_by_repo_snapshots``
row, keyed on the canonical ``(repo_name, window, snapshot_timestamp_minute)``
dimension declared as the migration's unique constraint.

REPO DIMENSION (Wave-5 upstream gap)
    The delegation terminal payload may carry a top-level ``repo`` (the
    DelegationProjectionRunner already maps ``data.get("repo")`` into
    delegation_events.repo). In practice Wave-5 found delegation_events.repo
    empty — the upstream emitter does not populate it today. When ``repo`` is
    absent we key the row on the honest ``UNKNOWN_REPO`` sentinel; we never
    fabricate a repo value. Removing the sentinel requires the upstream emitter
    to attach the originating repo to the delegation terminal event (precise
    follow-up reported in the PR body).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omnimarket.projection.runner import safe_parse_date

# Honest sentinel for the absent-repo case. NOT a fabricated repo name.
UNKNOWN_REPO = "unknown"

# Ordered tuple of cost_by_repo_snapshots columns this node writes. Matches the
# INSERT-able column set of 0001_create_cost_by_repo_snapshots.sql (everything
# except the DB-defaulted id / created_at / updated_at).
COST_BY_REPO_SNAPSHOTS_COLUMNS: tuple[str, ...] = (
    "repo_name",
    "window",
    "snapshot_timestamp_minute",
    "total_cost_usd",
    "total_tokens",
)

# Unique constraint columns from the migration:
#   uq_cost_by_repo_repo_window_minute (repo_name, "window", snapshot_timestamp_minute)
COST_BY_REPO_CONFLICT_COLUMNS: tuple[str, ...] = (
    "repo_name",
    "window",
    "snapshot_timestamp_minute",
)


def _terminal_result_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap the canonical delegation terminal envelope to its result payload.

    Mirrors node_projection_delegation._canonical_terminal_result_payload: the
    metered cost / usage live under ``payload`` (and sometimes a doubly-nested
    ``payload.payload``).
    """
    payload = data.get("payload")
    if isinstance(payload, Mapping):
        nested = payload.get("payload")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(payload)
    return dict(data)


def _first_present(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _first_mapping(data: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _decimal_str(value: Any) -> str:
    if value is None:
        return "0"
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return "0"
    if dec < 0:
        # Non-negative constraint on the table; clamp defensively-free at the
        # write boundary by failing the value back to 0 (negative cost is a
        # malformed upstream signal, not a row we want to drop the batch on).
        return "0"
    return str(dec)


def _repo_name(data: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Resolve the repo dimension, honestly defaulting to UNKNOWN_REPO.

    Wave-5: the delegation terminal does not populate repo today. We read the
    canonical aliases if/when the upstream emitter starts attaching them; until
    then every row keys on UNKNOWN_REPO.
    """
    raw = _first_present(
        data, "repo", "repo_name", "repoName", "repository"
    ) or _first_present(result, "repo", "repo_name", "repoName", "repository")
    if raw is None:
        return UNKNOWN_REPO
    text = str(raw).strip()
    return text or UNKNOWN_REPO


def _snapshot_timestamp_minute(
    data: Mapping[str, Any], result: Mapping[str, Any]
) -> datetime:
    raw = _first_present(
        data,
        "snapshot_timestamp_minute",
        "snapshotTimestampMinute",
        "timestamp",
        "emitted_at",
        "event_timestamp",
        "timestamp_iso",
    ) or _first_present(result, "timestamp", "emitted_at", "timestamp_iso")
    dt = safe_parse_date(raw) if raw is not None else datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


def build_cost_by_repo_row(data: Mapping[str, Any]) -> dict[str, Any]:
    """Map a delegation-completed event to a cost_by_repo_snapshots row.

    The returned dict has exactly the keys in COST_BY_REPO_SNAPSHOTS_COLUMNS.
    Metered cost is read from cost_usd / final_attempt_cost; tokens are summed
    from tokens_input + tokens_output (canonical delegation terminal aliases),
    falling back to the nested ``usage`` mapping.
    """
    result = _terminal_result_payload(data)
    usage = _first_mapping(result, "usage", "metrics", "token_usage", "tokenUsage")

    cost = _first_present(
        data, "cost_usd", "costUsd", "estimated_cost_usd", "estimatedCostUsd"
    ) or _first_present(
        result, "cost_usd", "costUsd", "final_attempt_cost", "estimated_cost_usd"
    )

    tokens_input = _safe_int(
        _first_present(data, "tokens_input", "tokensInput", "prompt_tokens")
        or _first_present(
            result, "tokens_input", "prompt_tokens", "promptTokens", "input_tokens"
        )
        or _first_present(usage, "prompt_tokens", "promptTokens", "input_tokens")
    )
    tokens_output = _safe_int(
        _first_present(data, "tokens_output", "tokensOutput", "completion_tokens")
        or _first_present(
            result,
            "tokens_output",
            "completion_tokens",
            "completionTokens",
            "output_tokens",
        )
        or _first_present(
            usage, "completion_tokens", "completionTokens", "output_tokens"
        )
    )
    total_tokens = _safe_int(
        _first_present(data, "total_tokens", "totalTokens")
        or _first_present(result, "total_tokens", "totalTokens")
        or (tokens_input + tokens_output)
    )

    window = str(
        _first_present(data, "window") or _first_present(result, "window") or "latest"
    )

    return {
        "repo_name": _repo_name(data, result),
        "window": window,
        "snapshot_timestamp_minute": _snapshot_timestamp_minute(data, result),
        "total_cost_usd": _decimal_str(cost),
        "total_tokens": total_tokens,
    }


__all__ = [
    "COST_BY_REPO_CONFLICT_COLUMNS",
    "COST_BY_REPO_SNAPSHOTS_COLUMNS",
    "UNKNOWN_REPO",
    "build_cost_by_repo_row",
]
