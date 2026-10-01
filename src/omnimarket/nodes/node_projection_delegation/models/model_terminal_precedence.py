# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure evidence precedence and terminal outcome consistency (OMN-19559)."""

from __future__ import annotations

import json
from collections.abc import Mapping

from omnibase_core.enums.enum_delegation_content_verdict import (
    EnumDelegationContentVerdict,
)
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

HANDLER_LOCAL_FAILURE_CAUSES: frozenset[str] = frozenset(
    {
        EnumDelegationTerminalFailureCause.TIMEOUT.value,
        EnumDelegationTerminalFailureCause.RUNTIME_SHUTDOWN.value,
    }
)

_OUTCOME_COLUMNS = (
    "terminal_ok",
    "terminal_failure_cause",
    "quality_gate_passed",
    "model_name",
    "delegated_to",
    "operational_outcome",
    "content_verdict",
    "attempt_history",
    "escalation_count",
    "quality_gates_checked",
    "quality_gates_failed",
    "quality_gates_checked_jsonb",
    "quality_gates_failed_jsonb",
    "quality_gate_detail",
    "response_text",
)


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _flag(value: object) -> bool | None:
    """Read a stored boolean the way each adapter returns it.

    Postgres and the in-memory adapter return a bool; SQLite returns 1 or 0.
    Anything else is unknown, never coerced into a verdict.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str) and value.strip().lower() in {"t", "true", "f", "false"}:
        return value.strip().lower() in {"t", "true"}
    return None


def _attempt_history(value: object) -> list[object]:
    """Normalize an adapter's JSON text or list without inventing evidence."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def is_unevidenced_handler_failure(row: Mapping[str, object]) -> bool:
    """Recognize a handler's budget/cancellation failure with no provider evidence."""
    cause = row.get("terminal_failure_cause")
    return (
        isinstance(cause, str)
        and cause in HANDLER_LOCAL_FAILURE_CAUSES
        and not _attempt_history(row.get("attempt_history"))
        and _is_blank(row.get("model_name"))
    )


def is_evidenced_success(row: Mapping[str, object]) -> bool:
    """Require a successful terminal, a model, and an accepted attempt."""
    return (
        _flag(row.get("terminal_ok")) is True
        and not _is_blank(row.get("model_name"))
        and any(
            isinstance(attempt, Mapping)
            and _flag(attempt.get("quality_gate_passed")) is True
            and _is_blank(attempt.get("failure_class"))
            for attempt in _attempt_history(row.get("attempt_history"))
        )
    )


def outcome_for_failure_cause(cause: str) -> tuple[str, str]:
    """Map a terminal failure onto the core outcome and content vocabulary."""
    outcomes = {
        EnumDelegationTerminalFailureCause.TIMEOUT.value: (
            EnumDelegationOperationalOutcome.TIMEOUT,
            EnumDelegationContentVerdict.NOT_APPLICABLE,
        ),
        EnumDelegationTerminalFailureCause.RUNTIME_SHUTDOWN.value: (
            EnumDelegationOperationalOutcome.CANCELLED,
            EnumDelegationContentVerdict.NOT_APPLICABLE,
        ),
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED.value: (
            EnumDelegationOperationalOutcome.PROVIDER_QUOTA,
            EnumDelegationContentVerdict.NOT_APPLICABLE,
        ),
        EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED.value: (
            EnumDelegationOperationalOutcome.QUALITY_REJECTED,
            EnumDelegationContentVerdict.UNUSABLE,
        ),
    }
    outcome, verdict = outcomes.get(
        cause,
        (
            EnumDelegationOperationalOutcome.INFERENCE_FAILED,
            EnumDelegationContentVerdict.NOT_APPLICABLE,
        ),
    )
    return outcome.value, verdict.value


def apply_terminal_precedence(
    existing: Mapping[str, object], row: dict[str, object]
) -> None:
    """Keep evidenced success and reconcile a failed row's effective outcome."""
    if is_evidenced_success(existing) and is_unevidenced_handler_failure(row):
        for column in _OUTCOME_COLUMNS:
            row.pop(column, None)
        return

    terminal_ok = _flag(row.get("terminal_ok", existing.get("terminal_ok")))
    cause = row.get("terminal_failure_cause", existing.get("terminal_failure_cause"))
    outcome = row.get("operational_outcome", existing.get("operational_outcome"))
    verdict = row.get("content_verdict", existing.get("content_verdict"))
    if terminal_ok is False and not _is_blank(cause) and outcome == "completed":
        failure_outcome, failure_verdict = outcome_for_failure_cause(str(cause))
        row["operational_outcome"] = failure_outcome
        if verdict in ("usable", "correct"):
            row["content_verdict"] = failure_verdict


def supersedes_handler_failure(
    existing: Mapping[str, object], row: Mapping[str, object]
) -> bool:
    """An accepted provider attempt supersedes an unevidenced handler failure."""
    return is_unevidenced_handler_failure(existing) and is_evidenced_success(row)


__all__ = [
    "HANDLER_LOCAL_FAILURE_CAUSES",
    "apply_terminal_precedence",
    "is_evidenced_success",
    "is_unevidenced_handler_failure",
    "outcome_for_failure_cause",
    "supersedes_handler_failure",
]
