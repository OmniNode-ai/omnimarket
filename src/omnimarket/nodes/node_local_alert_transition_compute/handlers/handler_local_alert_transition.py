# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alert transition compute: environment rows around a write in, transitions out.

``derive_local_alert_transitions`` compares a tenant's local_environment rows
before and after one write. A row whose stored alert appears raises it; a row
whose stored alert goes away, or is replaced by another code, clears it. Only a
write can clear an alert, so an alert clears only when a later report meets the
clear condition (its verdict no longer opens the code); time passing does not.

``plan_local_alert_fold`` is the whole fold decision, pure. Each row is a
function of the newest transition for its key, ordered by (occurred_at,
transition_id): a replayed transition (same id) or an older one writes nothing,
so the same transitions in any order, any number of times, give the same rows.
Rows carry no wall-clock column, so a replay into a fresh store gives the same
bytes. A writer reads the stored row, plans, and UPSERTs the plan on
``LOCAL_ALERTS_KEY``; this module does no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_alert_transition import EnumLocalAlertTransition
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict
from omnimarket.models.model_local_alert_transitioned import (
    ModelLocalAlertEvidence,
    ModelLocalAlertTransitioned,
    local_alert_subject,
)
from omnimarket.nodes.node_local_alert_transition_compute.models import (
    ModelLocalAlertTransitionRequest,
    ModelLocalAlertTransitionResult,
)

LOCAL_ALERTS_TABLE = "local_alerts"
LOCAL_ALERTS_KEY = "tenant_id,code,subject"


def _as_datetime(value: object) -> datetime:
    """A stored time: an ISO string from SQLite, a datetime from Postgres."""
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _key(row: Mapping[str, object]) -> tuple[str, str, str]:
    return (str(row["tenant_id"]), str(row["mode"]), str(row["service"]))


def _evidence(row: Mapping[str, object]) -> ModelLocalAlertEvidence:
    report_id = row.get("report_id")
    return ModelLocalAlertEvidence(
        mode=EnumLocalEnvironmentMode(str(row["mode"])),
        service=str(row["service"]),
        verdict=EnumLocalServiceVerdict(str(row["verdict"])),
        reason=str(row["verdict_reason"]),
        last_seen=_as_datetime(row["last_seen"]),
        report_id=None if report_id is None else str(report_id),
    )


def _transition(
    row: Mapping[str, object],
    *,
    code: str,
    severity: object,
    transition: EnumLocalAlertTransition,
    raised_at: datetime,
    occurred_at: datetime,
) -> ModelLocalAlertTransitioned:
    return ModelLocalAlertTransitioned(
        tenant_id=str(row["tenant_id"]),
        code=code,
        subject=local_alert_subject(
            EnumLocalEnvironmentMode(str(row["mode"])), str(row["service"])
        ),
        transition=transition,
        severity=EnumLocalAlertSeverity(str(severity)),
        raised_at=raised_at,
        occurred_at=occurred_at,
        evidence=_evidence(row),
    )


def derive_local_alert_transitions(
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
) -> tuple[ModelLocalAlertTransitioned, ...]:
    """The raises and clears one write made, from the environment rows around it.

    Rows are matched on (tenant, mode, service); a row missing from ``after`` is
    not judged. Clears come before raises for the same row, in ``after`` order.
    """
    previous = {_key(row): row for row in before}
    transitions: list[ModelLocalAlertTransitioned] = []
    for row in after:
        old = previous.get(_key(row))
        old_code = None if old is None else old.get("alert_code")
        new_code = row.get("alert_code")
        if old_code == new_code:
            continue
        new_since = (
            None if row.get("alert_since") is None else _as_datetime(row["alert_since"])
        )
        if old is not None and old_code is not None:
            raised_at = _as_datetime(old["alert_since"])
            # A replacing alert clears the old one when it starts; a plain clear
            # happens at the report that met the clear condition.
            occurred_at = (
                new_since if new_since is not None else _as_datetime(row["last_seen"])
            )
            transitions.append(
                _transition(
                    row,
                    code=str(old_code),
                    severity=old["alert_severity"],
                    transition=EnumLocalAlertTransition.CLEARED,
                    raised_at=raised_at,
                    occurred_at=max(occurred_at, raised_at),
                )
            )
        if new_code is not None and new_since is not None:
            transitions.append(
                _transition(
                    row,
                    code=str(new_code),
                    severity=row["alert_severity"],
                    transition=EnumLocalAlertTransition.RAISED,
                    raised_at=new_since,
                    occurred_at=new_since,
                )
            )
    return tuple(transitions)


def plan_local_alert_fold(
    transition: ModelLocalAlertTransitioned,
    stored: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """The row to UPSERT for a transition, or None when it is a replay or older."""
    if stored is not None:
        stored_order = (
            _as_datetime(stored["last_transition_at"]),
            str(stored["last_transition_id"]),
        )
        if (transition.occurred_at.astimezone(UTC), str(transition.transition_id)) <= (
            stored_order
        ):
            return None
    cleared = transition.transition is EnumLocalAlertTransition.CLEARED
    return {
        "tenant_id": transition.tenant_id,
        "code": transition.code,
        "subject": transition.subject,
        "mode": transition.evidence.mode.value,
        "service": transition.evidence.service,
        "state": transition.transition.value,
        "severity": transition.severity.value,
        "raised_at": _iso(transition.raised_at),
        "cleared_at": _iso(transition.occurred_at) if cleared else None,
        "evidence": json.dumps(
            transition.evidence.model_dump(mode="json"), sort_keys=True
        ),
        "last_transition_id": str(transition.transition_id),
        "last_transition_at": _iso(transition.occurred_at),
    }


class HandlerLocalAlertTransition:
    def handle(
        self, request: ModelLocalAlertTransitionRequest
    ) -> ModelLocalAlertTransitionResult:
        return ModelLocalAlertTransitionResult(
            transitions=derive_local_alert_transitions(request.before, request.after)
        )


__all__ = [
    "LOCAL_ALERTS_KEY",
    "LOCAL_ALERTS_TABLE",
    "HandlerLocalAlertTransition",
    "derive_local_alert_transitions",
    "plan_local_alert_fold",
]
