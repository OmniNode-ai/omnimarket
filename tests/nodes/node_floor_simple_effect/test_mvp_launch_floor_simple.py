# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor cycle publishes the MVP launch block (OMN-23205, plan task M1).

Golden: the cycle reads the overlay and the run histories through its source, writes ``mvp_launch`` with exactly the 17
ids into floor-simple.json, stamped with the record's time, and emits a start and a success for the step. Error: a failing
step ends in a typed failure and writes no floor; an overlay or a source that cannot be read draws every reading NOT
WORKING with the reason and the floor still publishes; the command route is exercised for real through a subprocess.
The named break (the newest-run rule turned off) is run through the same cycle and turns the golden test red.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleTickRequest,
    ModelFloorSimpleTickResult,
)
from omnimarket.models.floor_17a.model_mvp_launch import (
    MVP_READING_IDS,
    ModelMvpCandidate,
    ModelMvpLaunchBlock,
    ModelMvpLaunchRequest,
    ModelMvpReading,
    ModelMvpReadingSpec,
    ModelMvpSourceHistory,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.mvp_launch_sources import (
    InMemoryMvpRunSource,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_mvp_launch import (
    HandlerMvpLaunch,
)
from tests.nodes.node_floor_simple_effect.mvp_support import (
    CANDIDATE,
    MVP_NOW,
    green_world,
    overlay_dict,
    run,
    with_history,
)
from tests.nodes.node_floor_simple_effect.support import build_home, environ

pytestmark = pytest.mark.unit

STEP = "node_product_floor_verdict_compute:decide_mvp_launch"
FLOOR = ".claude_scratch/factory-floor/floor-simple.json"


def mvp_clock() -> Callable[[], datetime]:
    state = {"n": 0}

    def clock() -> datetime:
        state["n"] += 1
        return MVP_NOW + timedelta(seconds=state["n"])

    return clock


def mvp_env(home: Path) -> dict[str, str]:
    overlay = home.parent / "mvp-overlay.yaml"
    overlay.write_text(yaml.safe_dump(overlay_dict()), encoding="utf-8")
    return {**environ(home), "ONEX_MVP_LAUNCH_OVERLAY": str(overlay)}


def cycle(
    home: Path,
    sink: InMemoryEmissionSink | None = None,
    *,
    env: dict[str, str] | None = None,
    source: InMemoryMvpRunSource | None = None,
    candidate: ModelMvpCandidate | None = None,
    world: dict[str, ModelMvpSourceHistory] | None = None,
) -> ModelFloorSimpleTickResult:
    handler = HandlerFloorSimpleTick(
        sink=sink or InMemoryEmissionSink(),
        clock=mvp_clock(),
        served_alarm_ids=lambda: None,
        mvp_source=source
        or InMemoryMvpRunSource(
            world if world is not None else green_world(),
            candidate or ModelMvpCandidate(versions=CANDIDATE),
        ),
    )
    return handler.run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=env or mvp_env(home)
    )


def written(home: Path) -> ModelFloorSimple:
    return ModelFloorSimple.model_validate_json(
        (home / FLOOR).read_text(encoding="utf-8")
    )


def block_of(doc: ModelFloorSimple) -> ModelMvpLaunchBlock:
    assert doc.mvp_launch is not None
    return doc.mvp_launch


def test_golden_the_floor_carries_the_mvp_launch_block_with_exactly_the_17_ids(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()
    result = cycle(home, sink)
    assert result.ok
    doc = written(home)
    block = block_of(doc)
    assert sorted(r.id for r in block.readings) == sorted(MVP_READING_IDS)
    assert block.state == "WORKING"
    assert block.generated_at == doc.generated_at
    assert [s.id for s in block.scenarios] == [f"R{n}" for n in range(1, 10)]
    chain = [e for e in sink.events if e.node == STEP]
    assert [e.phase for e in chain] == ["start", "success"]
    assert chain[0].correlation_id == result.correlation_id
    # the served JSON carries the ids a checker reads with jq
    raw = json.loads((home / FLOOR).read_text(encoding="utf-8"))
    assert sorted(r["id"] for r in raw["mvp_launch"]["readings"]) == sorted(
        MVP_READING_IDS
    )


def test_golden_a_checker_can_repeat_the_block_from_the_same_histories(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert cycle(home).ok
    block = block_of(written(home))
    from tests.nodes.node_floor_simple_effect.mvp_support import request

    again = HandlerMvpLaunch().handle(request(now=block.generated_at))
    assert again == block


def test_golden_a_failed_latest_run_makes_the_written_block_not_working_and_names_the_reading(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    world = with_history(
        "C16", [run("x", MVP_NOW - timedelta(minutes=30), conclusion="failure")]
    )
    assert cycle(home, world=world).ok
    block = block_of(written(home))
    assert block.state == "NOT WORKING"
    assert "C16: failure" in block.named


def test_error_an_overlay_that_cannot_be_read_draws_all_17_not_working_and_the_floor_still_publishes(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    broken = home.parent / "broken-mvp.yaml"
    broken.write_text("title: ''\nreadings: []\n", encoding="utf-8")
    env = {**environ(home), "ONEX_MVP_LAUNCH_OVERLAY": str(broken)}
    assert cycle(home, env=env).ok
    block = block_of(written(home))
    assert block.state == "NOT WORKING"
    assert sorted(r.id for r in block.readings) == sorted(MVP_READING_IDS)
    assert {r.reason for r in block.readings} == {"overlay-unreadable"}


def test_error_a_missing_overlay_is_not_working_never_working(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    assert cycle(home, env=environ(home)).ok
    block = block_of(written(home))
    assert block.state == "NOT WORKING"
    assert {r.reason for r in block.readings} == {"overlay-unreadable"}


def test_error_a_source_that_cannot_be_read_draws_every_scheduled_reading_unreadable(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=mvp_clock(),
        served_alarm_ids=lambda: None,
    )
    # no command is configured: every history and the candidate are unreadable.
    assert handler.run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=mvp_env(home)
    ).ok
    block = block_of(written(home))
    assert block.state == "NOT WORKING"
    scheduled = [r for r in block.readings if r.kind == "scheduled"]
    assert {r.reason for r in scheduled} == {"unreadable"}
    assert {r.detail for r in scheduled} == {"read-command-not-configured"}
    assert block.candidate_error == "read-command-not-configured"


def test_a_command_route_reads_histories_and_candidate_through_a_subprocess(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    world = green_world()
    answers = {
        "runs": {
            key: [json.loads(r.model_dump_json()) for r in hist.runs]
            for key, hist in world.items()
        },
        "candidate": {"versions": CANDIDATE},
    }
    data = tmp_path / "answers.json"
    data.write_text(json.dumps(answers), encoding="utf-8")
    script = tmp_path / "read_command.py"
    script.write_text(
        "import json, sys\n"
        f"answers = json.load(open({str(data)!r}))\n"
        "req = json.load(sys.stdin)\n"
        "if req['query'] == 'candidate':\n"
        "    print(json.dumps(answers['candidate']))\n"
        "else:\n"
        "    wf = req['workflow'].removesuffix('.yml')\n"
        "    print(json.dumps({'runs': answers['runs'][wf]}))\n",
        encoding="utf-8",
    )
    env = {
        **mvp_env(home),
        "ONEX_MVP_LAUNCH_READ_COMMAND": f"{sys.executable} {script}",
    }
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=mvp_clock(),
        served_alarm_ids=lambda: None,
    )
    assert handler.run(ModelFloorSimpleTickRequest(home=str(home)), environ=env).ok
    block = block_of(written(home))
    assert block.state == "WORKING"
    assert block.candidate_versions == CANDIDATE


def test_error_chain_a_failing_mvp_step_emits_a_typed_failure_and_writes_no_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)

    def refuse(_self: HandlerMvpLaunch, _request: object) -> None:
        raise RuntimeError("mvp launch refused")

    monkeypatch.setattr(HandlerMvpLaunch, "handle", refuse)
    sink = InMemoryEmissionSink()
    result = cycle(home, sink)
    assert not result.ok
    failures = [e for e in sink.events if e.node == STEP and e.phase == "failure"]
    assert len(failures) == 1
    assert failures[0].failure_code
    assert not (home / FLOOR).exists()


def test_injected_break_newest_run_rule_off_turns_the_failed_latest_reading_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named break ``newest-run-rule-off``: the conclusion test is skipped, so a failed latest run reads WORKING."""
    world = with_history(
        "C16", [run("x", MVP_NOW - timedelta(minutes=30), conclusion="failure")]
    )
    home = build_home(tmp_path / "a")
    assert cycle(home, world=world).ok
    assert block_of(written(home)).state == "NOT WORKING"

    original = HandlerMvpLaunch._scheduled

    def conclusion_ignored(
        self: HandlerMvpLaunch,
        spec: ModelMvpReadingSpec,
        request: ModelMvpLaunchRequest,
        walk_id: str,
    ) -> ModelMvpReading:
        patched = (
            spec.model_copy(update={"green_conclusion": "failure"})
            if spec.id == "C16"
            else spec
        )
        return original(self, patched, request, walk_id)

    monkeypatch.setattr(HandlerMvpLaunch, "_scheduled", conclusion_ignored)
    home = build_home(tmp_path / "b")
    assert cycle(home, world=world).ok
    assert block_of(written(home)).state == "WORKING"
