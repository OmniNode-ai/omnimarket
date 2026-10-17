# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: the local environment shows what is running, its health and its alerts.

The alert compute turns each service's latest report and an observation time into
a verdict (RUNNING, DOWN, UNHEALTHY, STALE) and the alert that verdict opens, with
the rules and the freshness deadline read from its contract. The environment
projection keeps one row per (tenant, mode, service) through the real SQLite
adapter, and stores each row's verdict and open alert so the page never derives
one.

Failure modes each test is written against:

* a stopped service keeps reading RUNNING, or opens no alert, or more than one;
* a running service that fails its health check reads RUNNING;
* a silent service keeps its last state forever instead of turning STALE after
  the deadline, or turns STALE before it;
* a one-shot service that finished cleanly reads DOWN;
* the rules or the deadline are hard-coded rather than read from the contract,
  so a contract edit changes nothing;
* a rule has no fixture that raises it and none that does not;
* a replayed report rewrites a row, moves its time or its cursor;
* a report that arrives late, older than the stored one, overwrites it;
* last_change moves on every report instead of only when the state changes;
* a native install grows compose rows, or loses its versions and access line;
* a report with no tenant or no service is stored under a default.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.models.model_local_service_health import (
    ModelLocalAlertRule,
    ModelLocalAlertRules,
    ModelLocalServiceHealth,
)
from omnimarket.nodes.node_local_alert_compute.handlers.handler_local_alert import (
    HandlerLocalAlert,
    load_local_alert_rules,
)
from omnimarket.nodes.node_local_alert_compute.models import ModelLocalAlertRequest
from omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_store import (
    LOCAL_ENVIRONMENT_TABLE,
    apply_local_environment_status,
    plan_local_environment_write,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_local_alert_compute"
    / "contract.yaml"
)
T0 = datetime(2026, 10, 17, 4, 0, 0, tzinfo=UTC)
TENANT = "11111111-1111-4111-8111-111111111111"
# The catalog local bundle's long-running services, as OV-2 names them.
ROSTER = ("postgres", "redpanda", "valkey", "omninode-runtime", "runtime-effects")


def _rules() -> ModelLocalAlertRules:
    return load_local_alert_rules(COMPUTE_CONTRACT)


def _health(
    service: str,
    state: EnumLocalServiceState,
    *,
    seen: datetime = T0,
    changed: datetime = T0,
) -> ModelLocalServiceHealth:
    return ModelLocalServiceHealth(
        tenant_id=TENANT,
        mode=EnumLocalEnvironmentMode.COMPOSE,
        service=service,
        observed_state=state,
        reason=f"{service} {state.value}",
        last_seen=seen,
        last_change=changed,
    )


def _evaluate(
    rows: list[ModelLocalServiceHealth],
    at: datetime,
    rules: ModelLocalAlertRules | None = None,
):
    return HandlerLocalAlert().handle(
        ModelLocalAlertRequest(
            services=tuple(rows), observed_at=at, rules=rules or _rules()
        )
    )


def _report(
    service: str,
    state: EnumLocalServiceState,
    at: datetime,
    *,
    mode: EnumLocalEnvironmentMode = EnumLocalEnvironmentMode.COMPOSE,
    **extra: Any,
) -> ModelLocalEnvironmentStatus:
    return ModelLocalEnvironmentStatus(
        tenant_id=TENANT,
        mode=mode,
        service=service,
        observed_state=state,
        reason=f"{service} {state.value}",
        observed_at=at,
        versions={"omnimarket": "0.4.317"},
        **extra,
    )


def _rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {LOCAL_ENVIRONMENT_TABLE} ORDER BY mode, service"
            )
        ]
    finally:
        conn.close()


def _by_service(db_path: Path) -> dict[str, dict[str, Any]]:
    return {row["service"]: row for row in _rows(db_path)}


@pytest.fixture
def db(tmp_path: Path) -> SqliteDatabaseAdapter:
    return SqliteDatabaseAdapter(tmp_path / "delegation.sqlite")


def _boot_roster(db: SqliteDatabaseAdapter, at: datetime = T0) -> None:
    for service in ROSTER:
        assert apply_local_environment_status(
            _report(service, EnumLocalServiceState.RUNNING, at), db, _rules()
        )


# ---- the alert compute -------------------------------------------------------


def test_the_rules_and_the_deadline_come_from_the_contract() -> None:
    rules = _rules()
    raw = yaml.safe_load(COMPUTE_CONTRACT.read_text())["config"]["local_alert"]
    assert rules.freshness_deadline_seconds == raw["freshness_deadline_seconds"] == 60
    assert {(r.code, r.severity, r.verdict) for r in rules.rules} == {
        ("SERVICE_DOWN", EnumLocalAlertSeverity.CRITICAL, EnumLocalServiceVerdict.DOWN),
        (
            "SERVICE_DEGRADED",
            EnumLocalAlertSeverity.WARNING,
            EnumLocalServiceVerdict.UNHEALTHY,
        ),
        (
            "HEALTH_UNKNOWN",
            EnumLocalAlertSeverity.WARNING,
            EnumLocalServiceVerdict.STALE,
        ),
    }


def test_a_down_row_opens_exactly_one_service_down_alert() -> None:
    stopped_at = T0 + timedelta(seconds=10)
    rows = [_health(s, EnumLocalServiceState.RUNNING, seen=stopped_at) for s in ROSTER]
    rows[1] = _health(
        "redpanda", EnumLocalServiceState.DOWN, seen=stopped_at, changed=stopped_at
    )
    result = _evaluate(rows, stopped_at)
    verdicts = {v.service: v for v in result.verdicts}
    assert verdicts["redpanda"].verdict is EnumLocalServiceVerdict.DOWN
    assert [(a.code, a.service) for a in result.alerts] == [
        ("SERVICE_DOWN", "redpanda")
    ]
    assert result.alerts[0].severity is EnumLocalAlertSeverity.CRITICAL
    assert result.alerts[0].since == stopped_at
    assert all(
        v.verdict is EnumLocalServiceVerdict.RUNNING and v.alert_code is None
        for s, v in verdicts.items()
        if s != "redpanda"
    )


def test_a_failing_health_check_reads_unhealthy_not_running() -> None:
    rows = [_health("valkey", EnumLocalServiceState.UNHEALTHY)]
    result = _evaluate(rows, T0)
    assert result.verdicts[0].verdict is EnumLocalServiceVerdict.UNHEALTHY
    assert [(a.code, a.severity) for a in result.alerts] == [
        ("SERVICE_DEGRADED", EnumLocalAlertSeverity.WARNING)
    ]


def test_a_silent_service_turns_stale_after_the_deadline_and_not_before() -> None:
    rows = [_health(s, EnumLocalServiceState.RUNNING) for s in ROSTER]
    at_deadline = _evaluate(rows, T0 + timedelta(seconds=60))
    assert {v.verdict for v in at_deadline.verdicts} == {
        EnumLocalServiceVerdict.RUNNING
    }
    assert at_deadline.alerts == ()
    past = _evaluate(rows, T0 + timedelta(seconds=61))
    assert {v.verdict for v in past.verdicts} == {EnumLocalServiceVerdict.STALE}
    assert sorted(a.service for a in past.alerts) == sorted(ROSTER)
    assert {a.code for a in past.alerts} == {"HEALTH_UNKNOWN"}
    assert all(v.reason == "no report for 61 s" for v in past.verdicts)
    assert {a.since for a in past.alerts} == {T0 + timedelta(seconds=60)}


def test_staleness_outranks_the_last_observed_state() -> None:
    # A row last seen DOWN, then silent past the deadline, is STALE: the
    # observer no longer vouches for DOWN either.
    rows = [_health("postgres", EnumLocalServiceState.DOWN)]
    result = _evaluate(rows, T0 + timedelta(seconds=90))
    assert result.verdicts[0].verdict is EnumLocalServiceVerdict.STALE
    assert [a.code for a in result.alerts] == ["HEALTH_UNKNOWN"]


def test_a_one_shot_service_that_exited_cleanly_reads_running() -> None:
    rows = [_health("forward-migration", EnumLocalServiceState.EXITED_OK)]
    result = _evaluate(rows, T0)
    assert result.verdicts[0].verdict is EnumLocalServiceVerdict.RUNNING
    assert result.alerts == ()


@pytest.mark.parametrize("rule_index", [0, 1, 2])
def test_every_rule_has_a_positive_and_a_negative_fixture(rule_index: int) -> None:
    rules = _rules()
    rule = rules.rules[rule_index]
    raising = {
        EnumLocalServiceVerdict.DOWN: (_health("s", EnumLocalServiceState.DOWN), T0),
        EnumLocalServiceVerdict.UNHEALTHY: (
            _health("s", EnumLocalServiceState.UNHEALTHY),
            T0,
        ),
        EnumLocalServiceVerdict.STALE: (
            _health("s", EnumLocalServiceState.RUNNING),
            T0 + timedelta(seconds=rules.freshness_deadline_seconds + 1),
        ),
    }[rule.verdict]
    positive = _evaluate([raising[0]], raising[1], rules)
    assert [a.code for a in positive.alerts] == [rule.code]
    negative = _evaluate([_health("s", EnumLocalServiceState.RUNNING)], T0, rules)
    assert rule.code not in {a.code for a in negative.alerts}


def test_a_verdict_with_no_rule_opens_no_alert() -> None:
    # The rules are data: a contract without SERVICE_DOWN raises nothing for DOWN.
    without_down = ModelLocalAlertRules(
        freshness_deadline_seconds=60,
        rules=tuple(r for r in _rules().rules if r.code != "SERVICE_DOWN"),
    )
    result = _evaluate(
        [_health("postgres", EnumLocalServiceState.DOWN)], T0, without_down
    )
    assert result.verdicts[0].verdict is EnumLocalServiceVerdict.DOWN
    assert result.alerts == ()


def test_the_compute_is_pure() -> None:
    rows = [_health(s, EnumLocalServiceState.RUNNING) for s in ROSTER]
    at = T0 + timedelta(seconds=61)
    assert _evaluate(rows, at) == _evaluate(rows, at)


def test_rules_that_alert_on_running_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLocalAlertRules(
            freshness_deadline_seconds=60,
            rules=(
                ModelLocalAlertRule(
                    code="X",
                    severity=EnumLocalAlertSeverity.WARNING,
                    verdict=EnumLocalServiceVerdict.RUNNING,
                ),
            ),
        )


# ---- the environment projection, through the real SQLite adapter ------------


def test_stopping_one_service_turns_its_row_down_with_one_open_alert(
    db: SqliteDatabaseAdapter,
) -> None:
    _boot_roster(db)
    stopped_at = T0 + timedelta(seconds=15)
    for service in ROSTER:
        state = (
            EnumLocalServiceState.DOWN
            if service == "redpanda"
            else EnumLocalServiceState.RUNNING
        )
        apply_local_environment_status(
            _report(service, state, stopped_at), db, _rules()
        )
    rows = _by_service(db.db_path)
    assert rows["redpanda"]["verdict"] == "down"
    assert rows["redpanda"]["alert_code"] == "SERVICE_DOWN"
    assert rows["redpanda"]["alert_severity"] == "critical"
    assert rows["redpanda"]["alert_since"] == stopped_at.isoformat()
    assert [s for s, r in rows.items() if r["alert_code"]] == ["redpanda"]


def test_a_failing_health_check_stores_unhealthy(db: SqliteDatabaseAdapter) -> None:
    _boot_roster(db)
    apply_local_environment_status(
        _report("valkey", EnumLocalServiceState.UNHEALTHY, T0 + timedelta(seconds=15)),
        db,
        _rules(),
    )
    row = _by_service(db.db_path)["valkey"]
    assert (row["verdict"], row["alert_code"]) == ("unhealthy", "SERVICE_DEGRADED")


def test_any_later_report_marks_silent_rows_stale(db: SqliteDatabaseAdapter) -> None:
    # The observer reports only postgres after the deadline; every other row has
    # gone silent and turns STALE in the store, not in the browser.
    _boot_roster(db)
    late = T0 + timedelta(seconds=75)
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.RUNNING, late), db, _rules()
    )
    rows = _by_service(db.db_path)
    assert rows["postgres"]["verdict"] == "running"
    assert {s for s, r in rows.items() if r["verdict"] == "stale"} == set(ROSTER) - {
        "postgres"
    }
    assert {r["alert_code"] for s, r in rows.items() if s != "postgres"} == {
        "HEALTH_UNKNOWN"
    }


def test_replaying_the_same_reports_changes_no_row(db: SqliteDatabaseAdapter) -> None:
    reports = [_report(s, EnumLocalServiceState.RUNNING, T0) for s in ROSTER]
    reports.append(
        _report("redpanda", EnumLocalServiceState.DOWN, T0 + timedelta(seconds=15))
    )
    for report in reports:
        apply_local_environment_status(report, db, _rules())
    before = _rows(db.db_path)
    for report in reports:
        assert apply_local_environment_status(report, db, _rules()) is False
    assert _rows(db.db_path) == before


def test_a_late_older_report_changes_nothing(db: SqliteDatabaseAdapter) -> None:
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.DOWN, T0 + timedelta(seconds=20)),
        db,
        _rules(),
    )
    before = _rows(db.db_path)
    assert (
        apply_local_environment_status(
            _report("postgres", EnumLocalServiceState.RUNNING, T0), db, _rules()
        )
        is False
    )
    assert _rows(db.db_path) == before


def test_last_change_moves_only_when_the_state_changes(
    db: SqliteDatabaseAdapter,
) -> None:
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.RUNNING, T0), db, _rules()
    )
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.RUNNING, T0 + timedelta(seconds=15)),
        db,
        _rules(),
    )
    row = _by_service(db.db_path)["postgres"]
    assert row["last_seen"] == (T0 + timedelta(seconds=15)).isoformat()
    assert row["last_change"] == T0.isoformat()
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.DOWN, T0 + timedelta(seconds=30)),
        db,
        _rules(),
    )
    assert (
        _by_service(db.db_path)["postgres"]["last_change"]
        == (T0 + timedelta(seconds=30)).isoformat()
    )


def test_a_rewritten_row_takes_a_cursor_after_every_other_row(
    db: SqliteDatabaseAdapter,
) -> None:
    # The exposure walks rows by cursor; a row that changed in place must sort
    # after the rows read before it, or a reader resuming from its last cursor
    # never sees the change. Both the reported row's rewrite and a sibling's
    # re-judgement move it.
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.RUNNING, T0), db, _rules()
    )
    apply_local_environment_status(
        _report("valkey", EnumLocalServiceState.RUNNING, T0), db, _rules()
    )
    first = {s: r["projection_cursor"] for s, r in _by_service(db.db_path).items()}
    assert first["postgres"] < first["valkey"]

    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.DOWN, T0 + timedelta(seconds=15)),
        db,
        _rules(),
    )
    second = {s: r["projection_cursor"] for s, r in _by_service(db.db_path).items()}
    assert second["postgres"] > first["valkey"]
    assert second["valkey"] == first["valkey"]

    # 75 s later postgres reports again: valkey is re-judged STALE in place.
    apply_local_environment_status(
        _report("postgres", EnumLocalServiceState.DOWN, T0 + timedelta(seconds=75)),
        db,
        _rules(),
    )
    third = {s: r["projection_cursor"] for s, r in _by_service(db.db_path).items()}
    assert third["valkey"] > second["postgres"]


def test_rows_of_another_tenant_or_mode_are_never_judged_or_rewritten() -> None:
    stored = [
        {
            "tenant_id": "another-tenant",
            "mode": "compose",
            "service": "valkey",
            "observed_state": "running",
            "reason": "valkey running",
            "last_seen": T0.isoformat(),
            "last_change": T0.isoformat(),
            "verdict": "running",
            "verdict_reason": "valkey running",
            "alert_code": None,
            "alert_severity": None,
            "alert_since": None,
            "versions": "{}",
            "report_id": "r",
        }
    ]
    writes = plan_local_environment_write(
        _report("postgres", EnumLocalServiceState.RUNNING, T0 + timedelta(seconds=90)),
        stored,
        _rules(),
        T0,
    )
    assert [(w["tenant_id"], w["service"]) for w in writes] == [(TENANT, "postgres")]


def test_a_native_install_has_one_native_row_and_no_compose_rows(
    db: SqliteDatabaseAdapter,
) -> None:
    minted = T0 - timedelta(seconds=1)
    apply_local_environment_status(
        _report(
            "local-runtime",
            EnumLocalServiceState.RUNNING,
            T0,
            mode=EnumLocalEnvironmentMode.NATIVE,
            store_kind="sqlite",
            store_location="~/.omninode/delegation/delegation.sqlite",
            access_mode="loopback-token",
            token_minted_at=minted,
            rotate_hint="restart onex dashboard",
        ),
        db,
        _rules(),
    )
    rows = _rows(db.db_path)
    assert [(r["mode"], r["service"]) for r in rows] == [("native", "local-runtime")]
    row = rows[0]
    assert row["versions"] == '{"omnimarket": "0.4.317"}'
    assert (row["store_kind"], row["access_mode"], row["token_minted_at"]) == (
        "sqlite",
        "loopback-token",
        minted.isoformat(),
    )
    assert row["verdict"] == "running"


def test_a_report_without_a_tenant_or_a_service_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLocalEnvironmentStatus(
            tenant_id="",
            mode=EnumLocalEnvironmentMode.COMPOSE,
            service="postgres",
            observed_state=EnumLocalServiceState.RUNNING,
            reason="ok",
            observed_at=T0,
        )
    with pytest.raises(ValidationError):
        ModelLocalEnvironmentStatus(
            tenant_id=TENANT,
            mode=EnumLocalEnvironmentMode.COMPOSE,
            service="",
            observed_state=EnumLocalServiceState.RUNNING,
            reason="ok",
            observed_at=T0,
        )
