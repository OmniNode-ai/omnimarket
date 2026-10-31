# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21326 AC3: the deployed-lane readback fails by exit status when a run is over."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator import runtime_budget_readback
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_budget_readback as readback,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.budget_reading_source import (
    ProjectionBudgetReadingSource,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelBudgetReading,
    load_budget_readback_source,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


def _run(
    root: Path,
    run_id: str,
    *,
    duration_ms: int,
    execution_timeout: int = 50,
    requested: int | None = 50,
    locus: str = "deployed-lane",
    status: str = "completed",
    age: timedelta = timedelta(hours=1),
) -> None:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    budget = {
        "requested_timeout_seconds": requested,
        "execution_timeout_seconds": execution_timeout,
        "task_class_timeout_ceiling_seconds": 240,
        "terminal_delivery_margin_seconds": 60,
    }
    receipt = {
        "locus": locus,
        "receipt": {
            "result": {
                "status": status,
                "execution_duration_ms": duration_ms,
                "budget_evidence": budget,
            }
        },
    }
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    run_json = run_dir / "run.json"
    run_json.write_text("{}", encoding="utf-8")
    stamp = (NOW - age).timestamp()
    os.utime(run_json, (stamp, stamp))


def _main(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = runtime_budget_readback.main(
        ["--state-root", str(root), "--until", NOW.isoformat()]
    )
    return code, json.loads(capsys.readouterr().out)


def test_the_declared_grace_is_one_value() -> None:
    assert readback.declared_grace_seconds() == 15


@pytest.mark.parametrize(
    ("state_dir", "state_root", "expected"),
    [
        ("/configured/dir", "/configured/root", "/configured/dir"),
        ("", "/configured/root", "/configured/root"),
        ("  ", " /configured/root ", "/configured/root"),
        ("", "", None),
        ("relative/path", "", None),
    ],
)
def test_default_state_roots_follow_shared_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
    state_dir: str,
    state_root: str,
    expected: str | None,
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", state_dir)
    monkeypatch.setenv("ONEX_STATE_ROOT", state_root)

    assert readback.default_state_roots() == [
        Path(expected) if expected is not None else Path.home() / ".onex_state"
    ]


def test_runs_inside_the_bound_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "a", duration_ms=49_000)
    _run(tmp_path, "b", duration_ms=65_000)

    code, summary = _main(tmp_path, capsys)

    assert code == 0
    assert summary["deployed_lane_completed"] == 2
    assert summary["over_budget_plus_grace"] == 0


def test_a_completed_run_past_budget_plus_grace_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "inside", duration_ms=49_000)
    _run(tmp_path, "over", duration_ms=65_001)

    code, summary = _main(tmp_path, capsys)

    assert code == 1
    assert summary["over_runs"] == [
        {"run_id": "over", "execution_duration_ms": 65_001, "bound_ms": 65_000}
    ]


def test_an_empty_window_measures_nothing_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "before-window", duration_ms=99_000, age=timedelta(hours=30))
    _run(tmp_path, "in-process", duration_ms=99_000, locus="in-process")
    _run(tmp_path, "unrequested", duration_ms=99_000, requested=None)
    _run(tmp_path, "timed-out", duration_ms=99_000, status="failed")

    code, summary = _main(tmp_path, capsys)

    assert code == 2
    assert summary["deployed_lane_completed"] == 0


def test_a_missing_state_root_is_unrunnable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary = _main(tmp_path / "absent", capsys)

    assert code == 3
    assert "state root not found" in summary["error"]


def test_disagreeing_graces_are_unrunnable(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts.yaml"
    contracts.write_text(
        "execution_budgets:\n"
        "  a: {requested_timeout_grace_seconds: 15}\n"
        "  b: {requested_timeout_grace_seconds: 20}\n",
        encoding="utf-8",
    )
    module = readback

    with pytest.raises(module.CheckUnrunnableError):
        module.declared_grace_seconds(contracts)


@pytest.mark.parametrize(("duration_ms", "expected"), [(65_000, 0), (65_001, 1)])
def test_deployed_bus_terminal_payload_is_measured(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    duration_ms: int,
    expected: int,
) -> None:
    _run(tmp_path, "bus-terminal", duration_ms=duration_ms)
    receipt_path = tmp_path / "runs" / "bus-terminal" / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload = receipt["receipt"]["result"]
    receipt["receipt"]["result"] = {
        "handler_result": None,
        "terminal_payload": {"payload": payload},
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    code, summary = _main(tmp_path, capsys)

    assert code == expected
    assert summary["deployed_lane_completed"] == 1
    assert summary["over_budget_plus_grace"] == (expected == 1)


@pytest.mark.parametrize(
    ("duration_ms", "expected"), [(65_000, 0), (65_001, 1), (None, 2), (None, 3)]
)
def test_packaged_module_preserves_process_exit_status(
    tmp_path: Path, duration_ms: int | None, expected: int
) -> None:
    if duration_ms is not None:
        _run(tmp_path, "selected", duration_ms=duration_ms)
    state_root = tmp_path / "absent" if expected == 3 else tmp_path
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            runtime_budget_readback.__name__,
            "--state-root",
            str(state_root),
            "--until",
            NOW.isoformat(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == expected, result.stderr
    summary = json.loads(result.stdout)
    if expected == 3:
        assert "state root not found" in summary["error"]
    else:
        assert summary["grace_seconds"] == 15
        assert summary["deployed_lane_completed"] == (duration_ms is not None)
        assert summary["over_budget_plus_grace"] == (expected == 1)


# --- OMN-17427: the readback reads the lab projection, not one machine's disk ---


class _Projection:
    """A projection source that records the window it was asked for."""

    def __init__(self, readings: list[ModelBudgetReading]) -> None:
        self.readings = readings
        self.windows: list[tuple[datetime, datetime]] = []

    def describe(self) -> str:
        return "projection fixture"

    def read(self, since: datetime, until: datetime) -> list[ModelBudgetReading]:
        self.windows.append((since, until))
        return self.readings


def _reading(
    run_id: str,
    *,
    duration_ms: int | None,
    execution_timeout: int | None = 50,
    requested: int | None = 50,
    status: str | None = "completed",
) -> ModelBudgetReading:
    return ModelBudgetReading(
        run_id=run_id,
        status=status,
        requested_timeout_seconds=requested,
        execution_timeout_seconds=execution_timeout,
        execution_duration_ms=duration_ms,
    )


def _from_projection(
    projection: _Projection, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict]:
    code = runtime_budget_readback.main(
        ["--until", NOW.isoformat()], projection=projection
    )
    return code, json.loads(capsys.readouterr().out)


def test_the_default_source_is_the_projection_and_never_this_hosts_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An over-budget receipt sits in the default local state root, as it did on
    # the machine that invoked the CLI. The projection holds nothing for the
    # window, so a readback that fell back to the disk would exit 1, not 2.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    local_root = tmp_path / ".onex_state"
    local_root.mkdir()
    _run(local_root, "private-to-this-machine", duration_ms=99_000)

    code, summary = _from_projection(_Projection([]), capsys)

    assert code == 2
    assert summary["source"] == "projection fixture"
    assert summary["deployed_lane_completed"] == 0


def test_a_projection_run_past_budget_plus_grace_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    projection = _Projection(
        [
            _reading("inside", duration_ms=65_000),
            _reading("over", duration_ms=65_001),
        ]
    )

    code, summary = _from_projection(projection, capsys)

    assert code == 1
    assert summary["over_runs"] == [
        {"run_id": "over", "execution_duration_ms": 65_001, "bound_ms": 65_000}
    ]
    since, until = projection.windows[0]
    assert until == NOW
    assert since == NOW - timedelta(hours=24)


def test_a_projection_run_the_caller_never_bounded_is_not_selected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    projection = _Projection(
        [
            _reading("unrequested", duration_ms=99_000, requested=None),
            _reading("failed", duration_ms=99_000, status="failed"),
            _reading("unmeasured", duration_ms=None),
            _reading("no-budget", duration_ms=99_000, execution_timeout=None),
        ]
    )

    code, summary = _from_projection(projection, capsys)

    assert code == 2
    assert summary["deployed_lane_completed"] == 0


def test_state_root_cannot_be_combined_with_the_projection_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit):
        runtime_budget_readback.main(
            ["--source", "projection", "--state-root", str(tmp_path)]
        )


def test_an_unset_dsn_variable_is_unrunnable_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = load_budget_readback_source()
    monkeypatch.delenv(source.dsn_env, raising=False)

    code = runtime_budget_readback.main(["--until", NOW.isoformat()])
    summary = json.loads(capsys.readouterr().out)

    assert code == 3
    assert source.dsn_env in summary["error"]
    assert source.relation in summary["error"]


def test_the_contract_names_the_relation_and_variable_but_no_host() -> None:
    source = load_budget_readback_source()

    assert source.relation == "omninode_internal.delegation_budget_readings"
    assert source.dsn_env == "OMNINODE_INTERNAL_DB_URL"
    assert source.populated_by.endswith("node_projection_delegation_budget_readings")


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, query: object, params: tuple[object, ...]) -> None:
        self.connection.executed.append((query, params))
        if self.connection.error is not None:
            raise self.connection.error

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.connection.rows


class _Connection:
    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.executed: list[tuple[object, tuple[object, ...]]] = []
        self.closed = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def _projection_source(
    monkeypatch: pytest.MonkeyPatch, connection: _Connection, dsn: str
) -> ProjectionBudgetReadingSource:
    source = load_budget_readback_source()
    monkeypatch.setenv(source.dsn_env, dsn)
    seen: list[str] = []

    def connect(value: str) -> _Connection:
        seen.append(value)
        return connection

    monkeypatch.setattr(
        "omnimarket.projection.postgres_read_database.connect_read_only", connect
    )
    projection = ProjectionBudgetReadingSource(source)
    assert seen == []
    return projection


def test_the_projection_read_asks_for_completed_terminals_in_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(
        rows=[("run-a", "completed", 50, 50, 65_001)],
    )
    projection = _projection_source(monkeypatch, connection, "postgresql://h/db")

    readings = projection.read(NOW - timedelta(hours=24), NOW)

    assert readings == [_reading("run-a", duration_ms=65_001)]
    ((query, params),) = connection.executed
    rendered = str(query)
    assert "delegation_budget_readings" in rendered
    assert "observed_at >= " in rendered
    assert "observed_at < " in rendered
    assert "status = 'completed'" in rendered
    assert params == (NOW - timedelta(hours=24), NOW)
    assert connection.closed


def test_a_database_error_is_unrunnable_and_never_prints_the_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = "postgresql://reader:hunter2@lab-host:5432/db"
    connection = _Connection(error=RuntimeError(f"could not connect using {dsn}"))
    projection = _projection_source(monkeypatch, connection, dsn)

    code = runtime_budget_readback.main(
        ["--until", NOW.isoformat()], projection=projection
    )
    printed = capsys.readouterr().out

    assert code == 3
    assert "hunter2" not in printed
    assert dsn not in printed
    assert connection.closed


def test_a_terminal_the_lane_published_reaches_the_verdict_through_the_fold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The chain without a database: terminal -> projection row -> readback exit."""
    from omnibase_core.models.delegation.wire.model_delegation_budget_evidence import (
        ModelDelegationBudgetEvidence,
    )

    from omnimarket.models.delegation.wire.model_delegate_skill_response import (
        ModelDelegateSkillCompleted,
    )
    from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_delegation_budget_readings_writer import (
        event_from_delivery,
    )
    from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_projection_delegation_budget_readings import (
        HandlerProjectionDelegationBudgetReadings,
    )
    from omnimarket.nodes.node_projection_delegation_budget_readings.models import (
        ModelDelegationBudgetReadingProjectionRequest,
    )

    def project(correlation_id: UUID, duration_ms: int) -> ModelBudgetReading:
        terminal = ModelDelegateSkillCompleted(
            correlation_id=correlation_id,
            task_type="research",
            execution_duration_ms=duration_ms,
            budget_evidence=ModelDelegationBudgetEvidence(
                requested_timeout_seconds=50,
                task_class_timeout_ceiling_seconds=240,
                execution_timeout_seconds=50,
                terminal_delivery_margin_seconds=60,
            ),
        ).model_dump(mode="json")
        event = event_from_delivery(
            {**terminal, "_envelope_timestamp": (NOW - timedelta(hours=1)).isoformat()}
        )
        row = (
            HandlerProjectionDelegationBudgetReadings()
            .handle(ModelDelegationBudgetReadingProjectionRequest(event=event))
            .row
        )
        return ModelBudgetReading(
            run_id=str(row.correlation_id),
            status=row.status,
            requested_timeout_seconds=row.requested_timeout_seconds,
            execution_timeout_seconds=row.execution_timeout_seconds,
            execution_duration_ms=row.execution_duration_ms,
        )

    inside = UUID("00000000-0000-4000-8000-000000000001")
    over = UUID("00000000-0000-4000-8000-000000000002")

    code, summary = _from_projection(
        _Projection([project(inside, 65_000), project(over, 65_001)]), capsys
    )

    assert code == 1
    assert [run["run_id"] for run in summary["over_runs"]] == [str(over)]
