# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22030: the observer retires a service its lane's overlay no longer names.

The dev lane's observer probed the laptop catalog until its overlay was installed. The rows it had
stored for services the lane does not run (consumer-health-projection, tenant-projection-writer,
runtime-effects) stayed DOWN with nothing left to report on them, and the Overview status line, which
reads the projection's first row, read "Runtime down". A row is never deleted by hand: the overlay lists
the services the lane has retired, the observer reports each as ``retired`` on every pass, and the
projection writer removes the stored row, clears its open alert and publishes a tombstone.

Failure modes each test is written against:

* R1 the overlay's retired list is not reported, so the old rows stay;
* R2 a retired report is stored as a row (a `retired` state is not a state a service can hold);
* R3 the retirement removes a sibling's row, or leaves its own open alert open;
* R4 a repeated retired report for a service that holds no row writes or publishes something;
* R5 a service is both probed and retired, and the overlay is accepted.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.handlers.handler_local_environment_store import (
    plan_local_environment_write,
)
from omnimarket.models.model_local_environment_status import ModelLocalEnvironmentStatus
from omnimarket.nodes.node_local_environment_observe_effect.handlers import (
    handler_local_environment_observe as observer,
)
from omnimarket.nodes.node_local_environment_observe_effect.overlay import (
    ModelLocalEnvironmentRoster,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_writer import (
    HandlerLocalEnvironmentProjectionRunner,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_projection_local_environment import (
    HandlerProjectionLocalEnvironment,
    plan_local_environment_retirement,
)
from omnimarket.nodes.node_projection_local_environment.models.model_local_environment_plan import (
    ModelLocalEnvironmentPlanRequest,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn19988_local_environment_writer import (
    STATUS_TOPIC,
    T0,
    TENANT,
    _Conn,
    _Db,
    _report,
)

pytestmark = pytest.mark.unit

OVERLAY_ID = "node_local_environment_observe_effect"
RETIRED = ["consumer-health-projection", "tenant-projection-writer", "runtime-effects"]
NOW = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)


class _RunningProber:
    async def probe(
        self, target: observer.ModelProbeTarget, timeout: float
    ) -> observer.ModelProbeResult:
        return observer.ModelProbeResult(
            observed_state=EnumLocalServiceState.RUNNING, reason="probed."
        )


def _roster(**extra: Any) -> dict[str, Any]:
    return {
        "services": [
            {
                "service": "omninode-runtime",
                "port": 8085,
                "scheme": "http",
                "path": "/health",
            }
        ],
        **extra,
    }


def _tick() -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=NOW,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid4(),
        scheduler_id="omn22030-test",
        tick_interval_ms=1000,
    )


async def _observe(
    tmp_path: Path, document: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> list[ModelLocalEnvironmentStatus]:
    node_dir = tmp_path / "overlays" / OVERLAY_ID
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document))
    monkeypatch.setattr(observer, "_resolve_observed_tenant", lambda: TENANT)
    handler = observer.HandlerLocalEnvironmentObserve(
        prober=_RunningProber(),
        config=observer.ModelLocalEnvironmentObserveConfig(catalog_dir=str(tmp_path)),
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
    )
    output = await handler.handle(_tick())
    return [e for e in output.events if isinstance(e, ModelLocalEnvironmentStatus)]


# ---- R1, R5: the observer and the overlay -------------------------------------


@pytest.mark.asyncio
async def test_the_observer_reports_each_service_the_overlay_retires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = await _observe(tmp_path, _roster(retired=RETIRED), monkeypatch)
    by_service = {e.service: e.observed_state for e in events}
    assert by_service == {
        "omninode-runtime": EnumLocalServiceState.RUNNING,
        **dict.fromkeys(RETIRED, EnumLocalServiceState.RETIRED),
    }


@pytest.mark.asyncio
async def test_an_overlay_with_no_retired_list_reports_only_what_it_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = await _observe(tmp_path, _roster(), monkeypatch)
    assert [e.service for e in events] == ["omninode-runtime"]


def test_a_service_both_probed_and_retired_refuses_the_overlay() -> None:
    with pytest.raises(ValidationError, match="both probed and retired"):
        ModelLocalEnvironmentRoster.model_validate(
            _roster(retired=["omninode-runtime"])
        )


# ---- R2, R3: the plan ---------------------------------------------------------


def _stored(service: str, state: str, at: datetime) -> dict[str, Any]:
    report = ModelLocalEnvironmentStatus.model_validate(_report(service, state, at))
    row = plan_local_environment_write(report, [], load_local_alert_rules(), at)[0]
    return dict(row)


def _retired_report(service: str, at: datetime) -> ModelLocalEnvironmentStatus:
    return ModelLocalEnvironmentStatus.model_validate(
        {**_report(service, "retired", at), "reason": "no longer in the roster"}
    )


def test_a_retired_report_is_never_a_row_to_store() -> None:
    stored = [_stored("runtime-effects", "down", T0)]
    report = _retired_report("runtime-effects", T0 + timedelta(seconds=15))
    plan = HandlerProjectionLocalEnvironment().handle(
        ModelLocalEnvironmentPlanRequest(
            report=report, stored_rows=tuple(stored), rules=load_local_alert_rules()
        )
    )
    assert plan.rows == ()


def test_the_retirement_names_only_the_services_own_row() -> None:
    stored = [
        _stored("runtime-effects", "down", T0),
        _stored("redpanda", "running", T0),
    ]
    removed = plan_local_environment_retirement(
        _retired_report("runtime-effects", T0 + timedelta(seconds=15)), stored
    )
    assert removed is not None
    assert removed["service"] == "runtime-effects"


def test_a_service_with_no_row_has_nothing_to_retire() -> None:
    stored = [_stored("redpanda", "running", T0)]
    assert (
        plan_local_environment_retirement(
            _retired_report("runtime-effects", T0), stored
        )
        is None
    )


def test_a_running_report_retires_nothing() -> None:
    stored = [_stored("redpanda", "running", T0)]
    report = ModelLocalEnvironmentStatus.model_validate(_report("redpanda", "down", T0))
    assert plan_local_environment_retirement(report, stored) is None


# ---- R3, R4: the writer -------------------------------------------------------


class _DeletingConn(_Conn):
    """The stand-in table, taught the one DELETE the retirement sends."""

    async def execute(self, sql: str, *args: Any) -> str:
        if sql.lstrip().upper().startswith("DELETE FROM PUBLIC.LOCAL_ENVIRONMENT"):
            self.statements.append((sql, args))
            assert args[0] == self.tenant_setting
            self.table.pop((args[0], args[1], args[2]), None)
            return "DELETE 1"
        return await super().execute(sql, *args)


def _writer_with_delete(
    table: dict[tuple[str, str, str], dict[str, Any]],
) -> tuple[
    HandlerLocalEnvironmentProjectionRunner,
    _DeletingConn,
    list[dict[str, Any]],
    list[bytes],
]:
    writer = HandlerLocalEnvironmentProjectionRunner()
    conn = _DeletingConn(table)
    patch = pytest.MonkeyPatch()
    patch.setattr(writer, "_db", _Db(conn))
    deltas: list[dict[str, Any]] = []
    transitions: list[bytes] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        deltas.append(kwargs)
        return True

    async def _transition_publish(topic: str, value: bytes) -> None:
        transitions.append(value)

    patch.setattr(writer, "_publish_fn", _transition_publish)
    patch.setattr(writer, "publish_snapshot_delta", _capture)
    return writer, conn, deltas, transitions


def _project(
    writer: HandlerLocalEnvironmentProjectionRunner, report: dict[str, Any]
) -> None:
    meta = MessageMeta(partition=0, offset=9, fallback_id="t", topic=STATUS_TOPIC)
    asyncio.run(writer.project_event(STATUS_TOPIC, dict(report), meta))


def test_the_writer_removes_the_retired_row_clears_its_alert_and_leaves_siblings() -> (
    None
):
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, _conn, deltas, transitions = _writer_with_delete(table)
    _project(writer, _report("runtime-effects", "down", T0))
    _project(writer, _report("redpanda", "running", T0 + timedelta(seconds=1)))
    assert table[(TENANT, "compose", "runtime-effects")]["alert_code"] == "SERVICE_DOWN"
    sibling = copy.deepcopy(table[(TENANT, "compose", "redpanda")])
    deltas.clear()
    transitions.clear()

    _project(writer, _report("runtime-effects", "retired", T0 + timedelta(seconds=30)))

    assert sorted(k[2] for k in table) == ["redpanda"]
    assert table[(TENANT, "compose", "redpanda")] == sibling
    assert len(transitions) == 1
    assert b'"cleared"' in transitions[0]
    assert [(d["op"], d["row"], d["key"]["service"]) for d in deltas] == [
        ("delete", None, "runtime-effects")
    ]


def test_a_repeated_retired_report_for_a_forgotten_service_changes_and_publishes_nothing() -> (
    None
):
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, deltas, transitions = _writer_with_delete(table)
    _project(writer, _report("redpanda", "running", T0))
    deltas.clear()
    before = copy.deepcopy(table)
    deletes = len(conn.statements)

    _project(writer, _report("runtime-effects", "retired", T0 + timedelta(seconds=15)))

    assert table == before
    assert deltas == []
    assert transitions == []
    assert not [
        s
        for s, _ in conn.statements[deletes:]
        if s.lstrip().upper().startswith("DELETE")
    ]
