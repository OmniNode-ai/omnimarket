# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19991: local alert transitions, and the local_alerts fold keyed on (tenant, code, subject).

An hour of compose health reports goes through the real environment store on a
real SQLite file. Each write's verdict changes become raised or cleared
transitions, and each transition is folded into one local_alerts row per
(tenant, code, subject) by UPSERT.

Failure modes each test is written against:

* a transition id depends on when or where it was derived, so a replay is not
  recognised and writes again;
* replaying the hour's reports, or its transitions, changes a row;
* folding the same transitions in another order gives other rows;
* a repeated condition opens a second row instead of updating the first;
* an alert clears without a later report meeting the clear condition (time
  passing alone clears it), or never clears when one arrives;
* an older transition arriving late overwrites a newer row.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_alert_transition import EnumLocalAlertTransition
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict
from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.handlers.handler_local_environment_store import (
    LOCAL_ENVIRONMENT_TABLE,
    apply_local_environment_status,
)
from omnimarket.models.model_local_alert_transitioned import (
    ModelLocalAlertEvidence,
    ModelLocalAlertTransitioned,
    local_alert_subject,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_local_alert_transition_compute.handlers import (
    LOCAL_ALERTS_KEY,
    LOCAL_ALERTS_TABLE,
    HandlerLocalAlertTransition,
    plan_local_alert_fold,
)
from omnimarket.nodes.node_local_alert_transition_compute.models import (
    ModelLocalAlertTransitionRequest,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
TENANT = "11111111-1111-4111-8111-111111111111"
ROSTER = ("omninode-runtime", "postgres", "redpanda", "valkey")
CADENCE = timedelta(seconds=30)
COMPOSE = EnumLocalEnvironmentMode.COMPOSE

# The local SQLite store's local_alerts table for this proof. The store step,
# the Postgres migrations and their grants land with the projection node.
LOCAL_ALERTS_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS local_alerts (
    tenant_id TEXT NOT NULL,
    code TEXT NOT NULL,
    subject TEXT NOT NULL,
    mode TEXT NOT NULL,
    service TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('raised', 'cleared')),
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'warning')),
    raised_at TEXT NOT NULL,
    cleared_at TEXT,
    evidence TEXT NOT NULL,
    last_transition_id TEXT NOT NULL,
    last_transition_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, code, subject),
    CHECK ((state = 'cleared') = (cleared_at IS NOT NULL))
);
"""


def apply_local_alert_transition(
    transition: ModelLocalAlertTransitioned, db: SqliteDatabaseAdapter
) -> bool:
    """Read the stored row, plan, and UPSERT the plan, in one write transaction."""
    with db.write_transaction() as tx:
        stored = tx.query(
            LOCAL_ALERTS_TABLE,
            {
                "tenant_id": transition.tenant_id,
                "code": transition.code,
                "subject": transition.subject,
            },
        )
        row = plan_local_alert_fold(transition, stored[0] if stored else None)
        if row is None:
            return False
        tx.upsert(LOCAL_ALERTS_TABLE, LOCAL_ALERTS_KEY, row)
        return True


def derive_local_alert_transitions(
    before: list[dict[str, object]], after: list[dict[str, object]]
) -> tuple[ModelLocalAlertTransitioned, ...]:
    return (
        HandlerLocalAlertTransition()
        .handle(
            ModelLocalAlertTransitionRequest(before=tuple(before), after=tuple(after))
        )
        .transitions
    )


def _state_at(service: str, offset: int) -> EnumLocalServiceState | None:
    """The scripted hour: what each service reports at an offset, or None for silence.

    postgres is stopped twice (a repeated condition), redpanda fails its health
    check once, valkey goes silent for five minutes, the runtime stays up.
    """
    if service == "postgres" and (600 <= offset < 1200 or 3000 <= offset < 3300):
        return EnumLocalServiceState.DOWN
    if service == "redpanda" and 1800 <= offset < 2100:
        return EnumLocalServiceState.UNHEALTHY
    if service == "valkey" and 2400 <= offset < 2700:
        return None
    return EnumLocalServiceState.RUNNING


def _hour() -> list[ModelLocalEnvironmentStatus]:
    reports: list[ModelLocalEnvironmentStatus] = []
    for tick in range(int(timedelta(hours=1) / CADENCE)):
        offset = tick * int(CADENCE.total_seconds())
        for service in ROSTER:
            state = _state_at(service, offset)
            if state is None:
                continue
            reports.append(
                ModelLocalEnvironmentStatus(
                    tenant_id=TENANT,
                    mode=COMPOSE,
                    service=service,
                    observed_state=state,
                    reason=f"{service} {state.value}",
                    observed_at=T0 + timedelta(seconds=offset),
                )
            )
    return reports


def _store(path: Path) -> SqliteDatabaseAdapter:
    db = SqliteDatabaseAdapter(path)
    db.reconcile()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LOCAL_ALERTS_SQLITE_DDL)
    finally:
        conn.close()
    return db


def _table(path: Path, table: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        order = (
            "tenant_id, code, subject"
            if table == LOCAL_ALERTS_TABLE
            else "tenant_id, mode, service"
        )
        return [
            {k: v for k, v in dict(row).items() if k != "projection_cursor"}
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]
    finally:
        conn.close()


def _env_rows(db: SqliteDatabaseAdapter) -> list[dict[str, object]]:
    return db.query(LOCAL_ENVIRONMENT_TABLE, {"tenant_id": TENANT})


def _play(
    db: SqliteDatabaseAdapter, reports: list[ModelLocalEnvironmentStatus]
) -> list[ModelLocalAlertTransitioned]:
    """Store each report, derive the transitions its write made, fold each one."""
    rules = load_local_alert_rules()
    transitions: list[ModelLocalAlertTransitioned] = []
    for report in reports:
        before = _env_rows(db)
        if not apply_local_environment_status(report, db, rules):
            continue
        for transition in derive_local_alert_transitions(before, _env_rows(db)):
            apply_local_alert_transition(transition, db)
            transitions.append(transition)
    return transitions


@pytest.fixture(scope="module")
def played(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, list[ModelLocalAlertTransitioned]]]:
    # One play of the hour per module: no test below may change this store.
    path = tmp_path_factory.mktemp("omn19991") / "delegation.sqlite"
    db = _store(path)
    return path, _play(db, _hour())


# ---- AC1: replay changes no row --------------------------------------------


def test_replaying_the_hour_changes_no_alert_or_environment_row(
    played: tuple[Path, list[ModelLocalAlertTransitioned]],
) -> None:
    path, transitions = played
    alerts = _table(path, LOCAL_ALERTS_TABLE)
    environment = _table(path, LOCAL_ENVIRONMENT_TABLE)
    assert alerts, "positive control: the hour raised alerts"
    assert transitions

    replayed = _play(SqliteDatabaseAdapter(path), _hour())

    assert replayed == []
    assert _table(path, LOCAL_ALERTS_TABLE) == alerts
    assert _table(path, LOCAL_ENVIRONMENT_TABLE) == environment


def test_replaying_every_transition_writes_nothing(
    played: tuple[Path, list[ModelLocalAlertTransitioned]],
) -> None:
    path, transitions = played
    alerts = _table(path, LOCAL_ALERTS_TABLE)
    db = SqliteDatabaseAdapter(path)
    assert not any(apply_local_alert_transition(t, db) for t in transitions)
    assert _table(path, LOCAL_ALERTS_TABLE) == alerts


def test_a_second_run_of_the_hour_derives_the_same_transition_ids(
    tmp_path: Path, played: tuple[Path, list[ModelLocalAlertTransitioned]]
) -> None:
    path, transitions = played
    again = _play(_store(tmp_path / "second.sqlite"), _hour())
    assert [t.transition_id for t in again] == [t.transition_id for t in transitions]
    assert _table(tmp_path / "second.sqlite", LOCAL_ALERTS_TABLE) == _table(
        path, LOCAL_ALERTS_TABLE
    )


@pytest.mark.parametrize("order", ["reversed", "shuffled"])
def test_folding_the_transitions_in_any_order_gives_the_same_rows(
    tmp_path: Path,
    played: tuple[Path, list[ModelLocalAlertTransitioned]],
    order: str,
) -> None:
    path, transitions = played
    stream = list(reversed(transitions))
    if order == "shuffled":
        random.Random(19991).shuffle(stream)
    other = _store(tmp_path / f"{order}.sqlite")
    for transition in stream + stream:
        apply_local_alert_transition(transition, other)
    assert _table(tmp_path / f"{order}.sqlite", LOCAL_ALERTS_TABLE) == _table(
        path, LOCAL_ALERTS_TABLE
    )


def test_a_repeated_condition_updates_one_row(
    played: tuple[Path, list[ModelLocalAlertTransitioned]],
) -> None:
    path, transitions = played
    subject = local_alert_subject(COMPOSE, "postgres")
    raises = [
        t
        for t in transitions
        if t.code == "SERVICE_DOWN"
        and t.subject == subject
        and t.transition is EnumLocalAlertTransition.RAISED
    ]
    assert [t.raised_at for t in raises] == [
        T0 + timedelta(seconds=600),
        T0 + timedelta(seconds=3000),
    ]
    rows = [
        r
        for r in _table(path, LOCAL_ALERTS_TABLE)
        if r["code"] == "SERVICE_DOWN" and r["subject"] == subject
    ]
    assert len(rows) == 1
    assert rows[0]["state"] == "cleared"
    assert rows[0]["raised_at"] == (T0 + timedelta(seconds=3000)).isoformat()
    assert rows[0]["cleared_at"] == (T0 + timedelta(seconds=3300)).isoformat()


def test_the_hour_ends_with_one_row_per_condition_all_cleared(
    played: tuple[Path, list[ModelLocalAlertTransitioned]],
) -> None:
    path, _ = played
    rows = _table(path, LOCAL_ALERTS_TABLE)
    assert [(r["code"], r["subject"], r["state"]) for r in rows] == [
        ("HEALTH_UNKNOWN", "compose/valkey", "cleared"),
        ("SERVICE_DEGRADED", "compose/redpanda", "cleared"),
        ("SERVICE_DOWN", "compose/postgres", "cleared"),
    ]
    stale = rows[0]
    # Silent from 2400 s: stale once 60 s pass after its last report at 2370 s,
    # and cleared only by its own next report at 2700 s.
    assert stale["raised_at"] == (T0 + timedelta(seconds=2430)).isoformat()
    assert stale["cleared_at"] == (T0 + timedelta(seconds=2700)).isoformat()


# ---- clear only from a later event meeting the clear condition -------------


def test_an_alert_stays_raised_until_a_report_meets_the_clear_condition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clear.sqlite"
    db = _store(path)
    down_until_1200 = [
        r for r in _hour() if r.observed_at < T0 + timedelta(seconds=1200)
    ]
    _play(db, down_until_1200)
    (row,) = _table(path, LOCAL_ALERTS_TABLE)
    assert (row["code"], row["state"], row["cleared_at"]) == (
        "SERVICE_DOWN",
        "raised",
        None,
    )
    # The first postgres report at 1200 s is RUNNING: that, and nothing else, clears.
    _play(db, [r for r in _hour() if r.observed_at == T0 + timedelta(seconds=1200)])
    (row,) = _table(path, LOCAL_ALERTS_TABLE)
    assert row["state"] == "cleared"
    assert row["cleared_at"] == (T0 + timedelta(seconds=1200)).isoformat()
    assert row["raised_at"] == (T0 + timedelta(seconds=600)).isoformat()


def test_a_late_older_transition_does_not_overwrite_a_newer_row(
    tmp_path: Path, played: tuple[Path, list[ModelLocalAlertTransitioned]]
) -> None:
    path, transitions = played
    db = SqliteDatabaseAdapter(path)
    first_raise = transitions[0]
    assert first_raise.transition is EnumLocalAlertTransition.RAISED
    before = _table(path, LOCAL_ALERTS_TABLE)
    assert not apply_local_alert_transition(first_raise, db)
    assert _table(path, LOCAL_ALERTS_TABLE) == before


# ---- the transition id -----------------------------------------------------


def _transition(**overrides: Any) -> ModelLocalAlertTransitioned:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "code": "SERVICE_DOWN",
        "subject": "compose/postgres",
        "transition": EnumLocalAlertTransition.RAISED,
        "severity": EnumLocalAlertSeverity.CRITICAL,
        "raised_at": T0,
        "occurred_at": T0,
        "evidence": ModelLocalAlertEvidence(
            mode=COMPOSE,
            service="postgres",
            verdict=EnumLocalServiceVerdict.DOWN,
            reason="postgres down",
            last_seen=T0,
        ),
    }
    fields.update(overrides)
    return ModelLocalAlertTransitioned(**fields)


def test_the_transition_id_is_derived_from_the_transition_alone() -> None:
    a = _transition()
    b = _transition(evidence=a.evidence.model_copy(update={"reason": "other"}))
    assert a.transition_id == b.transition_id
    assert a.transition_id != _transition(subject="compose/valkey").transition_id
    # The same instant in another zone is the same transition.
    assert (
        _transition(
            raised_at=T0.astimezone(timezone(timedelta(hours=5))),
            occurred_at=T0.astimezone(timezone(timedelta(hours=5))),
        ).transition_id
        == a.transition_id
    )


def test_a_transition_id_that_does_not_match_its_fields_is_refused() -> None:
    other = _transition(code="SERVICE_DEGRADED").transition_id
    with pytest.raises(ValidationError):
        _transition(transition_id=other)


def test_a_clear_before_its_raise_is_refused() -> None:
    with pytest.raises(ValidationError):
        _transition(
            transition=EnumLocalAlertTransition.CLEARED,
            raised_at=T0 + timedelta(seconds=1),
        )
