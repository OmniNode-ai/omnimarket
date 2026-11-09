# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The MVP launch block's scheduled read and its command route (OMN-23205, plan task M1).

Golden: the publish step's read lists each producer workflow's scheduled runs through the source-control node once, keeps them in a
record, and the floor cycle, reading through the real command route (a subprocess answering from that record and the launch candidate
manifest), draws the readings the record supports. Error: a workflow the source-control node could not list, a read older than its
bound, a missing record, a missing candidate manifest and an overlay that cannot be read each draw the reading NOT WORKING with the
reason, never WORKING. The named break (the read drops the conclusion) turns the golden test red.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleTickRequest,
)
from omnimarket.models.floor_17a.model_mvp_launch import (
    MVP_READING_IDS,
    ModelMvpLaunchBlock,
    ModelMvpLaunchOverlay,
    ModelMvpRun,
)
from omnimarket.nodes.node_floor_simple_effect.handlers import mvp_launch_reads
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
    load_runtime,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.mvp_launch_reads import (
    answer,
    declared_sources,
    refresh,
)
from omnimarket.nodes.node_floor_simple_effect.runtime_floor_simple import (
    run_mvp_answer,
    run_mvp_reads,
)
from tests.nodes.node_floor_simple_effect.mvp_support import CANDIDATE, MVP_NOW
from tests.nodes.node_floor_simple_effect.support import build_home, environ

pytestmark = pytest.mark.unit

RT = load_runtime().mvp_launch
FLOOR = ".claude_scratch/factory-floor/floor-simple.json"
REAL_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
# The shape of the overlay the floor reads on the lab (the overlays repository's node_product_floor_verdict_compute_mvp_launch), with the
# estate's repository names replaced.
OVERLAY_FILE = Path(__file__).parent / "fixtures" / "mvp_launch" / "overlay.yaml"


def ungraded_overlay() -> dict[str, object]:
    doc = yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def overlay_model() -> ModelMvpLaunchOverlay:
    return ModelMvpLaunchOverlay.model_validate(ungraded_overlay())


def run_list_answer(conclusion: str = "success") -> str:
    created = MVP_NOW - timedelta(minutes=20)
    run = {
        "id": 4242,
        "event": "schedule",
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return json.dumps(
        {"outcome": "completed", "result": {"runs": [run]}}, separators=(",", ":")
    )


class Runner:
    """Stands in for ``onex scm run-list``: answers by workflow, counts the calls."""

    def __init__(self, answers: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        workflow = argv[argv.index("--workflow") + 1]
        event = argv[argv.index("--event") + 1]
        self.calls.append((workflow, event))
        return self.answers.get(workflow, (0, run_list_answer(), ""))


def fixed(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def refreshed(home: Path, runner: Runner, at: datetime = REAL_NOW) -> None:
    refresh(
        overlay_model(),
        home=home,
        onex_relpath="onex",
        reads_relpath=RT.reads_relpath,
        read_timeout_seconds=RT.read_timeout_seconds,
        stage_deadline_seconds=RT.stage_deadline_seconds,
        refresh_seconds=RT.refresh_seconds,
        run=runner,
        clock=fixed(at),
    )


def write_candidate(home: Path) -> None:
    path = home / RT.candidate_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"versions": CANDIDATE}), encoding="utf-8")


def tick(home: Path) -> ModelMvpLaunchBlock:
    """One floor cycle reading through the real command route: a subprocess answering from the record."""
    overlay = home.parent / "mvp-overlay.yaml"
    overlay.write_text(yaml.safe_dump(ungraded_overlay()), encoding="utf-8")
    command = f"{sys.executable} -m omnimarket.nodes.node_floor_simple_effect --home {home} --mvp-answer"
    env = {
        **environ(home),
        "ONEX_MVP_LAUNCH_OVERLAY": str(overlay),
        RT.command_env: command,
    }
    state = {"n": 0}

    def clock() -> datetime:
        state["n"] += 1
        return MVP_NOW + timedelta(seconds=state["n"])

    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(), clock=clock, served_alarm_ids=lambda: None
    )
    assert handler.run(ModelFloorSimpleTickRequest(home=str(home)), environ=env).ok
    doc = ModelFloorSimple.model_validate_json(
        (home / FLOOR).read_text(encoding="utf-8")
    )
    assert doc.mvp_launch is not None
    return doc.mvp_launch


def reading(block: ModelMvpLaunchBlock, item_id: str) -> tuple[str, str]:
    found = next(r for r in block.readings if r.id == item_id)
    return found.state, found.reason


def test_golden_the_scheduled_read_feeds_the_floor_through_the_command_route(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    runner = Runner()
    refreshed(home, runner, datetime.now(UTC))
    write_candidate(home)
    sources = declared_sources(overlay_model())
    assert sorted(w for w, _ in runner.calls) == sorted(
        s[1] for s in sources.values() for _ in s[3]
    )
    record = json.loads((home / RT.reads_relpath).read_text(encoding="utf-8"))
    assert len(record["reads"]) == len(sources)
    block = tick(home)
    assert sorted(r.id for r in block.readings) == sorted(MVP_READING_IDS)
    # a producer with a green newest run and nothing else to grade reads WORKING from the real read
    assert reading(block, "C11") == ("WORKING", "ok")
    assert reading(block, "C16") == ("WORKING", "ok")
    # the customer-machine readings still need the walk's Mac leg, which no producer records yet
    assert reading(block, "C13") == ("NOT WORKING", "mac-leg-missing")
    # a run record carries no graded versions, so a graded reading is not the candidate until its producer records them
    assert reading(block, "onboarding-verify-native") == (
        "NOT WORKING",
        "not-the-candidate",
    )
    assert block.candidate_versions == CANDIDATE


def test_golden_the_publish_step_reads_through_the_overlay_and_prints_what_it_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = build_home(tmp_path)
    overlay = tmp_path / "mvp-overlay.yaml"
    overlay.write_text(yaml.safe_dump(ungraded_overlay()), encoding="utf-8")
    env = {"ONEX_MVP_LAUNCH_OVERLAY": str(overlay)}
    runner = Runner()
    code = run_mvp_reads(str(home), env, runner, fixed(REAL_NOW))
    assert code == 0
    said = json.loads(capsys.readouterr().out)
    assert said == {
        "workflows_read": len(declared_sources(overlay_model())),
        "unread": 0,
    }
    assert (home / RT.reads_relpath).is_file()


def test_golden_a_read_inside_its_refresh_interval_is_kept_and_the_stalest_is_read_first(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    first = Runner()
    refreshed(home, first)
    again = Runner()
    refreshed(home, again, REAL_NOW + timedelta(seconds=RT.refresh_seconds - 1))
    assert again.calls == []
    later = Runner()
    refreshed(home, later, REAL_NOW + timedelta(seconds=RT.refresh_seconds + 1))
    assert len(later.calls) == len(first.calls)


def test_a_stage_deadline_reads_what_fits_and_keeps_the_rest_as_they_were(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    runner = Runner()
    moments = iter(range(0, 10_000, 30))

    def clock() -> datetime:
        return REAL_NOW + timedelta(seconds=next(moments))

    refresh(
        overlay_model(),
        home=home,
        onex_relpath="onex",
        reads_relpath=RT.reads_relpath,
        read_timeout_seconds=25,
        stage_deadline_seconds=80,
        refresh_seconds=240,
        run=runner,
        clock=clock,
    )
    read = json.loads((home / RT.reads_relpath).read_text(encoding="utf-8"))["reads"]
    assert 0 < len(read) < len(declared_sources(overlay_model()))


def test_error_a_workflow_the_source_control_node_could_not_list_is_not_working_with_its_reason(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    failing = overlay_model().readings[0].source
    assert failing is not None
    runner = Runner({failing.workflow: (1, "", "HTTP 404 Not Found")})
    refreshed(home, runner, datetime.now(UTC))
    write_candidate(home)
    block = tick(home)
    named = next(r for r in block.readings if r.id == "C11")
    assert (named.state, named.reason) == ("NOT WORKING", "unreadable")
    assert "run-list exit 1" in named.detail
    assert reading(block, "C12") == ("WORKING", "ok")


def test_error_a_run_list_that_failed_carries_the_gateways_reason(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    failed = json.dumps(
        {"outcome": "failed", "result": {"failure_reason": "HTTP 404 workflow absent"}}
    )
    spec = overlay_model().readings[0].source
    assert spec is not None
    refreshed(home, Runner({spec.workflow: (0, failed, "")}), datetime.now(UTC))
    write_candidate(home)
    named = next(r for r in tick(home).readings if r.id == "C11")
    assert named.state == "NOT WORKING"
    assert named.detail == "HTTP 404 workflow absent"


def test_error_a_read_older_than_its_bound_is_stale_never_current(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    refreshed(
        home,
        Runner(),
        datetime.now(UTC) - timedelta(seconds=RT.reads_max_age_seconds + 60),
    )
    write_candidate(home)
    block = tick(home)
    assert reading(block, "C11") == ("NOT WORKING", "unreadable")
    assert next(r for r in block.readings if r.id == "C11").detail == "read-stale"


def test_error_no_record_and_no_candidate_manifest_say_so(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    block = tick(home)
    c11 = next(r for r in block.readings if r.id == "C11")
    assert (c11.state, c11.detail) == ("NOT WORKING", "reads-record-missing")
    assert block.candidate_error == "candidate-manifest-not-found"
    assert block.state == "NOT WORKING"


def test_error_a_malformed_candidate_manifest_is_unreadable_not_empty(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    path = home / RT.candidate_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"versions": {}}), encoding="utf-8")
    got = answer(
        {"query": "candidate"},
        reads_path=home / RT.reads_relpath,
        candidate_path=path,
        reads_max_age_seconds=RT.reads_max_age_seconds,
        now=REAL_NOW,
    )
    assert got == {"error": "candidate-manifest-malformed"}


def test_error_an_unknown_query_and_a_request_that_is_not_json_answer_with_a_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = build_home(tmp_path)
    assert run_mvp_answer(str(home), {}, "not json") == 0
    assert json.loads(capsys.readouterr().out) == {"error": "request-not-an-object"}
    assert run_mvp_answer(str(home), {}, json.dumps({"query": "nope"})) == 0
    assert json.loads(capsys.readouterr().out) == {"error": "unknown-query:nope"}


def test_error_an_overlay_that_cannot_be_read_reads_nothing_and_exits_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = build_home(tmp_path)
    broken = tmp_path / "broken.yaml"
    broken.write_text("title: ''\nreadings: []\n", encoding="utf-8")
    runner = Runner()
    code = run_mvp_reads(
        str(home), {"ONEX_MVP_LAUNCH_OVERLAY": str(broken)}, runner, fixed(REAL_NOW)
    )
    assert code == 2
    assert runner.calls == []
    assert not (home / RT.reads_relpath).exists()
    assert "REFUSED mvp-reads" in capsys.readouterr().err


def test_injected_break_the_read_drops_the_conclusion_and_turns_the_golden_reading_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named break ``read-drops-conclusion``: a failed latest run is recorded as a success, so the floor reads it WORKING."""
    failing = Runner({w: (0, run_list_answer("failure"), "") for w in _workflows()})
    home = build_home(tmp_path / "honest")
    refreshed(home, failing, datetime.now(UTC))
    write_candidate(home)
    assert reading(tick(home), "C11") == ("NOT WORKING", "failure")

    original = mvp_launch_reads._run_from

    def conclusion_dropped(raw: dict[str, object]) -> ModelMvpRun:
        return original({**raw, "conclusion": "success"})

    monkeypatch.setattr(mvp_launch_reads, "_run_from", conclusion_dropped)
    home = build_home(tmp_path / "broken")
    refreshed(home, failing, datetime.now(UTC))
    write_candidate(home)
    assert reading(tick(home), "C11") == ("WORKING", "ok")


def _workflows() -> list[str]:
    return [s[1] for s in declared_sources(overlay_model()).values()]
