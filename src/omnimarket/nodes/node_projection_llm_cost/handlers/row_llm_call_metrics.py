"""Per-call llm_call_metrics row builder — single source of truth.

OMN-13001: the contract-owned runtime writer (handler_llm_cost.LlmCostProjectionRunner)
and the explicit backfill entrypoint (backfill_llm_call_metrics) both build rows
through ``build_llm_call_metrics_row`` so there is exactly one write authority for
``llm_call_metrics`` and one column set to keep in parity with the migration
(0001_create_llm_call_metrics.sql). The schema-parity test asserts
``LLM_CALL_METRICS_COLUMNS`` equals the migration column set.

Idempotency: ``input_hash`` is a deterministic dedup key; writers insert with
``ON CONFLICT (input_hash) DO NOTHING`` so replay never duplicates rows.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from omnimarket.projection.runner import safe_parse_date

# Ordered tuple of llm_call_metrics columns this node writes. Asserted equal to
# the migration's INSERT-able column set (everything except the DB-defaulted
# ``id``) by tests/test_schema_parity_projection_llm_cost.py.
LLM_CALL_METRICS_COLUMNS: tuple[str, ...] = (
    "correlation_id",
    "session_id",
    "run_id",
    "model_id",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "latency_ms",
    "usage_source",
    "usage_is_estimated",
    "usage_raw",
    "input_hash",
    "source",
    "code_version",
    "contract_version",
    "created_at",
)

# DB enum usage_source_type accepts only these values. The upstream event uses
# MEASURED (token usage measured via the provider's usage response) which maps to
# the DB's API provenance; UNKNOWN/absent maps to MISSING.
_USAGE_SOURCE_MAP: dict[str, str] = {
    "MEASURED": "API",
    "API": "API",
    "ESTIMATED": "ESTIMATED",
    "MISSING": "MISSING",
    "UNKNOWN": "MISSING",
}


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def event_has_projectable_fields(data: dict[str, Any]) -> bool:
    """Return True if the event carries enough to be worth projecting."""
    has_model = bool(data.get("model_name") or data.get("model_id"))
    has_tokens = any(
        data.get(field) is not None
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    return has_model and has_tokens


def _compute_input_hash(
    reporting_source: str | None,
    session_id: str | None,
    model_id: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> str:
    """Deterministic dedup key matching the contract spec."""
    key = (
        f"{reporting_source or ''}:{session_id or ''}:{model_id}:"
        f"{prompt_tokens or 0}:{completion_tokens or 0}"
    )
    return hashlib.sha256(key.encode()).hexdigest()


def build_llm_call_metrics_row(data: dict[str, Any]) -> dict[str, Any]:
    """Map an inbound llm-call-completed event to an llm_call_metrics row.

    The returned dict has exactly the keys in ``LLM_CALL_METRICS_COLUMNS``.
    ``usage_source`` is honest: MEASURED→API, anything else→ESTIMATED/MISSING,
    with ``usage_is_estimated`` derived from it.
    """
    model_id = str(data.get("model_id") or data.get("model_name") or "unknown")
    session_id = data.get("session_id") or data.get("sessionId")
    run_id = data.get("run_id") or data.get("runId")
    reporting_source = data.get("reporting_source") or data.get("source")

    correlation_id_raw = (
        data.get("correlation_id") or data.get("_correlation_id") or data.get("call_id")
    )
    correlation_id: str | None = None
    if correlation_id_raw:
        try:
            correlation_id = str(uuid.UUID(str(correlation_id_raw)))
        except ValueError:
            correlation_id = None

    prompt_tokens = _safe_int(
        data.get("prompt_tokens")
        or data.get("input_tokens")
        or data.get("promptTokens")
    )
    completion_tokens = _safe_int(
        data.get("completion_tokens")
        or data.get("output_tokens")
        or data.get("completionTokens")
    )
    total_tokens = _safe_int(
        data.get("total_tokens")
        or data.get("totalTokens")
        or (prompt_tokens + completion_tokens)
    )
    estimated_cost_usd = _safe_float(
        data.get("estimated_cost_usd")
        or data.get("estimatedCostUsd")
        or data.get("cost_usd")
    )
    latency_ms_raw = data.get("latency_ms") or data.get("latencyMs")
    latency_ms: float | None = (
        _safe_float(latency_ms_raw) if latency_ms_raw is not None else None
    )

    usage_source_raw = str(
        data.get("usage_source") or data.get("usageSource") or "MISSING"
    ).upper()
    usage_source = _USAGE_SOURCE_MAP.get(usage_source_raw, "MISSING")
    usage_is_estimated = usage_source != "API"

    input_hash = _compute_input_hash(
        reporting_source=str(reporting_source) if reporting_source else None,
        session_id=str(session_id) if session_id else None,
        model_id=model_id,
        prompt_tokens=prompt_tokens or None,
        completion_tokens=completion_tokens or None,
    )

    timestamp_raw = (
        data.get("emitted_at")
        or data.get("timestamp")
        or data.get("timestamp_iso")
        or data.get("created_at")
        or data.get("createdAt")
    )
    created_at = safe_parse_date(timestamp_raw)

    return {
        "correlation_id": correlation_id,
        "session_id": str(session_id)[:255] if session_id else None,
        "run_id": str(run_id)[:255] if run_id else None,
        "model_id": model_id[:255],
        "prompt_tokens": prompt_tokens or None,
        "completion_tokens": completion_tokens or None,
        "total_tokens": total_tokens or None,
        "estimated_cost_usd": estimated_cost_usd if estimated_cost_usd else None,
        "latency_ms": latency_ms,
        "usage_source": usage_source,
        "usage_is_estimated": usage_is_estimated,
        "usage_raw": json.dumps(data),
        "input_hash": input_hash,
        "source": str(reporting_source)[:255] if reporting_source else None,
        # code_version / contract_version are not in the event payload — NULL.
        "code_version": None,
        "contract_version": None,
        "created_at": created_at,
    }


__all__ = [
    "LLM_CALL_METRICS_COLUMNS",
    "build_llm_call_metrics_row",
    "event_has_projectable_fields",
]
