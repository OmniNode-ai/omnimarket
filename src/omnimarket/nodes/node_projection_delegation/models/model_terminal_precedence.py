# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure evidence precedence and terminal outcome consistency (OMN-19559)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

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

# The columns a terminal owns as one unit: who answered, what it scored, when it
# ended and what it concluded. A stored failure keeps all of them together when a
# later event is not allowed to replace it, so the row never states a failure
# next to another event's model, score or time.
_TERMINAL_OWNED_COLUMNS = (
    *_OUTCOME_COLUMNS,
    "actual_score",
    "timestamp",
    "routed_model",
    "answering_backend",
    "backend_id",
    "host",
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


def is_failure_terminal(row: Mapping[str, object]) -> bool:
    """A row or event that states a typed failure cause and does not claim success."""
    return (
        not _is_blank(row.get("terminal_failure_cause"))
        and _flag(row.get("terminal_ok")) is not True
    )


def _event_instant(value: object) -> datetime | None:
    """Read an event time as an aware instant, or None when it cannot be read.

    Adapters hand back a datetime or an ISO string. A naive value is taken as
    UTC, the only zone the projection writes.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_out_of_order(existing: Mapping[str, object], row: Mapping[str, object]) -> bool:
    """True when the incoming event is strictly older than the stored one.

    An unreadable or missing time on either side is never out of order, so the
    arrival order decides exactly as it did before times were consulted.
    """
    incoming = _event_instant(row.get("timestamp"))
    stored = _event_instant(existing.get("timestamp"))
    return incoming is not None and stored is not None and incoming < stored


def stored_terminal_owns_outcome(
    existing: Mapping[str, object], row: Mapping[str, object]
) -> bool:
    """Decide by each event's own terminal meaning, never by arrival order alone.

    A stored typed failure is a terminal. It is replaced as a whole by exactly
    two things: an accepted provider attempt over a handler-local failure
    (``supersedes_handler_failure``, either order), and a later failure terminal
    whose own time is not older than the stored one. Everything else is a
    non-terminal or out-of-order event for this row and replaces none of it: a
    late completion that proves no accepted attempt, an evidenced success after a
    provider-evidenced failure, a failure terminal that happened before the one
    stored.
    """
    if not is_failure_terminal(existing) or supersedes_handler_failure(existing, row):
        return False
    return not is_failure_terminal(row) or _is_out_of_order(existing, row)


def keep_stored_terminal(
    existing: Mapping[str, object], row: dict[str, object]
) -> None:
    """Drop every terminal-owned column from an event that may not replace them.

    The upsert leaves a column the row does not name untouched, so the stored
    terminal stays whole. A terminal that never stated an outcome or verdict
    (the delegate-skill failure names a cause only) gets the ones its cause maps
    to, so the row reads one failure rather than a cause beside a blank outcome.
    Metering and attribution columns are not terminal state and are merged by the
    writer's own rules.
    """
    for column in _TERMINAL_OWNED_COLUMNS:
        row.pop(column, None)
    outcome, verdict = outcome_for_failure_cause(
        str(existing.get("terminal_failure_cause"))
    )
    if _is_blank(existing.get("operational_outcome")):
        row["operational_outcome"] = outcome
    if _is_blank(existing.get("content_verdict")):
        row["content_verdict"] = verdict


def fold_terminal_ownership(
    existing: Mapping[str, object], row: dict[str, object]
) -> bool:
    """Make exactly one terminal own the outcome columns of ``row`` (OMN-17427).

    Returns whether the stored score may still fill a blank incoming one.

    * The stored failure owns the row: every terminal-owned column is dropped.
    * A failure terminal over a non-failure row owns the score too. A failed run's
      score is its own, so a stored completion's score never fills it and a
      failure that states none writes none rather than leaving the stored one.
    * A failure that replaces another failure states the outcome its own cause
      maps to, so the older cause's outcome does not outlive it.
    """
    if stored_terminal_owns_outcome(existing, row):
        keep_stored_terminal(existing, row)
        return True
    if not is_failure_terminal(row):
        return True
    if not is_failure_terminal(existing):
        row.setdefault("actual_score", None)
        return False
    if _is_blank(row.get("operational_outcome")):
        row["operational_outcome"], failure_verdict = outcome_for_failure_cause(
            str(row.get("terminal_failure_cause"))
        )
        if _is_blank(row.get("content_verdict")):
            row["content_verdict"] = failure_verdict
    return True


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
    "fold_terminal_ownership",
    "is_evidenced_success",
    "is_failure_terminal",
    "is_unevidenced_handler_failure",
    "keep_stored_terminal",
    "outcome_for_failure_cause",
    "stored_terminal_owns_outcome",
    "supersedes_handler_failure",
]
