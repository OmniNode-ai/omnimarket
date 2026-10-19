# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hourly tick behavior ported from the Workflow implementation (OMN-20680)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_hourly_tick_effect.handlers.handler_hourly_tick import (
    HandlerHourlyTick,
    build_record_request,
    checkpoint_line,
    completion_line,
    facts_from_tail,
    result_payload,
    tick_outcome,
)
from omnimarket.nodes.node_hourly_tick_effect.handlers.handler_hourly_tick_cli import (
    main as cli_main,
)
from omnimarket.nodes.node_hourly_tick_effect.models.model_hourly_tick import (
    ModelHourlyTickRequest,
    ModelHourlyTickResult,
    ModelSweepResult,
    ModelTickState,
)

PARITY = json.loads(
    (Path(__file__).parent / "fixtures/hourly_tick_parity.json").read_text()
)


@pytest.mark.parametrize("case", PARITY["cases"])
def test_old_behavior_parity(case: dict[str, object]) -> None:
    state = ModelTickState.model_validate(case["state"])
    sweep = (
        ModelSweepResult.model_validate(case["sweep"])
        if case["sweep"] is not None
        else None
    )
    assert checkpoint_line(state) == case["checkpoint"]
    assert completion_line(state, sweep, str(case["run_id"])) == case["completion"]
    assert tick_outcome(sweep) == case["outcome"]
    assert result_payload("fire-123", sweep) == case["result"]
    assert facts_from_tail(state.tail, 12) == case["facts"]


def fixture_home(tmp_path: Path) -> Path:
    (tmp_path / "checkpoints.md").write_text(
        "- 2026-09-16T21:27:48Z 60m checkpoint: earlier\n"
        "- 2026-09-16T21:30:00Z operator mentioned 60m checkpoint later\n"
    )
    (tmp_path / "pointer.txt").write_text("2026-09-16T21:20:00Z\n")
    (tmp_path / "ledger.md").write_text(
        "2026-09-16T21:27:48Z | CLAIM | lane=excluded | before\n"
        "2026-09-16T21:30:00Z | TERMINAL | lane=codex:whole ticket=x | prose lane=wrong\n"
        "2026-09-16T21:31:00Z | CLAIM | lane=other | body\n"
        "2026-09-16T21:40:57Z | TERMINAL | lane=last | body\n"
        "2026-09-16T21:40:58Z | CLAIM | lane=excluded | after\n"
    )
    return tmp_path


def request(home: Path, **kwargs: object) -> ModelHourlyTickRequest:
    return ModelHourlyTickRequest.model_validate(
        {
            "registry_root": str(home),
            "checkpointFile": "checkpoints.md",
            "sweepStateFile": "pointer.txt",
            "ledgerFile": "ledger.md",
            "now": "2026-09-16T21:40:57Z",
            "fireId": "fire-123",
            **kwargs,
        }
    )


def test_prepare_and_record_preserve_window_pointer_and_readbacks(
    tmp_path: Path,
) -> None:
    home = fixture_home(tmp_path)
    handler = HandlerHourlyTick()
    prepared = asyncio.run(handler.handle(request(home)))
    assert prepared.state.rows == 3
    assert prepared.state.terminal == 2
    assert prepared.state.lanes == "lane=codex:whole,lane=last"
    assert prepared.state.ledger_total == 5
    assert prepared.sweep_request is not None
    assert prepared.sweep_request.since == "2026-09-16T21:20:00Z"
    assert "operator mentioned" in prepared.sweep_request.facts
    # OMN-20047: the pointer moves only when record accepts a successful read of the window.
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"
    assert (home / "checkpoints.md").read_text().splitlines()[
        -1
    ] == prepared.checkpoint_line
    completed = asyncio.run(
        handler.handle(
            request(
                home,
                operation="record",
                correlation_id=prepared.correlation_id,
                state=prepared.state.model_dump(),
                sweep={
                    "needing": 3,
                    "accepted": 0,
                    "held": 3,
                    "held_by_class": {"transport": 3},
                },
            )
        )
    )
    assert completed.phase == "degraded"
    assert completed.held_class == "transport"
    assert completed.posted is False
    assert "held 3, phase=degraded class=transport" in completed.completion_line
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:40:57Z\n"
    assert completed.readbacks["pointer"] == "READBACK: 2026-09-16T21:40:57Z"
    assert (home / "checkpoints.md").read_text().splitlines()[
        -1
    ] == completed.completion_line
    payload = json.loads(
        (home / ".onex_state/morning-workflows/results/fire-123.json").read_text()
    )
    assert payload["class"] == "transport"
    assert payload["fire_id"] == "fire-123"


@pytest.mark.parametrize(("dry", "skip"), [(True, False), (False, True)])
def test_dry_run_and_skip_sweep(tmp_path: Path, dry: bool, skip: bool) -> None:
    home = fixture_home(tmp_path)
    before = (home / "checkpoints.md").read_text()
    result = asyncio.run(
        HandlerHourlyTick().handle(request(home, dryRun=dry, skipSweep=skip))
    )
    assert result.sweep_request is None
    assert result.sweep_skipped
    assert result.phase == "completed"
    if dry:
        assert (home / "checkpoints.md").read_text() == before
        assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"
        assert not (home / ".onex_state").exists()
    else:
        assert result.completion_line
        assert (home / ".onex_state/morning-workflows/results/fire-123.json").is_file()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("post", True),
        ("post", "true"),
        ("fireId", "../escape"),
        ("now", "bad"),
        ("tailLines", 0),
    ],
)
def test_invalid_inputs_refused_before_io(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=r"hourly-tick|validation error"):
        request(tmp_path, **{field: value})


@pytest.mark.parametrize(
    ("file", "value"),
    [
        ("pointer.txt", "bad"),
        ("ledger.md", "no timestamps"),
        ("checkpoints.md", "no checkpoint"),
    ],
)
def test_probe_refuses_invalid_or_missing_state(
    tmp_path: Path, file: str, value: str
) -> None:
    home = fixture_home(tmp_path)
    (home / file).write_text(value)
    before = (home / "checkpoints.md").read_text()
    with pytest.raises(ValueError, match=r"hourly-tick|validation error"):
        asyncio.run(HandlerHourlyTick().handle(request(home)))
    assert (home / "checkpoints.md").read_text() == before


@pytest.mark.parametrize("tail", [[], ["", " "]])
def test_empty_facts_refused(tail: list[str]) -> None:
    with pytest.raises(ValueError, match="no facts supplied"):
        facts_from_tail(tail, 12)


def test_missing_root_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(ValueError, match="OMNI_HOME"):
        asyncio.run(HandlerHourlyTick().handle(ModelHourlyTickRequest()))


def test_record_requires_state_and_sweep() -> None:
    with pytest.raises(ValueError, match="record requires the prepared state"):
        ModelHourlyTickRequest(operation="record")


def test_real_contract_bus_dispatch(tmp_path: Path) -> None:
    from importlib.metadata import entry_points

    import omnimarket.nodes.node_hourly_tick_effect as package
    from omnimarket.nodes.node_hourly_tick_effect.handlers.handler_hourly_tick_cli import (
        dispatch,
    )
    from omnimarket.nodes.node_hourly_tick_effect.node import NodeHourlyTickEffect

    found = entry_points(group="onex.nodes", name="node_hourly_tick_effect")
    assert len(found) == 1
    assert next(iter(found)).load() is package
    assert (
        NodeHourlyTickEffect.__module__
        == "omnimarket.nodes.node_hourly_tick_effect.node"
    )
    home = fixture_home(tmp_path)
    prepared = dispatch(request(home), tmp_path / "prepare-evidence")
    assert prepared.phase == "prepared"
    result = dispatch(
        request(
            home,
            operation="record",
            correlation_id=prepared.correlation_id,
            state=prepared.state.model_dump(),
            sweep={
                "needing": 2,
                "accepted": 0,
                "held": 2,
                "held_by_class": {"timeout": 2},
            },
        ),
        tmp_path / "record-evidence",
    )
    assert result.phase == "degraded"
    evidence = json.loads(
        (tmp_path / "record-evidence/workflow_result.json").read_text()
    )
    assert evidence["handler_result"]["completion_line"] == result.completion_line
    assert evidence["handler_locus"] == "in_process"
    assert evidence["wire_correlation_id"] == str(result.correlation_id)


def test_record_refuses_caller_modified_prepared_state(tmp_path: Path) -> None:
    home = fixture_home(tmp_path)
    handler = HandlerHourlyTick()
    prepared = asyncio.run(handler.handle(request(home)))
    state = prepared.state.model_dump()
    state["rows"] = 999
    before = (home / "checkpoints.md").read_text()
    with pytest.raises(ValueError, match="prepared"):
        asyncio.run(
            handler.handle(
                request(
                    home,
                    operation="record",
                    correlation_id=prepared.correlation_id,
                    state=state,
                    sweep={"needing": 1, "accepted": 1},
                )
            )
        )
    assert (home / "checkpoints.md").read_text() == before


def failed_record(home: Path, **sweep: object) -> ModelHourlyTickResult:
    handler = HandlerHourlyTick()
    prepared = asyncio.run(handler.handle(request(home, fireId="fire-failed")))
    return asyncio.run(
        handler.handle(
            request(
                home,
                operation="record",
                fireId="fire-failed",
                correlation_id=prepared.correlation_id,
                state=prepared.state.model_dump(),
                sweep={"failed": True, **sweep},
            )
        )
    )


def test_prepare_alone_leaves_the_sweep_pointer(tmp_path: Path) -> None:
    """OMN-20047: a tick that dies between prepare and record must not skip the window."""
    home = fixture_home(tmp_path)
    prepared = asyncio.run(HandlerHourlyTick().handle(request(home)))
    assert prepared.sweep_request is not None
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"
    assert prepared.readbacks["pointer"] is None


def test_failed_linear_read_keeps_the_pointer_and_says_failed(tmp_path: Path) -> None:
    """OMN-20047 AC1 and AC2: a failed read is not a quiet hour and the hour is read again."""
    home = fixture_home(tmp_path)
    completed = failed_record(home, error_class="linear_tooling_failure")
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"
    assert completed.readbacks["pointer"] == "READBACK: 2026-09-16T21:20:00Z"
    line = (home / "checkpoints.md").read_text().splitlines()[-1]
    assert line == completed.completion_line
    assert "FAILED error_class=linear_tooling_failure" in line
    assert "collected" not in line
    assert "sweep since=2026-09-16T21:20:00Z" in line
    assert "phase=degraded class=linear_tooling_failure" in line
    assert completed.phase == "degraded"
    assert completed.held_class == "linear_tooling_failure"
    payload = json.loads(
        (home / ".onex_state/morning-workflows/results/fire-failed.json").read_text()
    )
    assert (payload["phase"], payload["class"]) == (
        "degraded",
        "linear_tooling_failure",
    )


def test_failed_linear_read_without_a_class_is_unclassified(tmp_path: Path) -> None:
    home = fixture_home(tmp_path)
    completed = failed_record(home)
    assert completed.held_class == "unclassified"
    assert "FAILED error_class=unclassified" in completed.completion_line
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"


def test_quiet_window_still_reads_collected_zero_and_advances(tmp_path: Path) -> None:
    """Positive control: a successful read of an empty hour is still a quiet hour."""
    home = fixture_home(tmp_path)
    handler = HandlerHourlyTick()
    prepared = asyncio.run(handler.handle(request(home)))
    completed = asyncio.run(
        handler.handle(
            request(
                home,
                operation="record",
                correlation_id=prepared.correlation_id,
                state=prepared.state.model_dump(),
                sweep={"collected": 0, "needing": 0, "accepted": 0, "held": 0},
            )
        )
    )
    assert completed.phase == "completed"
    assert "collected 0" in completed.completion_line
    assert "FAILED" not in completed.completion_line
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:40:57Z\n"


def test_skipped_sweep_still_advances_the_pointer(tmp_path: Path) -> None:
    home = fixture_home(tmp_path)
    asyncio.run(HandlerHourlyTick().handle(request(home, skipSweep=True)))
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:40:57Z\n"


def test_hourly_tick_delegate_no_consumer_reason_is_the_degraded_class(
    tmp_path: Path,
) -> None:
    """OMN-20764 AC2: a window held for a missing consumer records that class, not transport."""
    home = fixture_home(tmp_path)
    handler = HandlerHourlyTick()
    prepared = asyncio.run(handler.handle(request(home)))
    completed = asyncio.run(
        handler.handle(
            request(
                home,
                operation="record",
                correlation_id=prepared.correlation_id,
                state=prepared.state.model_dump(),
                sweep={
                    "needing": 3,
                    "accepted": 0,
                    "held": 3,
                    "held_by_class": {"no_consumer": 2, "transport": 1},
                },
            )
        )
    )
    assert completed.phase == "degraded"
    assert completed.held_class == "no_consumer"
    assert "phase=degraded class=no_consumer" in completed.completion_line


# OMN-19854: the record request is built by the tick's own command from the prepared result and
# the sweep return it just produced; the agent never retypes the facts string, the tail or the
# sweep fields. Four hourly-tick sessions were refused at the record step because the agent
# retyped that request: it shortened the facts string, dropped tail entries of the state, or
# added a checkpoint_line field to it. The refused requests are rebuilt here from one prepare.

SWEEP_RETURN = {
    "linear_file": "docs/linear-comments-hourly.md",
    "since": "2026-09-16T21:20:00Z",
    "collected": 4,
    "needing": 2,
    "refused": 2,
    "accepted": 2,
    "held": 0,
    "held_by_class": {},
    "degraded": False,
    "failed": False,
    "written": "wrote the report",
}


def prepared_fire(home: Path) -> ModelHourlyTickResult:
    return asyncio.run(HandlerHourlyTick().handle(request(home, tailLines=3)))


def refused_requests(prepared: ModelHourlyTickResult) -> dict[str, dict[str, object]]:
    """The three retyped requests, each shaped like the rejection the agent produced."""
    assert prepared.sweep_request is not None
    full = json.loads(prepared.state.model_dump_json())
    base: dict[str, object] = {
        "operation": "record",
        "correlation_id": str(prepared.correlation_id),
        "fireId": "fire-123",
        "registry_root": None,
        "sweep": SWEEP_RETURN,
    }
    return {
        "abbreviated-facts": {
            **base,
            "state": full,
            "facts": prepared.sweep_request.facts.splitlines()[0],
        },
        "dropped-tail-entries": {**base, "state": {**full, "tail": full["tail"][:1]}},
        "extra-checkpoint-line": {
            **base,
            "state": {**full, "checkpoint_line": prepared.checkpoint_line},
        },
    }


def test_retyped_record_requests_are_still_refused(tmp_path: Path) -> None:
    """The record validator stays strict: a retyped request that differs from the prepare is out."""
    home = fixture_home(tmp_path)
    prepared = prepared_fire(home)
    before = (home / "checkpoints.md").read_text()
    refused = refused_requests(prepared)
    with pytest.raises(ValueError, match="checkpoint_line"):
        ModelHourlyTickRequest.model_validate(refused["extra-checkpoint-line"])
    with pytest.raises(ValueError, match="prepared"):
        asyncio.run(
            HandlerHourlyTick().handle(
                ModelHourlyTickRequest.model_validate(refused["dropped-tail-entries"])
            )
        )
    assert (home / "checkpoints.md").read_text() == before


def test_command_builds_the_record_request_the_agent_got_refused_on(
    tmp_path: Path,
) -> None:
    home = fixture_home(tmp_path)
    prepared = prepared_fire(home)
    assert prepared.sweep_request is not None
    built = build_record_request(prepared, json.dumps(SWEEP_RETURN), "fire-123")
    assert built.operation == "record"
    assert built.fire_id == "fire-123"
    assert built.correlation_id == prepared.correlation_id
    assert built.state == prepared.state
    assert built.state is not None
    assert built.state.tail == prepared.state.tail
    assert len(built.state.tail) == 2
    assert built.facts == prepared.sweep_request.facts
    assert built.task_type == prepared.sweep_request.task_type
    assert built.sweep is not None
    for field in ("collected", "needing", "accepted", "held", "linear_file", "failed"):
        assert getattr(built.sweep, field) == SWEEP_RETURN[field], field
    # the request the command built is the one record accepts
    completed = asyncio.run(
        HandlerHourlyTick().handle(built.model_copy(update={"registry_root": home}))
    )
    assert completed.phase == "completed"
    assert "collected 4, needing 2, accepted 2, held 0" in completed.completion_line
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:40:57Z\n"


@pytest.mark.parametrize(
    "sweep_text",
    ["", "not json", "[]", '{"needing": 2}', '{"failed": "no"}'],
    ids=["empty", "not-json", "not-an-object", "no-failed-flag", "failed-not-a-bool"],
)
def test_builder_refuses_a_sweep_file_that_is_not_a_sweep_return(
    tmp_path: Path, sweep_text: str
) -> None:
    """Positive control for the builder: a stray file never records n/a counts as a quiet hour."""
    prepared = prepared_fire(fixture_home(tmp_path))
    with pytest.raises(ValueError, match="sweep"):
        build_record_request(prepared, sweep_text, "fire-123")


def test_builder_refuses_a_result_that_awaits_no_sweep(tmp_path: Path) -> None:
    home = fixture_home(tmp_path)
    skipped = asyncio.run(HandlerHourlyTick().handle(request(home, skipSweep=True)))
    with pytest.raises(ValueError, match="awaits no sweep"):
        build_record_request(skipped, json.dumps(SWEEP_RETURN), "fire-123")


def run_cli(home: Path, *argv: str) -> tuple[int, list[str], str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(
            ["--state-root", str(home / "evidence" / str(len(argv))), *argv]
        )
    return code, out.getvalue().splitlines(), err.getvalue()


def test_cli_prepare_then_record_without_the_agent_retyping_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fixture_home(tmp_path)
    monkeypatch.setenv("OMNI_HOME", str(home))
    prepare_request = request(home).model_dump_json()
    code, lines, _ = run_cli(home, "--request-json", prepare_request)
    assert code == 0
    prepared = json.loads(lines[-1])
    assert prepared["phase"] == "prepared"
    sweep_file = tmp_path / "workflow-result.txt"
    sweep_file.write_text(json.dumps(SWEEP_RETURN))
    # the command alone carries state, tail, facts and correlation id into record
    code, lines, err = run_cli(
        home,
        "--record",
        "--fire-id",
        "fire-123",
        "--sweep-file",
        str(sweep_file),
        "--build-only",
    )
    assert code == 0
    built = json.loads(lines[-1])
    assert built["operation"] == "record"
    assert built["correlation_id"] == prepared["correlation_id"]
    assert built["state"]["tail"] == prepared["state"]["tail"]
    assert built["facts"] == prepared["sweep_request"]["facts"]
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:20:00Z\n"
    code, lines, err = run_cli(
        home, "--record", "--fire-id", "fire-123", "--sweep-file", str(sweep_file)
    )
    assert code == 0, err
    recorded = json.loads(lines[-1])
    assert recorded["phase"] == "completed"
    assert "record request" in err
    assert (home / "pointer.txt").read_text() == "2026-09-16T21:40:57Z\n"
    fire = json.loads(
        (home / ".onex_state/morning-workflows/results/fire-123.json").read_text()
    )
    assert (fire["fire_id"], fire["phase"], fire["accepted"]) == (
        "fire-123",
        "completed",
        2,
    )


def test_cli_record_without_a_prepare_for_the_fire_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fixture_home(tmp_path)
    monkeypatch.setenv("OMNI_HOME", str(home))
    sweep_file = tmp_path / "workflow-result.txt"
    sweep_file.write_text(json.dumps(SWEEP_RETURN))
    with pytest.raises(ValueError, match="no prepared result"):
        run_cli(
            home, "--record", "--fire-id", "fire-none", "--sweep-file", str(sweep_file)
        )
    with pytest.raises(ValueError, match="fire id"):
        run_cli(
            home, "--record", "--fire-id", "../escape", "--sweep-file", str(sweep_file)
        )
