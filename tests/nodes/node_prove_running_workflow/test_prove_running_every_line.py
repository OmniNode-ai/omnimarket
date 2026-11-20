# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly batch reads every NOT RUNNING line of the integration projection, with no cap, page size or layer cut.

The hourly run on h051 read 3 changes of a projection that held 245 NOT RUNNING lines: the contract declared a start
limit of 3 and the starter stopped there. These tests feed more lines than 3, in both layer groups, through the step
input the schedule hands the read step.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import WorkflowEnvironment

from omnimarket.handlers.floor_projection import InMemorySnapshotSource
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers.handler_integration_projection import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_prove_running_workflow import (
    runtime_prove_running as runtime,
)
from omnimarket.nodes.node_prove_running_workflow.handlers import (
    HandlerProveRunningLineRows,
    HandlerProveRunningRows,
)
from omnimarket.nodes.node_prove_running_workflow.models import (
    EnumProveRunningLineCase,
    EnumProveRunningOutcome,
    ModelProveRunningAnswer,
    ModelProveRunningChange,
    ModelProveRunningEarlier,
    ModelProveRunningLineRowRequest,
    ModelProveRunningResult,
    ModelProveRunningRowRequest,
    ModelProveRunningStartBatch,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_batch import (
    _run_batch,
    _worker,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import FAKE
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    env as env,
)
from tests.nodes.node_prove_running_workflow.test_prove_running_workflow import (
    files as files,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.snapshot.projection.floor-integration.v1"
MERGED = datetime(2026, 11, 16, 9, tzinfo=UTC)

# Layer 0 lines target the runtime itself; the lines above layer 0 target units, hosts and registrations.
LAYER_0_TARGETS = ("dev runtime", "dev runtime (bus)")
ABOVE_LAYER_0_TARGETS = ("h051 unit", "a schedule the node installs", "h053 worker")


def not_running_lines(count: int) -> tuple[ModelIntegrationReading, ...]:
    lines = []
    for index in range(count):
        layer_0 = index % 2 == 0
        targets = LAYER_0_TARGETS if layer_0 else ABOVE_LAYER_0_TARGETS
        lines.append(
            ModelIntegrationReading(
                repository="omnimarket",
                number=7000 + index,
                title=f"Change {7000 + index}",
                merged_at=MERGED + timedelta(minutes=index),
                target=targets[index % len(targets)],
                state=EnumIntegrationState.NOT_RUNNING,
                gap_class=(
                    EnumIntegrationGapClass.RUNTIME_BEHIND
                    if layer_0
                    else EnumIntegrationGapClass.NEEDS_UNIT
                ),
                gap="not in the build" if layer_0 else "unit missing",
                step="roll dev" if layer_0 else "install the unit",
                owner="" if layer_0 else "lane-owner",
                read_at=MERGED,
                origin="test-pass",
            )
        )
    return tuple(lines)


def projection_publication(
    readings: tuple[ModelIntegrationReading, ...], as_of: datetime
) -> bytes:
    projection = HandlerIntegrationProjection().handle(
        ModelIntegrationReadingBatch(as_of=as_of, origin="test-pass", readings=readings)
    )
    return (
        ModelFloorProjectionEnvelope(
            projection_id="integration",
            as_of=as_of,
            producer="node_integration_reading_publish_effect:test-pass",
            payload=projection.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode()
    )


def declare_projection(
    monkeypatch: pytest.MonkeyPatch, readings: tuple[ModelIntegrationReading, ...]
) -> None:
    held = {TOPIC: [projection_publication(readings, datetime.now(UTC))]}
    monkeypatch.setattr(
        runtime, "snapshot_source", lambda *_args: InMemorySnapshotSource(held)
    )
    monkeypatch.setattr(
        runtime,
        "projection_overlay",
        lambda *_args: ModelFloorProjectionReadOverlay.model_validate(
            {
                "bus": "inmemory",
                "read_window_s": 1.0,
                "projections": {"integration": {"topic": TOPIC, "max_age_s": 7800}},
            }
        ),
    )
    monkeypatch.setenv(
        "ONEX_PROVE_RUNNING_MERGE_SHA_ARGV",
        json.dumps([sys.executable, FAKE, "merge-sha"]),
    )


def schedule_step_input() -> str:
    """The read step's stdin as the schedule's plan declares it: whatever limits the plan carries ride along."""
    plan = runtime.build_batch_plan(runtime.contract_config())
    step: dict[str, object] = {
        "step": "read_not_running",
        "step_key": "batch/read_not_running",
        "batch_id": "batch",
    }
    if "limit" in type(plan).model_fields:
        step["limit"] = plan.model_dump()["limit"]
    return json.dumps(step)


@pytest.mark.parametrize("count", [4, 12, 61])
def test_the_read_step_reads_every_not_running_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    count: int,
) -> None:
    lines = not_running_lines(count)
    assert {line.target for line in lines} & set(LAYER_0_TARGETS)
    assert {line.target for line in lines} & set(ABOVE_LAYER_0_TARGETS)
    declare_projection(monkeypatch, lines)
    monkeypatch.setattr(sys, "stdin", io.StringIO(schedule_step_input()))
    assert runtime.main(["read-not-running"]) == 0
    selected = ModelProveRunningStartBatch.model_validate_json(capsys.readouterr().out)
    assert len(selected.changes) == count
    assert {c.number for c in selected.changes} == {7000 + i for i in range(count)}


# --- one result row per line, in each of the three kinds, and no agent --------------------------------------------

KINDS = ("proven", "blocked", "next-step")
LINES = 7  # above 3, in both layer groups: indexes 0, 2, 4, 6 are layer 0 (no owner); 1, 3, 5 are above (an owner)


def assert_one_row_each(rows: list[str], keys: list[str]) -> None:
    """Every line has exactly one result row, and every row is one of the three kinds."""
    for key in keys:
        mine = [r for r in rows if f" | pr={key} | " in r]
        assert len(mine) == 1, f"{key} has {len(mine)} result rows"
        assert sum(f" | result={kind} | " in mine[0] for kind in KINDS) == 1
    assert len(rows) == len(keys)


def test_the_row_check_fails_on_zero_rows_and_on_two_rows() -> None:
    keys = ["omnimarket#1", "omnimarket#2"]
    row = "t | STATUS | result=proven | pr={} | x"
    assert_one_row_each([row.format(k) for k in keys], keys)
    with pytest.raises(AssertionError, match="omnimarket#2 has 0 result rows"):
        assert_one_row_each([row.format(keys[0])], keys)
    with pytest.raises(AssertionError, match="omnimarket#1 has 2 result rows"):
        assert_one_row_each([row.format(k) for k in (*keys, keys[0])], keys)


@pytest.fixture
def many_lines(
    files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    published = tmp_path / "integration-publication.json"
    published.write_bytes(
        projection_publication(not_running_lines(LINES), datetime.now(UTC))
    )
    monkeypatch.setenv("FAKE_INTEGRATION_PUBLICATION", str(published))
    monkeypatch.setenv(
        "ONEX_PROVE_RUNNING_MERGE_SHA_ARGV",
        json.dumps([sys.executable, FAKE, "merge-sha"]),
    )
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "1")
    return files


KEYS = [f"omnimarket#{7000 + i}" for i in range(LINES)]


def _rows(many_lines: dict[str, Path]) -> list[str]:
    return many_lines["ledger"].read_text(encoding="utf-8").splitlines()


async def test_every_line_read_is_proven_with_its_reading(
    env: WorkflowEnvironment, many_lines: dict[str, Path]
) -> None:
    async with _worker(env.client):
        result = await _run_batch(env.client, "batch-all-proven")
    assert result.lines_read == LINES
    assert result.results_written == LINES
    assert set(result.results) == set(KEYS)
    assert set(result.results.values()) == {"proven"}
    rows = _rows(many_lines)
    assert_one_row_each(rows, KEYS)
    for row in rows:
        assert " | result=proven | " in row
        assert "installed=" in row
        assert "observed=dispatch seen in the runtime log" in row


async def test_a_change_that_never_runs_is_blocked_with_its_owner_or_one_next_step(
    env: WorkflowEnvironment,
    many_lines: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "0")
    async with _worker(env.client):
        result = await _run_batch(env.client, "batch-none-run")
    assert result.lines_read == result.results_written == LINES
    rows = _rows(many_lines)
    assert_one_row_each(rows, KEYS)
    for index, key in enumerate(KEYS):
        (row,) = [r for r in rows if f" | pr={key} | " in r]
        if index % 2:  # above layer 0: the line names an owner
            assert " | result=blocked | owner=lane-owner | " in row
            assert result.results[key] == "blocked"
        else:  # layer 0: the line names its next step
            assert " | result=next-step | next_step=roll-dev | " in row
            assert result.results[key] == "next-step"


async def test_a_second_batch_answers_each_started_line_from_the_earlier_workflow(
    env: WorkflowEnvironment,
    many_lines: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RUNNING_AFTER", "0")
    async with _worker(env.client):
        await _run_batch(env.client, "batch-first")
        second = await _run_batch(env.client, "batch-second")
    assert second.started == ()
    assert second.lines_read == second.results_written == LINES
    rows = [r for r in _rows(many_lines) if "batch=batch-second" in r]
    assert_one_row_each(rows, KEYS)
    for row in rows:
        assert " | case=already-started | " in row
        assert " | earlier_status=COMPLETED | earlier_outcome=alarm | " in row


async def test_a_line_whose_merge_sha_is_unread_still_gets_its_row(
    env: WorkflowEnvironment, many_lines: dict[str, Path], tmp_path: Path
) -> None:
    unread = (
        *not_running_lines(2),
        ModelIntegrationReading(
            repository="omnibase_infra",  # the stand-in answers no sha for any repository but omnimarket
            number=5990,
            title="Change 5990",
            merged_at=MERGED,
            target="h051 unit",
            state=EnumIntegrationState.NOT_RUNNING,
            gap_class=EnumIntegrationGapClass.NEEDS_UNIT,
            gap="unit missing",
            step="install the unit",
            owner="infra-owner",
            read_at=MERGED,
            origin="test-pass",
        ),
        ModelIntegrationReading(
            repository="omnibase_infra",
            number=5991,
            title="Change 5991",
            merged_at=MERGED,
            target="h051 unit",
            state=EnumIntegrationState.NOT_RUNNING,
            gap_class=EnumIntegrationGapClass.NEEDS_UNIT,
            gap="unit missing",
            step="install the unit",
            read_at=MERGED,
            origin="test-pass",
        ),
    )
    Path(os.environ["FAKE_INTEGRATION_PUBLICATION"]).write_bytes(
        projection_publication(unread, datetime.now(UTC))
    )
    async with _worker(env.client):
        result = await _run_batch(env.client, "batch-unread")
    assert result.lines_read == result.results_written == 4
    assert result.read_changes == 2
    rows = _rows(many_lines)
    keys = [
        "omnimarket#7000",
        "omnimarket#7001",
        "omnibase_infra#5990",
        "omnibase_infra#5991",
    ]
    assert_one_row_each(rows, keys)
    (blocked,) = [r for r in rows if "pr=omnibase_infra#5990 " in r]
    assert " | result=blocked | " in blocked
    assert " | owner=infra-owner | " in blocked
    assert " | case=merge-sha-unread | " in blocked
    (step,) = [r for r in rows if "pr=omnibase_infra#5991 " in r]
    assert " | result=next-step | " in step
    assert " | next_step=install-the-unit | " in step


def _answer(running: bool) -> ModelProveRunningAnswer:
    return ModelProveRunningAnswer(
        installed=True,
        running=running,
        host="dev-runtime",
        revision="abcdef012345",
        observed="dispatch seen",
        read_at="2026-11-20T01:00:00Z",
    )


CHANGE = ModelProveRunningChange(
    repository="omnimarket", number=1, merge_sha="a" * 40, target="dev-runtime"
)


def _line(
    case: EnumProveRunningLineCase, **cells: Any
) -> ModelProveRunningLineRowRequest:
    return ModelProveRunningLineRowRequest(
        case=case,
        key="omnimarket#1",
        target="dev-runtime",
        workflow_id=CHANGE.workflow_id,
        step_key="batch/write",
        lane="prove-running",
        ticket="OMN-24901",
        written_at="2026-11-20T02:00:00Z",
        **cells,
    )


def _earlier(
    status: str, outcome: EnumProveRunningOutcome | None, running: bool = True
) -> ModelProveRunningEarlier:
    return ModelProveRunningEarlier(
        workflow_id=CHANGE.workflow_id,
        status=status,
        result=None
        if outcome is None
        else ModelProveRunningResult(
            workflow_id=CHANGE.workflow_id,
            outcome=outcome,
            readings=1,
            installed_by_workflow=False,
            last_reading=_answer(running),
        ),
    )


def test_an_already_started_change_carries_its_earlier_proof_and_says_so() -> None:
    row = (
        HandlerProveRunningLineRows()
        .handle(
            _line(
                EnumProveRunningLineCase.ALREADY_STARTED,
                earlier=_earlier("COMPLETED", EnumProveRunningOutcome.PROVED),
            )
        )
        .row
    )
    assert (
        " | result=proven | pr=omnimarket#1 | installed=dev-runtime:abcdef012345 | "
        in row
    )
    assert "observed=dispatch seen | read_at=2026-11-20T01:00:00Z" in row
    assert f"earlier_proof={CHANGE.workflow_id} " in row
    assert "was already proven by workflow" in row


def test_an_already_running_change_is_a_next_step_not_a_skip() -> None:
    row = (
        HandlerProveRunningLineRows()
        .handle(
            _line(
                EnumProveRunningLineCase.ALREADY_STARTED,
                owner="someone",
                earlier=_earlier("RUNNING", None),
            )
        )
        .row
    )
    assert " | result=next-step | " in row
    assert "earlier_status=RUNNING" in row


def test_the_alarm_row_of_a_child_is_blocked_with_its_owner_or_a_next_step() -> None:
    def alarm(change: ModelProveRunningChange) -> str:
        return (
            HandlerProveRunningRows()
            .handle(
                ModelProveRunningRowRequest(
                    outcome=EnumProveRunningOutcome.ALARM,
                    change=change,
                    reading=_answer(False),
                    step_key="wf/write_alarm",
                    lane="prove-running",
                    ticket="OMN-24901",
                    written_at="2026-11-20T02:00:00Z",
                )
            )
            .row
        )

    blocked = alarm(CHANGE.model_copy(update={"owner": "lane-x"}))
    assert " | result=blocked | owner=lane-x | src=alarm | " in blocked
    step = alarm(CHANGE.model_copy(update={"next_step": "roll dev"}))
    assert " | result=next-step | next_step=roll-dev | src=alarm | " in step
    default = alarm(CHANGE)
    assert " | result=next-step | next_step=roll-dev-runtime-to-aaaaaaaaaaaa" in default


# --- no Claude agent --------------------------------------------------------------------------------------------


def _is_agent_spawn(argv: tuple[object, ...]) -> bool:
    names = [Path(str(a)).name.lower() for a in argv]
    return any(
        n in ("claude", "claude-code", "codex") or n.startswith("claude-")
        for n in names
    )


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Every process the code could start, and every agent entry point, recorded before the real call goes through."""
    seen: list[tuple[object, ...]] = []

    real_popen_init = subprocess.Popen.__init__
    real_system = os.system

    def popen_init(self: subprocess.Popen[bytes], args: Any, *a: Any, **k: Any) -> None:
        seen.append(tuple(args) if isinstance(args, (list, tuple)) else (args,))
        real_popen_init(self, args, *a, **k)

    def system(command: str) -> int:
        seen.append((command,))
        return real_system(command)

    real_exec = asyncio.create_subprocess_exec

    async def exec_wrapper(*args: Any, **kwargs: Any) -> Any:
        seen.append(tuple(args))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_wrapper)
    monkeypatch.setattr(subprocess.Popen, "__init__", popen_init)
    monkeypatch.setattr(os, "system", system)
    agent_calls: list[tuple[object, ...]] = []
    sdk = types.ModuleType("claude_agent_sdk")

    def entry(*args: object, **_kwargs: object) -> None:
        agent_calls.append(args)

    for name in ("query", "ClaudeSDKClient", "Agent"):
        setattr(sdk, name, entry)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    seen.append(("agent-calls", agent_calls))
    return seen


async def test_the_workflow_starts_no_claude_agent_for_any_change(
    env: WorkflowEnvironment,
    many_lines: dict[str, Path],
    spawns: list[tuple[object, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FAKE_RUNNING_AFTER", "0"
    )  # alarms, the hard case: still a row, never an agent
    async with _worker(env.client):
        await _run_batch(env.client, "batch-no-agent-1")
        result = await _run_batch(env.client, "batch-no-agent-2")
    assert result.lines_read == result.results_written == LINES
    processes = [s for s in spawns if s and s[0] != "agent-calls"]
    assert (
        len(processes) > LINES
    )  # the stand-in node commands ran through the patched spawn points
    assert not [s for s in processes if _is_agent_spawn(s)]
    (agent_calls,) = [s[1] for s in spawns if s and s[0] == "agent-calls"]
    assert agent_calls == []


async def test_the_no_agent_check_sees_a_claude_spawn(
    spawns: list[tuple[object, ...]],
) -> None:
    """Positive control: the same patched spawn points do record a claude process when one is started."""
    with pytest.raises(FileNotFoundError):
        await asyncio.create_subprocess_exec("claude-not-installed-control")
    with pytest.raises(FileNotFoundError):
        subprocess.run(["/nonexistent/bin/claude-control"], check=False)
    caught = [s for s in spawns if s and s[0] != "agent-calls" and _is_agent_spawn(s)]
    assert ("claude-not-installed-control",) in caught
    assert ("/nonexistent/bin/claude-control",) in caught


def test_the_node_package_names_no_agent_launcher() -> None:
    package = Path(runtime.__file__).parent
    for path in [*package.rglob("*.py"), *package.glob("*.yaml")]:
        text = path.read_text(encoding="utf-8").lower()
        for banned in ("claude", "anthropic", "subagent", "codex exec"):
            assert banned not in text, f"{path.name} names {banned}"
