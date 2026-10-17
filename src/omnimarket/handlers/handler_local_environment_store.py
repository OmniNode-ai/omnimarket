# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral write of one environment report, and the verdicts it moves.

One row per (tenant, mode, service) holds that service's latest report. Every
stored report is also a heartbeat: every row of the tenant's mode is judged at
the report's time, so a service that has gone silent turns STALE in the store
when any other service reports. Each row stores its verdict and its open alert,
so a page shows them as served. When nothing reports at all (the observer or the
broker is gone), the exposure's served freshness turns stale instead: no write
can happen then, and the read path, not the browser, says so.

``plan_local_environment_write`` is the whole decision, pure, over the rows
already stored; the local SQLite path applies its plan here and the Postgres
writer applies the same plan in one transaction, so the two stores cannot judge
differently.

Every row of a mode is judged at the newest time that mode has seen, never
earlier, so a late report cannot revive a service the mode already holds as
silent past the deadline. The read, the plan and the writes run in one write
transaction when the store offers one, so two writers never interleave and a
failure part way leaves nothing behind for the replay guard to refuse.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.handlers.handler_local_environment_judgement import judge_local_services
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.models.model_local_service_health import (
    ModelLocalAlertRules,
    ModelLocalServiceHealth,
    ModelLocalServiceVerdict,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

LOCAL_ENVIRONMENT_TABLE = "local_environment"
LOCAL_ENVIRONMENT_KEY = "tenant_id,mode,service"
# The columns a re-judged sibling changes; the rest of its row is its own report.
VERDICT_COLUMNS = (
    "verdict",
    "verdict_reason",
    "alert_code",
    "alert_severity",
    "alert_since",
)
_TIME_COLUMNS = frozenset(
    {"last_seen", "last_change", "alert_since", "token_minted_at"}
)


def _as_datetime(value: object) -> datetime:
    """A stored time: an ISO string from SQLite, a datetime from Postgres."""
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _comparable(name: str, value: object) -> object:
    if value is None:
        return None
    return _iso(_as_datetime(value)) if name in _TIME_COLUMNS else value


def _verdict_columns(verdict: ModelLocalServiceVerdict) -> dict[str, object]:
    return {
        "verdict": verdict.verdict.value,
        "verdict_reason": verdict.reason,
        "alert_code": verdict.alert_code,
        "alert_severity": (
            None if verdict.alert_severity is None else verdict.alert_severity.value
        ),
        "alert_since": None
        if verdict.alert_since is None
        else _iso(verdict.alert_since),
    }


def _health(row: Mapping[str, object]) -> ModelLocalServiceHealth:
    return ModelLocalServiceHealth(
        tenant_id=str(row["tenant_id"]),
        mode=EnumLocalEnvironmentMode(str(row["mode"])),
        service=str(row["service"]),
        observed_state=EnumLocalServiceState(str(row["observed_state"])),
        reason=str(row["reason"]),
        last_seen=_as_datetime(row["last_seen"]),
        last_change=_as_datetime(row["last_change"]),
    )


def _stored_versions(value: object) -> dict[str, str]:
    if isinstance(value, str):
        loaded: dict[str, str] = json.loads(value)
        return loaded
    return dict(value) if isinstance(value, Mapping) else {}


def plan_local_environment_write(
    report: ModelLocalEnvironmentStatus,
    stored_rows: Sequence[Mapping[str, object]],
    rules: ModelLocalAlertRules,
    updated_at: datetime,
) -> list[dict[str, object]]:
    """The rows to write for a report, given every stored row of its tenant's mode.

    Empty when the report is a replay (its id is the stored one) or late (no
    newer than the stored one): then nothing is written, so no row, time or
    cursor moves. Otherwise the report's own row comes first, followed by each
    sibling whose verdict or alert the report's time changes, in full with only
    its verdict columns and updated_at replaced. Times are ISO strings.
    """
    # Only the report's own tenant and mode are judged: a caller's wider read
    # never leaks another tenant's rows into this write.
    stored_rows = [
        row
        for row in stored_rows
        if str(row["tenant_id"]) == report.tenant_id
        and str(row["mode"]) == report.mode.value
    ]
    previous = next(
        (row for row in stored_rows if str(row["service"]) == report.service), None
    )
    last_change = report.observed_at
    if previous is not None:
        if str(previous["report_id"]) == str(report.report_id):
            return []
        if _as_datetime(previous["last_seen"]) >= report.observed_at:
            return []
        if str(previous["observed_state"]) == report.observed_state.value:
            last_change = _as_datetime(previous["last_change"])

    siblings = [row for row in stored_rows if str(row["service"]) != report.service]
    # The mode's clock only moves forward: a late report is judged at the
    # newest time any row of the mode has been seen, never at its own.
    evaluated_at = max(
        [report.observed_at, *(_as_datetime(row["last_seen"]) for row in siblings)]
    )
    reported = ModelLocalServiceHealth(
        tenant_id=report.tenant_id,
        mode=report.mode,
        service=report.service,
        observed_state=report.observed_state,
        reason=report.reason,
        last_seen=report.observed_at,
        last_change=last_change,
    )
    verdicts, _ = judge_local_services(
        (reported, *(_health(row) for row in siblings)), evaluated_at, rules
    )
    stamp = _iso(updated_at)
    writes: list[dict[str, object]] = [
        {
            "tenant_id": report.tenant_id,
            "mode": report.mode.value,
            "service": report.service,
            "observed_state": report.observed_state.value,
            "reason": report.reason,
            "versions": dict(report.versions),
            "last_seen": _iso(report.observed_at),
            "last_change": _iso(last_change),
            **_verdict_columns(verdicts[0]),
            "store_kind": report.store_kind,
            "store_location": report.store_location,
            "access_mode": report.access_mode,
            "token_minted_at": (
                None if report.token_minted_at is None else _iso(report.token_minted_at)
            ),
            "rotate_hint": report.rotate_hint,
            "report_id": str(report.report_id),
            "updated_at": stamp,
        }
    ]
    for row, verdict in zip(siblings, verdicts[1:], strict=True):
        columns = _verdict_columns(verdict)
        if all(
            _comparable(name, row.get(name)) == value for name, value in columns.items()
        ):
            continue
        kept = {
            name: _comparable(name, value)
            for name, value in row.items()
            if name not in {"projection_cursor", "updated_at"}
        }
        kept["versions"] = _stored_versions(row.get("versions"))
        writes.append({**kept, **columns, "updated_at": stamp})
    return writes


def apply_local_environment_status(
    report: ModelLocalEnvironmentStatus,
    db: DatabaseAdapter,
    rules: ModelLocalAlertRules,
) -> bool:
    """Store a report through the portable adapter; a replay or a late report writes nothing.

    With a store that offers ``write_transaction`` (the local SQLite store),
    the read and every write are one transaction under its write lock.
    """
    transaction = getattr(db, "write_transaction", None)
    if transaction is None:
        return _apply(report, db, rules)
    with transaction() as tx:
        return _apply(report, tx, rules)


def _apply(
    report: ModelLocalEnvironmentStatus,
    db: DatabaseAdapter,
    rules: ModelLocalAlertRules,
) -> bool:
    stored = db.query(
        LOCAL_ENVIRONMENT_TABLE,
        {"tenant_id": report.tenant_id, "mode": report.mode.value},
    )
    writes = plan_local_environment_write(report, stored, rules, datetime.now(UTC))
    for row in writes:
        db.upsert(LOCAL_ENVIRONMENT_TABLE, LOCAL_ENVIRONMENT_KEY, row)
    return bool(writes)


__all__ = [
    "LOCAL_ENVIRONMENT_KEY",
    "LOCAL_ENVIRONMENT_TABLE",
    "VERDICT_COLUMNS",
    "apply_local_environment_status",
    "plan_local_environment_write",
]
