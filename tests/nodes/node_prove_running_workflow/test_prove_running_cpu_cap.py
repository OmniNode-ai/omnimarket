# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The prove-running worker no longer saturates a lab host (OMN-24901).

Measured on h051 (the 23:00Z hourly pass): 454 of 476 lines were already started, each ended in its own
``write-line-row`` process plus its own append process, and the worker ran 99 activities at once (the SDK's default is
100). These tests pin the three fixes: one append for the rows of a pass (chunked only at the bus request's own size
cap), a declared bound on concurrent activities, and a declared CPU quota on the worker's unit.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import WorkflowEnvironment

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_prove_running_workflow import (
    runtime_prove_running as runtime,
)
from omnimarket.nodes.node_prove_running_workflow.handlers.handler_prove_running_line_rows import (
    HandlerProveRunningLineRows,
    write_line_rows,
)
from omnimarket.nodes.node_prove_running_workflow.models import (
    EnumProveRunningLineCase,
    ModelProveRunningBatchStepInput,
    ModelProveRunningLinePayload,
    ModelProveRunningLineRowRequest,
    ModelProveRunningLinesPayload,
    ModelProveRunningLinesWritten,
)
from tests.nodes.node_prove_running_workflow import (
    test_prove_running_batch as batch_tests,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_batch import (
    _run_batch,
    _worker,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_batch import (
    batch_files as batch_files,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    env as env,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    files as files,
)

pytestmark = pytest.mark.unit

LANE = "prove-running"
TICKET = "OMN-24901"
WRITTEN_AT = "2026-11-24T23:00:00Z"
STEP_KEY = "batch-x/write_line_result"
ROW_STAMP_CHARS = len(" | req=") + 36  # the cell the bus append stamps on every row


def _payloads(count: int) -> list[ModelProveRunningLinePayload]:
    return [
        ModelProveRunningLinePayload(
            case=EnumProveRunningLineCase.UNRESOLVED
            if index % 2
            else EnumProveRunningLineCase.CHILD_FAILED,
            key=f"omnimarket#{9000 + index}",
            target="h051 unit",
            owner="lane-owner" if index % 3 == 0 else "",
            workflow_id=f"omnimarket#{9000 + index}@{index:040x}",
            merge_sha=f"{index:040x}",
            detail="merge sha unread",
        )
        for index in range(count)
    ]


def _step_json(
    payloads: list[ModelProveRunningLinePayload], written_at: str = ""
) -> str:
    return ModelProveRunningBatchStepInput(
        step="write_line_result",
        step_key=STEP_KEY,
        batch_id="batch-x",
        payload=ModelProveRunningLinesPayload(lines=tuple(payloads)).model_dump_json(),
        written_at=written_at,
    ).model_dump_json()


def _one_row_each_as_before(payloads: list[ModelProveRunningLinePayload]) -> list[str]:
    """The row each line got when it was written alone: step key ``<batch>/write_line_result/<case>/<key>``."""
    return [
        HandlerProveRunningLineRows()
        .handle(
            ModelProveRunningLineRowRequest(
                **p.model_dump(),
                step_key=f"{STEP_KEY}/{p.case.value}/{p.key}",
                lane=LANE,
                ticket=TICKET,
                written_at=WRITTEN_AT,
                batch_id="batch-x",
            )
        )
        .row
        for p in payloads
    ]


def test_the_rows_of_one_pass_go_in_one_append_in_input_order() -> None:
    payloads = _payloads(40)
    appended: list[tuple[str, str, str]] = []

    def append(rows: str, step_key: str, lane: str) -> tuple[int, str]:
        appended.append((rows, step_key, lane))
        return 0, ""

    code, text = write_line_rows(
        _step_json(payloads),
        lane=LANE,
        ticket=TICKET,
        written_at=WRITTEN_AT,
        append=append,
        max_chars=60000,
    )
    assert code == 0
    assert len(appended) == 1
    rows, step_key, lane = appended[0]
    assert (step_key, lane) == (STEP_KEY, LANE)
    assert rows.splitlines() == _one_row_each_as_before(payloads)
    written = ModelProveRunningLinesWritten.model_validate_json(text)
    assert written.rows == 40
    assert written.appends == 1
    assert len(written.results) == 40


def test_a_pass_larger_than_the_bus_request_is_split_in_order_at_the_declared_size() -> (
    None
):
    payloads = _payloads(40)
    expected = _one_row_each_as_before(payloads)
    row_chars = max(len(row) for row in expected) + ROW_STAMP_CHARS
    limit = row_chars * 7  # at most seven rows to a request
    appended: list[tuple[str, str]] = []

    def append(rows: str, step_key: str, lane: str) -> tuple[int, str]:
        appended.append((rows, step_key))
        return 0, ""

    code, text = write_line_rows(
        _step_json(payloads),
        lane=LANE,
        ticket=TICKET,
        written_at=WRITTEN_AT,
        append=append,
        max_chars=limit,
    )
    assert code == 0
    assert len(appended) == math.ceil(40 / 7)
    assert [row for rows, _ in appended for row in rows.splitlines()] == expected
    for rows, _ in appended:
        assert sum(len(r) + ROW_STAMP_CHARS for r in rows.splitlines()) <= limit
    assert len({key for _, key in appended}) == len(appended)  # one request id each
    assert ModelProveRunningLinesWritten.model_validate_json(text).appends == len(
        appended
    )


def test_an_append_refusal_keeps_its_reason_and_exit_code() -> None:
    def refuse(rows: str, step_key: str, lane: str) -> tuple[int, str]:
        return 1, "ledger bus append refused: the writer is not the broker login"

    code, text = write_line_rows(
        _step_json(_payloads(3)),
        lane=LANE,
        ticket=TICKET,
        written_at=WRITTEN_AT,
        append=refuse,
        max_chars=60000,
    )
    assert code == 1
    assert (
        text
        == "ledger append exited 1: ledger bus append refused: the writer is not the broker login"
    )


def test_a_retried_step_writes_the_same_rows_so_the_bus_answers_one_request_once() -> (
    None
):
    """A step delivered twice must hand the bus the same rows: the time the workflow gave it rides in the input."""
    payloads = _payloads(5)
    seen: list[str] = []

    def append(rows: str, step_key: str, lane: str) -> tuple[int, str]:
        seen.append(rows)
        return 0, ""

    for clock in ("2026-11-24T23:00:01Z", "2026-11-24T23:09:59Z"):
        write_line_rows(
            _step_json(payloads, written_at="2026-11-24T23:00:00Z"),
            lane=LANE,
            ticket=TICKET,
            written_at=clock,
            append=append,
            max_chars=60000,
        )
    assert seen[0] == seen[1]
    assert seen[0].splitlines()[0].startswith("2026-11-24T23:00:00Z | STATUS | ")


async def test_a_batch_runs_one_row_step_however_many_lines_it_answers(
    env: WorkflowEnvironment, batch_files: dict[str, Path]
) -> None:
    async with _worker(env.client):
        await _run_batch(env.client, "batch-first")
        second = await _run_batch(env.client, "batch-second")
    assert second.started == ()
    log = batch_files["log"].read_text(encoding="utf-8").splitlines()
    mine = [line for line in log if line.startswith("write-line-row batch-second/")]
    assert mine == ["write-line-row batch-second/write_line_result"]
    rows = [
        r
        for r in batch_files["ledger"].read_text(encoding="utf-8").splitlines()
        if "batch=batch-second" in r
    ]
    assert [r.split(" | pr=")[1].split(" | ")[0] for r in rows] == [
        wf.split("@")[0] for wf in batch_tests.WORKFLOW_IDS
    ]


# --- the worker's bound on concurrent activities ---------------------------------------------------------------


def test_the_worker_bounds_its_concurrent_activities_by_a_declared_setting() -> None:
    cfg = runtime.contract_config()
    declared = cfg["worker"]["max_concurrent_activities"]
    assert isinstance(declared, int)
    assert 1 <= declared <= 16  # the measured peak was 99 under the SDK default of 100
    assert runtime.worker_options(cfg)["max_concurrent_activities"] == declared


@pytest.mark.parametrize("bad", [0, -1, "8", None, True])
def test_a_worker_bound_that_is_not_a_positive_integer_is_refused(bad: Any) -> None:
    cfg = runtime.contract_config()
    cfg["worker"]["max_concurrent_activities"] = bad
    with pytest.raises(ValueError, match="max_concurrent_activities"):
        runtime.worker_options(cfg)


async def test_run_worker_hands_the_bound_to_the_sdk_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, client: object, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self) -> None:
            return None

    async def _connect(target: str, namespace: str) -> object:
        return object()

    monkeypatch.setattr("temporalio.worker.Worker", _Recorder)
    monkeypatch.setattr(runtime, "_connect", _connect)
    await runtime.run_worker("127.0.0.1:7233", "default", "q")
    declared = runtime.contract_config()["worker"]["max_concurrent_activities"]
    assert captured["max_concurrent_activities"] == declared


# --- the worker unit's CPU quota ---------------------------------------------------------------------------------


def _render(unit: dict[str, Any], tmp_path: Path) -> str:
    from tests.nodes.node_host_installer_registry_units import (
        test_registry_units_systemd as base,
    )
    from tests.nodes.node_host_installer_registry_units.test_service_type_unit import (
        request,
    )

    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        request(tmp_path, "temporal-worker-systemd"),
        unit,
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    return service


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.nodes.node_host_installer_registry_units import (
        test_registry_units_systemd as base,
    )

    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_worker_unit_renders_the_declared_cpu_quota(
    tmp_path: Path, linux_host: None
) -> None:
    unit = load_units()["temporal-worker-systemd"]
    declared = unit["cpu_quota_percent"]
    assert isinstance(declared, int)
    assert 100 <= declared <= 1600
    assert f"\nCPUQuota={declared}%" in _render(unit, tmp_path)
    assert f"\nCPUQuota={declared + 100}%" in _render(
        unit | {"cpu_quota_percent": declared + 100}, tmp_path
    )


def test_a_unit_declaring_no_cpu_quota_renders_none(
    tmp_path: Path, linux_host: None
) -> None:
    unit = load_units()["temporal-worker-systemd"]
    assert "CPUQuota" not in _render(unit | {"cpu_quota_percent": None}, tmp_path)


@pytest.mark.parametrize("bad", [0, -5, "800%", 1.5, True])
def test_a_cpu_quota_that_is_not_a_positive_integer_is_refused(
    tmp_path: Path, linux_host: None, bad: Any
) -> None:
    unit = load_units()["temporal-worker-systemd"]
    with pytest.raises(ValueError, match="cpu_quota_percent"):
        _render(unit | {"cpu_quota_percent": bad}, tmp_path)
