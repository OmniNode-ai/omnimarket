# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reducer for receipt-gate projection.

Consumes ``onex.evt.omnimarket.verification-receipt-completed.v1`` and
``onex.evt.omnimarket.evidence-validated.v1`` events and projects them into
``ModelReceiptGateRow`` snapshots for the omnidash receipt-gate widget.

Design constraints:
- Pure function: (rows, event) -> rows  (no I/O)
- Keeps at most 100 rows (most-recent first)
- Handles two event shapes:
  1. ``verification-receipt-completed.v1`` — one row per check dimension
  2. ``evidence-validated.v1`` — one row representing the OCC validation outcome
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_projection_receipt_gate.models.model_receipt_gate_row import (
    ModelReceiptGateRow,
)

_MAX_ROWS = 100


def reduce_receipt_gate(
    rows: tuple[ModelReceiptGateRow, ...],
    event: dict[str, object],
) -> tuple[ModelReceiptGateRow, ...]:
    """Apply a receipt-gate event to the row snapshot.

    Args:
        rows: Current projection state (newest-first, capped at ``_MAX_ROWS``).
        event: Raw event dict; either a verification-receipt or evidence-validated
               shape (distinguished by the ``_event_type`` or ``event_type`` hint,
               or by structural field presence).

    Returns:
        Updated projection state with new row(s) prepended, capped at
        ``_MAX_ROWS``.
    """
    new_rows = _rows_from_event(event)
    return (*new_rows, *rows)[:_MAX_ROWS]


# ── Event shape dispatch ─────────────────────────────────────────────────────


def _rows_from_event(event: dict[str, object]) -> list[ModelReceiptGateRow]:
    """Extract one or more projection rows from a raw event dict."""
    event_type = str(
        event.get("_event_type") or event.get("event_type") or event.get("type") or ""
    )

    # Evidence-validated events from node_occ_evidence_validator_compute.
    if "evidence-validated" in event_type or "evidence_lifecycle_state" in event:
        return [_row_from_evidence_validated(event)]

    # Verification-receipt-completed events produce one row per check dimension.
    # Detect by: explicit event_type hint, presence of "checks" list, or presence
    # of canonical receipt fields (overall_pass + verified_at / task_id).
    _is_receipt = (
        "checks" in event
        or "verification-receipt" in event_type
        or (
            "overall_pass" in event
            and ("verified_at" in event or "task_id" in event or "pr_number" in event)
        )
    )
    if _is_receipt:
        return _rows_from_verification_receipt(event)

    # Unknown shape — emit a single best-effort row.
    return [_best_effort_row(event)]


def _rows_from_verification_receipt(
    event: dict[str, object],
) -> list[ModelReceiptGateRow]:
    """Map a verification-receipt-completed event to per-check rows."""
    task_id = _str(event.get("task_id"))
    pr_number = _optional_int(event.get("pr_number"))
    repo = _str(event.get("repo"))
    verifier = _str(event.get("verifier")) or None
    verified_at = _observed_at(event, key="verified_at")

    pr_ref: str | None = None
    if task_id and pr_number is not None:
        pr_ref = f"{task_id} / #{pr_number}"
    elif pr_number is not None:
        pr_ref = f"#{pr_number}"
    elif task_id:
        pr_ref = task_id

    raw_checks = _as_sequence(event.get("checks"))
    if not raw_checks:
        # No checks — emit a single summary row from overall_pass.
        return [
            ModelReceiptGateRow(
                name="overall",
                **{"pass": _bool(event.get("overall_pass"))},
                detail=_str(event.get("claim")) or repo or task_id,
                pr_ref=pr_ref,
                worker=None,
                verifier=verifier,
                evidence_count=None,
                evidence_hash=None,
                signed_at=verified_at.isoformat(),
                observed_at=verified_at,
            )
        ]

    rows: list[ModelReceiptGateRow] = []
    for check in raw_checks:
        d = _as_dict(check)
        rows.append(
            ModelReceiptGateRow(
                name=_str(d.get("dimension")) or "check",
                **{"pass": _bool(d.get("passed"))},
                detail=_str(d.get("summary")),
                pr_ref=pr_ref,
                worker=None,
                verifier=verifier,
                evidence_count=None,
                evidence_hash=None,
                signed_at=verified_at.isoformat(),
                observed_at=verified_at,
            )
        )
    return rows


def _row_from_evidence_validated(event: dict[str, object]) -> ModelReceiptGateRow:
    """Map an evidence-validated event to a single projection row."""
    ticket_id = _str(event.get("ticket_id"))
    pr_number = _optional_int(event.get("pr_number"))
    validation_state = _str(event.get("validation_state") or event.get("state") or "")
    passed = validation_state.upper() == "PASSED" or _bool(event.get("passed"))

    pr_ref: str | None = None
    if ticket_id and pr_number is not None:
        pr_ref = f"{ticket_id} / #{pr_number}"
    elif ticket_id:
        pr_ref = ticket_id

    evidence_hash = _optional_str(
        event.get("evidence_bundle_hash") or event.get("draft_hash")
    )
    validated_at = _observed_at(event, key="validated_at")

    return ModelReceiptGateRow(
        name="occ-evidence",
        **{"pass": passed},
        detail=f"validation_state={validation_state}"
        if validation_state
        else "OCC evidence gate",
        pr_ref=pr_ref,
        worker=_optional_str(event.get("model_identity") or event.get("worker")),
        verifier=None,
        evidence_count=None,
        evidence_hash=evidence_hash,
        signed_at=validated_at.isoformat(),
        observed_at=validated_at,
    )


def _best_effort_row(event: dict[str, object]) -> ModelReceiptGateRow:
    """Fallback row for unrecognised event shapes."""
    observed = _observed_at(event, key="timestamp")
    return ModelReceiptGateRow(
        name=_str(event.get("name") or event.get("event_type") or "unknown"),
        **{"pass": _bool(event.get("pass") or event.get("passed") or event.get("ok"))},
        detail=_str(event.get("detail") or event.get("reason") or ""),
        pr_ref=None,
        worker=None,
        verifier=None,
        evidence_count=None,
        evidence_hash=None,
        signed_at=None,
        observed_at=observed,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "pass", "passed"}
    return False


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _observed_at(event: dict[str, object], key: str = "timestamp") -> datetime:
    raw = event.get(key) or event.get("observed_at") or event.get("timestamp")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        value = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


def _as_sequence(value: object) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if hasattr(value, "model_dump"):
        model_dump = value.model_dump
        raw = model_dump(mode="json")
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items()}
    return {}


__all__ = ["reduce_receipt_gate"]
