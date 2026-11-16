# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh on merge: build only when a repository head moved and the last build is old.

A tick whose heads are unchanged appends nothing and runs no build; a tick whose build is too
young defers; a tick that cannot read a head fails rather than passing as fresh; the schedule
renders its own ten-minute timer; and the writing build's marker is what the refresh reads.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_code_graph import (
    EnumGraphFault,
    EnumRefreshOutcome,
    ModelCodeGraphCommand,
    ModelCodeGraphRefreshResult,
    ModelCodeGraphResult,
)
from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    GitRepoSource,
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_refresh import (
    CodeGraphRefresh,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_state import (
    ATTEMPT_MARKER,
    BUILT_MARKER,
    build_lock,
    write_marker,
)
from omnimarket.nodes.node_code_graph_effect.handlers.schedule import (
    CodeGraphScheduler,
    load_schedule,
)
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolRepoSource,
    RepoSourceError,
)
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import _keep, main
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    HOST,
    REF,
    RUN_AT,
    make_clone,
    overlay,
    source,
)

pytestmark = pytest.mark.unit

CORE_HEAD = "c" * 12
APP_HEAD = "a" * 12
MOVED_HEAD = "d" * 40


class _StaticHeads:
    """Heads for the refresh without files: only what ``head()`` reads."""

    def __init__(self, heads: dict[str, str]) -> None:
        self._heads = heads

    def head(self, repo: str, ref: str) -> str:
        head = self._heads.get(repo)
        if head is None:
            raise RepoSourceError(f"no-repository:{repo}@{ref}")
        return head


class _WritingBuild:
    """The nightly build with an appender, counting how often the refresh ran it."""

    def __init__(self, repos: ProtocolRepoSource) -> None:
        self.calls = 0
        #: Where and when the build leaves its marker; set by ``_refresh``.
        self.keep_in: tuple[Path, datetime] | None = None
        self.ledger = InMemoryLedger()
        self._handler = CodeGraphRun(
            overlay=overlay(),
            host=HOST,
            source=repos,
            appender=self.ledger,
            clock=lambda: RUN_AT,
        )

    def __call__(self) -> ModelCodeGraphResult:
        self.calls += 1
        result = self._handler.handle(
            ModelCodeGraphCommand(correlation_id=f"refresh-{self.calls}")
        )
        if self.keep_in is not None:
            directory, at = self.keep_in
            write_marker(directory, result, at)
        return result


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _write_marker(directory: Path, built_at: datetime, heads: dict[str, str]) -> None:
    """The marker a completed build leaves, as the refresh reads it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "code-graph-built.json").write_text(
        json.dumps({"built_at": built_at.isoformat(), "heads": heads}),
        encoding="utf-8",
    )


def _refresh(
    state: Path,
    build: _WritingBuild,
    heads: dict[str, str],
    now: datetime,
) -> ModelCodeGraphRefreshResult:
    build.keep_in = (state, now)
    handler = CodeGraphRefresh(
        overlay=overlay(),
        heads=_StaticHeads(heads),
        state_directory=state,
        run=build,
        clock=lambda: now,
    )
    return handler.handle()


def test_refresh_builds_once_when_a_head_moved_and_there_is_no_marker(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    result = _refresh(
        tmp_path / "state",
        build,
        {"example_core": MOVED_HEAD, "example_app": "a" * 40},
        RUN_AT,
    )
    assert result.outcome is EnumRefreshOutcome.REFRESHED
    assert build.calls == 1
    assert len(build.ledger.lines) == 1
    # no marker means no build to compare against: every repository counts as changed
    assert result.changed == ("example_core", "example_app")
    assert result.reason == "heads-moved"


def test_refresh_is_fresh_when_every_head_matches_the_marker(tmp_path: Path) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    _write_marker(
        tmp_path / "state",
        RUN_AT,
        {"example_core": CORE_HEAD, "example_app": APP_HEAD},
    )
    result = _refresh(
        tmp_path / "state",
        build,
        {"example_core": "c" * 40, "example_app": "a" * 40},
        RUN_AT + timedelta(hours=1),
    )
    assert result.outcome is EnumRefreshOutcome.FRESH
    assert build.calls == 0, "a fresh refresh must not run the build"
    assert build.ledger.lines == []
    assert result.changed == ()
    assert result.built_heads == {"example_core": CORE_HEAD, "example_app": APP_HEAD}


def test_refresh_defers_when_a_head_moved_and_the_build_is_five_minutes_old(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    _write_marker(
        tmp_path / "state",
        RUN_AT,
        {"example_core": CORE_HEAD, "example_app": APP_HEAD},
    )
    result = _refresh(
        tmp_path / "state",
        build,
        {"example_core": MOVED_HEAD, "example_app": "a" * 40},
        RUN_AT + timedelta(minutes=5),
    )
    assert result.outcome is EnumRefreshOutcome.DEFERRED
    assert build.calls == 0, "a deferred refresh must not run the build"
    assert build.ledger.lines == []
    assert result.changed == ("example_core",)


def test_refresh_builds_when_a_head_moved_and_the_build_is_twenty_minutes_old(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    _write_marker(
        tmp_path / "state",
        RUN_AT,
        {"example_core": CORE_HEAD, "example_app": APP_HEAD},
    )
    result = _refresh(
        tmp_path / "state",
        build,
        {"example_core": MOVED_HEAD, "example_app": "a" * 40},
        RUN_AT + timedelta(minutes=20),
    )
    assert result.outcome is EnumRefreshOutcome.REFRESHED
    assert build.calls == 1
    assert len(build.ledger.lines) == 1
    assert result.changed == ("example_core",)


def test_refresh_fails_rather_than_passing_as_fresh_when_a_head_is_unreadable(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    _write_marker(
        tmp_path / "state",
        RUN_AT,
        {"example_core": CORE_HEAD, "example_app": APP_HEAD},
    )
    result = _refresh(tmp_path / "state", build, {}, RUN_AT)
    assert result.outcome is EnumRefreshOutcome.FAILED
    assert build.calls == 0
    assert result.reason.startswith("heads-unreadable")


def test_the_refresh_schedule_renders_the_refresh_verb_and_ten_minute_timer(
    tmp_path: Path,
) -> None:
    scheduler = CodeGraphScheduler(
        schedule=load_schedule(block="refresh_schedule"),
        overlay=overlay(),
        home=tmp_path / "home",
        run=_Runner(),
    )
    result = scheduler.install(
        "example-host",
        omni_root=tmp_path / "root",
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert result.exit_code == 0, result.stderr
    assert "OnCalendar=*-*-* *:0/10:00 UTC" in result.stdout
    assert "-m omnimarket.nodes.node_code_graph_effect refresh\n" in result.stdout
    assert "code-graph-refresh.out.log" in result.stdout
    assert "Persistent=false" in result.stdout
    assert scheduler.timer_name == "onex-code-graph-refresh.timer"


def test_refresh_marker_written_by_keep_for_a_completed_run_and_not_for_a_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos = source("memory", tmp_path)
    handler = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=repos,
        appender=InMemoryLedger(),
        clock=lambda: RUN_AT,
    )
    state = tmp_path / "state"
    monkeypatch.setenv("ONEX_CODE_GRAPH_STATE_DIR", str(state))
    _keep(handler.handle(ModelCodeGraphCommand(correlation_id="k")))
    marker = json.loads((state / "code-graph-built.json").read_text(encoding="utf-8"))
    assert marker["heads"] == {"example_core": CORE_HEAD, "example_app": APP_HEAD}
    assert datetime.fromisoformat(marker["built_at"]).tzinfo is not None
    # a drill (here one that still completes) must not touch the built marker
    _keep(
        handler.handle(
            ModelCodeGraphCommand(
                correlation_id="d", fault=EnumGraphFault.DROP_ENTRY_POINTS
            )
        )
    )
    assert json.loads(
        (state / "code-graph-built.json").read_text(encoding="utf-8")
    ) == (marker)
    assert (state / "code-graph-latest.json").is_file()


def test_refresh_head_reads_the_head_the_in_memory_source_snapshotted(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    assert isinstance(repos, InMemoryRepoSource)
    assert repos.head("example_core", REF) == "c" * 40
    with pytest.raises(RepoSourceError):
        repos.head("example_core", "origin/other")


TWO_HEADS = {"example_core": MOVED_HEAD, "example_app": "a" * 40}


def _probe_result(tmp_path: Path) -> ModelCodeGraphResult:
    """A completed probe: the build and verdict of a run that appends nothing."""
    handler = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=source("memory", tmp_path),
        appender=None,
        clock=lambda: RUN_AT,
    )
    return handler.handle(ModelCodeGraphCommand(correlation_id="p", write=False))


def test_marker_is_not_written_for_a_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("ONEX_CODE_GRAPH_STATE_DIR", str(state))
    result = _probe_result(tmp_path)
    assert result.status == "completed"
    assert result.ledger == "not-written"
    _keep(result)
    assert not (state / BUILT_MARKER).exists(), (
        "a probe appends nothing and builds nothing"
    )
    assert (state / "code-graph-latest.json").is_file()


def test_marker_is_not_written_for_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("ONEX_CODE_GRAPH_STATE_DIR", str(state))
    ledger = InMemoryLedger()
    ledger.append_status = "error"
    handler = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=source("memory", tmp_path),
        appender=ledger,
        clock=lambda: RUN_AT,
    )
    result = handler.handle(ModelCodeGraphCommand(correlation_id="f"))
    assert result.status == "failed"
    assert result.ledger.startswith("append-failed")
    _keep(result)
    assert not (state / BUILT_MARKER).exists()


def test_marker_is_written_when_the_ledger_already_holds_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("ONEX_CODE_GRAPH_STATE_DIR", str(state))
    ledger = InMemoryLedger()
    handler = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=source("memory", tmp_path),
        appender=ledger,
        clock=lambda: RUN_AT,
    )
    command = ModelCodeGraphCommand(correlation_id="same")
    assert handler.handle(command).ledger == "written"
    second = handler.handle(command)
    assert second.ledger == "already-written"
    _keep(second)
    assert (state / BUILT_MARKER).is_file()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Two real clones, the example overlay on the git backend, and the environment the verb reads."""
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    roots = tmp_path / "overlays"
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    target = roots / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    monkeypatch.setenv("EXAMPLE_HOST_NAME", HOST)
    monkeypatch.setenv("ONEX_CODE_GRAPH_STATE_DIR", str(state))
    ledger = InMemoryLedger()
    builds: list[str] = []

    def appender(*_arguments: object, **_keywords: object) -> InMemoryLedger:
        builds.append("build")
        return ledger

    monkeypatch.setattr(runtime_code_graph, "build_appender", appender)
    return {"state": state, "ledger": ledger, "builds": builds}


def _tick(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["refresh"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_tick_builds_and_writes_the_marker_then_the_next_tick_is_fresh(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    state = world["state"]
    assert isinstance(state, Path)
    builds = world["builds"]
    assert isinstance(builds, list)
    ledger = world["ledger"]
    assert isinstance(ledger, InMemoryLedger)
    code, out, _err = _tick(capsys)
    assert code == 0, out
    assert "REFRESH outcome=refreshed" in out
    marker = json.loads((state / BUILT_MARKER).read_text(encoding="utf-8"))
    assert set(marker["heads"]) == {"example_core", "example_app"}
    assert len(ledger.lines) == 1
    # the result reports the marker the build wrote, not the refresh's own decision
    assert f"built_at={marker['built_at']}" in out
    assert f"example_core@{marker['heads']['example_core']}" in out
    code, out, _err = _tick(capsys)
    assert code == 0, out
    assert "REFRESH outcome=fresh" in out
    assert len(builds) == 1, "one build in total across the two ticks"
    assert len(ledger.lines) == 1


def test_failed_build_exits_one_and_leaves_the_marker_untouched(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    state = world["state"]
    assert isinstance(state, Path)
    ledger = world["ledger"]
    assert isinstance(ledger, InMemoryLedger)
    ledger.append_status = "error"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    _write_marker(state, old, {"example_core": "0" * 12, "example_app": "0" * 12})
    before = (state / BUILT_MARKER).read_bytes()
    code, out, _err = _tick(capsys)
    assert code == 1, out
    assert "REFRESH outcome=failed" in out
    # the build's own failure is the reason: not the read-back's marker-not-rewritten, which also
    # answers failed when the status branch is gone
    assert "reason=append-failed-error" in out, out
    assert "marker-not-rewritten" not in out
    assert (state / BUILT_MARKER).read_bytes() == before
    assert ledger.lines == []
    assert (state / ATTEMPT_MARKER).is_file(), (
        "the failed build is recorded as an attempt"
    )


@pytest.mark.parametrize("missing", ["EXAMPLE_HOST_NAME", "ONEX_CODE_GRAPH_STATE_DIR"])
def test_unset_host_or_state_directory_variable_exits_three(
    world: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    code, out, err = _tick(capsys)
    assert code == 3, (out, err)
    assert "INDETERMINATE" in err
    assert missing in err
    assert "REFRESH" not in out


def test_attempt_after_a_failed_build_defers_the_next_tick_within_fifteen_minutes(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    state = world["state"]
    assert isinstance(state, Path)
    builds = world["builds"]
    assert isinstance(builds, list)
    ledger = world["ledger"]
    assert isinstance(ledger, InMemoryLedger)
    ledger.append_status = "error"
    assert _tick(capsys)[0] == 1
    code, out, _err = _tick(capsys)
    assert code == 0, out
    assert "REFRESH outcome=deferred" in out
    assert "reason=attempted-too-recently" in out
    assert len(builds) == 1, "the deferred tick must not rerun the build"
    assert not (state / BUILT_MARKER).exists()


def test_attempt_record_spans_ticks_until_fifteen_minutes_pass(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    build.ledger.append_status = "error"
    state = tmp_path / "state"
    first = _refresh(state, build, TWO_HEADS, RUN_AT)
    assert first.outcome is EnumRefreshOutcome.FAILED
    assert first.attempted_at == RUN_AT
    ten = _refresh(state, build, TWO_HEADS, RUN_AT + timedelta(minutes=10))
    assert ten.outcome is EnumRefreshOutcome.DEFERRED
    assert ten.reason == "attempted-too-recently"
    assert build.calls == 1
    later = _refresh(state, build, TWO_HEADS, RUN_AT + timedelta(minutes=16))
    assert later.outcome is EnumRefreshOutcome.FAILED
    assert build.calls == 2, "after fifteen minutes the refresh tries again"


def test_readback_reports_the_marker_the_build_rewrote(tmp_path: Path) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    state = tmp_path / "state"
    decided = RUN_AT
    built = RUN_AT + timedelta(minutes=3)

    def run() -> ModelCodeGraphResult:
        # the build ends minutes after the decision and re-snapshots a head that moved again
        result = build()
        _write_marker(state, built, {"example_core": "e" * 12, "example_app": APP_HEAD})
        return result

    handler = CodeGraphRefresh(
        overlay=overlay(),
        heads=_StaticHeads(TWO_HEADS),
        state_directory=state,
        run=run,
        clock=lambda: decided,
    )
    result = handler.handle()
    assert result.outcome is EnumRefreshOutcome.REFRESHED
    assert result.built_at == built
    assert result.built_heads == {"example_core": "e" * 12, "example_app": APP_HEAD}
    assert result.current_heads["example_core"] == "d" * 12


def test_readback_fails_when_the_build_did_not_rewrite_the_marker(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    state = tmp_path / "state"
    old = RUN_AT - timedelta(hours=2)
    _write_marker(state, old, {"example_core": CORE_HEAD, "example_app": APP_HEAD})
    handler = CodeGraphRefresh(
        overlay=overlay(),
        heads=_StaticHeads(TWO_HEADS),
        state_directory=state,
        run=build,  # builds, appends the row, but never keeps a marker (build.keep_in unset)
        clock=lambda: RUN_AT,
    )
    result = handler.handle()
    assert build.calls == 1
    assert result.outcome is EnumRefreshOutcome.FAILED
    assert result.reason == "marker-not-rewritten"
    assert result.built_at == old, "the stale marker is what stays reported"
    assert result.result is not None


def test_readback_fails_for_a_probe_standing_in_for_the_build(tmp_path: Path) -> None:
    state = tmp_path / "state"
    probe = _probe_result(tmp_path)

    def run() -> ModelCodeGraphResult:
        write_marker(state, probe, RUN_AT)
        return probe

    handler = CodeGraphRefresh(
        overlay=overlay(),
        heads=_StaticHeads(TWO_HEADS),
        state_directory=state,
        run=run,
        clock=lambda: RUN_AT,
    )
    result = handler.handle()
    assert result.outcome is EnumRefreshOutcome.FAILED
    assert result.reason == "marker-not-rewritten"
    assert not (state / BUILT_MARKER).exists()


def test_lock_second_refresh_defers_while_another_run_holds_the_build_lock(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)
    state = tmp_path / "state"
    with build_lock(state) as held:
        assert held
        result = _refresh(state, build, TWO_HEADS, RUN_AT)
        with build_lock(state) as second:
            assert not second, (
                "one lock in the state directory: a second holder is refused"
            )
    assert result.outcome is EnumRefreshOutcome.DEFERRED
    assert result.reason == "build-lock-held"
    assert build.calls == 0
    assert build.ledger.lines == []
    assert (
        _refresh(state, build, TWO_HEADS, RUN_AT).outcome
        is EnumRefreshOutcome.REFRESHED
    )
    assert build.calls == 1


def test_lock_nightly_defers_while_the_refresh_holds_the_build_lock(
    world: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = world["state"]
    assert isinstance(state, Path)
    ledger = world["ledger"]
    assert isinstance(ledger, InMemoryLedger)
    monkeypatch.setattr(runtime_code_graph, "BUILD_LOCK_WAIT", timedelta(0))
    with build_lock(state) as held:
        assert held
        code = main(["nightly"])
    out = capsys.readouterr().out
    assert code == 0
    assert "RUN status=deferred reason=build-lock-held" in out
    assert ledger.lines == []
    assert not (state / "code-graph-latest.json").exists()
    # with the lock free the nightly builds and writes its row
    assert main(["nightly"]) == 0
    assert len(ledger.lines) == 1


CONTRACT = Path(__file__).parents[3] / "contracts" / "OMN-24642.yaml"


def _contract() -> dict[str, object]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _dod(document: dict[str, object], dod_id: str) -> dict[str, object]:
    evidence = document["dod_evidence"]
    assert isinstance(evidence, list)
    (found,) = [item for item in evidence if item["id"] == dod_id]
    return found


def test_contract_states_the_bound_ticket_criteria_with_the_architecture_graph() -> (
    None
):
    document = _contract()
    assert "architecture graph" in str(document["title"])
    requirements = document["requirements"]
    assert isinstance(requirements, list)
    criteria = {item["id"]: item["statement"] for item in requirements[0]["acceptance"]}
    assert list(criteria) == ["AC1", "AC2", "AC3", "AC4"]
    assert "architecture graph" in criteria["AC1"]
    assert "NOT MET" in criteria["AC1"]
    assert "OMN-14572" in criteria["AC1"]
    # AC2 as the ticket words it, and marked NOT MET: its dod check reads a lane report that holds no query yet
    assert (
        'the query "who imports node_host_table_keys_ingest_effect" is run against the refreshed graph'
        " and its answer recorded in the lane report"
    ) in criteria["AC2"]
    assert "NOT MET" in criteria["AC2"]
    assert "polling timer" in criteria["AC3"]
    # the installed-unit criterion follows the merge: no branch install is observable before it
    assert "after the pull request merges" in criteria["AC4"]
    assert "before the pull request is handed off" not in criteria["AC4"]
    # every dod_evidence id that names an AC names one the acceptance list holds
    for item in document["dod_evidence"]:
        for named in re.findall(r"-ac(\d+)", str(item["id"])):
            assert f"AC{named}" in criteria, item["id"]


def test_contract_binds_the_installed_unit_to_a_local_done_gate_check_on_h053() -> None:
    gate = _dod(_contract(), "dod-omn24642-ac4-installed-unit")
    assert gate["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in gate, "a local-only check is unbound for hosted dod-verify"
    (check,) = gate["checks"]
    command = check["check_value"]
    assert "--host h053" in command
    assert "onex-code-graph-refresh.service" in command
    assert "code-graph-refresh.out.log" in command
    assert "-mmin" in command, "a fresh log entry, not only an installed unit"
    # the render test alone cannot satisfy AC4
    render = _dod(_contract(), "dod-omn24642-ac4-unit-renders")
    assert render.get("execution_scope") != "local_done_gate"


def test_contract_integration_proof_matches_only_non_failed_outcomes_and_states_its_row() -> (
    None
):
    proof = _dod(_contract(), "dod-omn24642-integration-proof")
    (check,) = proof["checks"]
    command = check["check_value"]
    assert "outcome=(refreshed|fresh|deferred) " in command
    assert '"^REFRESH outcome="' not in command
    assert "appends one operational code graph row" in str(proof["description"])
    # the pattern really excludes a failed outcome and accepts the three others
    pattern = command.split('grep -Eq "')[1].split('"')[0]
    assert (
        re.search(pattern, "REFRESH outcome=failed changed=none built_at=none") is None
    )
    for outcome in ("refreshed", "fresh", "deferred"):
        assert re.search(pattern, f"REFRESH outcome={outcome} changed=none") is not None


def test_contract_of_the_review_findings_labels_no_check_with_an_ac_its_list_lacks() -> (
    None
):
    document = yaml.safe_load(
        (CONTRACT.parent / "OMN-24659.yaml").read_text(encoding="utf-8")
    )
    requirements = document["requirements"]
    stated = {item["id"] for item in requirements[0]["acceptance"]}
    assert stated == {"AC1", "AC2", "AC3", "AC4", "AC5"}
    for item in document["dod_evidence"]:
        for named in re.findall(r"-ac(\d+)", str(item["id"])):
            assert f"AC{named}" in stated, item["id"]
        for named in re.findall(r"\bAC(\d+)\b", str(item["description"])):
            assert f"AC{named}" in stated, (item["id"], "describes an AC it lacks")
    # the pull request body check survives under a label that claims no criterion
    scope = _dod(document, "dod-omn24659-pr-body-scope")
    assert scope["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in scope
    assert "ARCH_GRAPH_BOLT_URI" in scope["checks"][0]["check_value"]


def _installed_unit_script(tmp_path: Path) -> str:
    """The sh -c script of the installed-unit check, its host paths moved under ``tmp_path``."""
    command = _dod(_contract(), "dod-omn24642-ac4-installed-unit")["checks"][0][
        "check_value"
    ]
    script = shlex.split(command)[-1]
    assert "/data/omninode" in script
    return script.replace("/data/omninode", str(tmp_path / "data"))


def _installed_unit(
    tmp_path: Path, ticks: Sequence[str], *, age: timedelta = timedelta(0)
) -> int:
    """Run the installed-unit check over a unit and a refresh log whose ticks print ``ticks``."""
    home = tmp_path / "home"
    unit = home / ".config" / "systemd" / "user" / "onex-code-graph-refresh.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        "ExecStart=/venv/bin/python -m omnimarket.nodes.node_code_graph_effect refresh\n",
        encoding="utf-8",
    )
    log = (
        tmp_path
        / "data"
        / "registry"
        / ".onex_state"
        / "code-graph"
        / "code-graph-refresh.out.log"
    )
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"{tick}\n" for tick in ticks), encoding="utf-8")
    stamp = (datetime.now(UTC) - age).timestamp()
    os.utime(log, (stamp, stamp))
    done = subprocess.run(
        ["sh", "-c", _installed_unit_script(tmp_path)],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


FRESH_TICK = "REFRESH outcome=fresh changed=none built_at=none built_heads=none attempted_at=none reason=heads-unchanged"
FAILED_TICK = "REFRESH outcome=failed changed=none built_at=none built_heads=none attempted_at=none reason=heads-unreadable"


@pytest.mark.parametrize("outcome", ["refreshed", "fresh", "deferred"])
def test_contract_installed_unit_check_passes_when_the_last_tick_did_not_fail(
    tmp_path: Path, outcome: str
) -> None:
    tick = f"REFRESH outcome={outcome} changed=none built_at=none"
    assert _installed_unit(tmp_path, [FAILED_TICK, tick]) == 0


def test_contract_installed_unit_check_fails_when_every_tick_printed_failed(
    tmp_path: Path,
) -> None:
    assert _installed_unit(tmp_path, [FAILED_TICK, FAILED_TICK, FAILED_TICK]) != 0


def test_contract_installed_unit_check_fails_when_the_last_tick_failed(
    tmp_path: Path,
) -> None:
    assert _installed_unit(tmp_path, [FRESH_TICK, FRESH_TICK, FAILED_TICK]) != 0


def test_contract_installed_unit_check_fails_for_a_log_with_no_refresh_line(
    tmp_path: Path,
) -> None:
    assert _installed_unit(tmp_path, ["RUN status=deferred"]) != 0


def test_contract_installed_unit_check_fails_for_a_log_older_than_thirty_minutes(
    tmp_path: Path,
) -> None:
    assert _installed_unit(tmp_path, [FRESH_TICK], age=timedelta(hours=2)) != 0


def _status_row_check(ledger: Path) -> int:
    command = _dod(_contract(), "dod-omn24642-ac1-status-rows")["checks"][0][
        "check_value"
    ]
    # the variable the check reads the ledger path from is the one its own text names
    variable = re.search(r"\$\{(\w+):-", command)
    assert variable is not None
    done = subprocess.run(
        ["sh", "-c", command],
        env={variable.group(1): str(ledger), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


STATUS_HEAD = "2026-11-16T10:00:00Z | STATUS | lane=graphfresh-2300 | ticket=OMN-24642 | graph=code-graph"


@pytest.mark.parametrize(
    ("cells", "passes"),
    [
        (
            " | graph_built=2026-11-16T09:40:12Z | revisions=omnimarket@0123456789ab,omnibase_core@abcdef012345 | stale=false",
            True,
        ),
        (
            " | graph_built=2026-11-16T09:40:12Z | revisions=omnimarket@0123456789ab | stale=true",
            True,
        ),
        ("", False),
        (" | revisions=omnimarket@0123456789ab | stale=false", False),
        (" | graph_built=2026-11-16T09:40:12Z | stale=false", False),
        (
            " | graph_built=2026-11-16T09:40:12Z | revisions=omnimarket@0123456789ab",
            False,
        ),
        (
            " | graph_built=none | revisions=omnimarket@0123456789ab | stale=false",
            False,
        ),
        (
            " | graph_built=2026-11-16T09:40:12Z | revisions=omnimarket@0123456789ab | stale=unknown",
            False,
        ),
    ],
)
def test_contract_status_row_check_reads_build_time_revisions_and_staleness(
    tmp_path: Path, cells: str, passes: bool
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(f"{STATUS_HEAD}{cells}\n", encoding="utf-8")
    assert (_status_row_check(ledger) == 0) is passes


def test_the_refresh_schedule_installs_through_the_cli_unit_flag_in_a_dry_run(
    world: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the declared-overlay-root guard reads the lab host table, which has its own tests
    monkeypatch.setattr(
        runtime_code_graph, "refuse_undeclared_overlay_root", lambda *_a, **_k: None
    )
    code = main(
        [
            "schedule",
            "install",
            "--unit",
            "refresh",
            "--host",
            "example-host",
            "--workspace-root",
            str(tmp_path / "root"),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "onex-code-graph-refresh" in out
    assert "-m omnimarket.nodes.node_code_graph_effect refresh\n" in out
    assert "OnCalendar=*-*-* *:0/10:00 UTC" in out
    assert runtime_code_graph.SCHEDULE_BLOCKS["refresh"] == "refresh_schedule"


def test_every_writing_unit_renders_the_one_state_directory_variable(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(
        (Path(runtime_code_graph.__file__).parent / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    blocks = [
        runtime_code_graph.SCHEDULE_BLOCKS[unit]
        for unit in ("nightly", "drill", "refresh")
    ]
    directories = {contract[block]["state_directory"] for block in blocks}
    assert len(directories) == 1, (
        "the nightly, the drill and the refresh share one lock and one marker"
    )
    (directory,) = directories
    root = tmp_path / "root"
    for block in blocks:
        scheduler = CodeGraphScheduler(
            schedule=load_schedule(block=block),
            overlay=overlay(),
            home=tmp_path / "home",
            run=_Runner(),
        )
        result = scheduler.install(
            "example-host",
            omni_root=root,
            overlay_root=tmp_path / "overlays",
            dry_run=True,
        )
        assert result.exit_code == 0, result.stderr
        assert (
            f'Environment="ONEX_CODE_GRAPH_STATE_DIR={root / directory}"'
            in result.stdout
        ), block


def test_a_writing_run_that_waits_for_the_lock_builds_once_it_is_released(
    world: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = world["state"]
    assert isinstance(state, Path)
    ledger = world["ledger"]
    assert isinstance(ledger, InMemoryLedger)
    monkeypatch.setattr(runtime_code_graph, "BUILD_LOCK_WAIT", timedelta(seconds=30))
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        # a separate open file description, so the nightly's flock contends with it
        with build_lock(state) as held:
            assert held
            holding.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=10)
    threading.Timer(1.0, release.set).start()
    started = time.monotonic()
    code = main(["nightly"])
    waited = time.monotonic() - started
    holder.join(timeout=10)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "status=deferred" not in out
    assert waited >= 0.9, "the run waited for the holder instead of deferring at once"
    assert len(ledger.lines) == 1, "the waited-for run built and wrote its row"


def test_a_writing_run_that_waits_out_the_lock_defers_with_its_stated_reason(
    world: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = world["state"]
    assert isinstance(state, Path)
    ledger = world["ledger"]
    assert isinstance(ledger, InMemoryLedger)
    monkeypatch.setattr(runtime_code_graph, "BUILD_LOCK_WAIT", timedelta(seconds=1))
    with build_lock(state) as held:
        assert held
        code = main(["nightly"])
    out = capsys.readouterr().out
    # exit 0 by design: the holder is a writing build whose row the ledger gets, and a nonzero exit
    # would fail the drill unit, which counts exit 1 as success; the line says it waited and why
    assert code == 0
    assert "RUN status=deferred reason=build-lock-held waited=1s" in out
    assert ledger.lines == []


def test_repo_sources_head_fails_on_a_stale_fetch_record(tmp_path: Path) -> None:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    git_source = GitRepoSource(
        clones, max_file_bytes=1_000_000, max_fetch_age=timedelta(hours=1)
    )
    fetch_head = clones / "example_core" / ".git" / "FETCH_HEAD"
    fetch_head.write_text("", encoding="utf-8")
    assert len(git_source.head("example_core", REF)) == 40
    old = (datetime.now(UTC) - timedelta(hours=3)).timestamp()
    os.utime(fetch_head, (old, old))
    with pytest.raises(RepoSourceError, match="fetch-stale:example_core"):
        git_source.head("example_core", REF)


def test_repo_sources_head_reads_a_clone_with_no_fetch_record_and_no_age_limit(
    tmp_path: Path,
) -> None:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    limited = GitRepoSource(
        clones, max_file_bytes=1_000_000, max_fetch_age=timedelta(hours=1)
    )
    assert len(limited.head("example_core", REF)) == 40
    fetch_head = clones / "example_core" / ".git" / "FETCH_HEAD"
    fetch_head.write_text("", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(fetch_head, (old, old))
    unlimited = GitRepoSource(clones, max_file_bytes=1_000_000)
    assert len(unlimited.head("example_core", REF)) == 40


def test_repo_sources_a_stalled_fetch_makes_the_refresh_fail_and_not_read_fresh(
    world: dict[str, object], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert _tick(capsys)[0] == 0
    capsys.readouterr()
    fetch_head = tmp_path / "clones" / "example_core" / ".git" / "FETCH_HEAD"
    fetch_head.write_text("", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(hours=3)).timestamp()
    os.utime(fetch_head, (old, old))
    code, out, _err = _tick(capsys)
    assert code == 1, out
    assert "REFRESH outcome=failed" in out
    assert "reason=heads-unreadable:fetch-stale-example-core" in out
    assert "outcome=fresh" not in out


def test_the_refresh_reason_is_one_cell_safe_token_when_the_build_failed(
    tmp_path: Path,
) -> None:
    repos = source("memory", tmp_path)
    build = _WritingBuild(repos)

    def failing() -> ModelCodeGraphResult:
        result = build()
        return result.model_copy(
            update={
                "status": "failed",
                "failure": "append-failed:error:Ledger said No | try again\nline two",
            }
        )

    handler = CodeGraphRefresh(
        overlay=overlay(),
        heads=_StaticHeads(TWO_HEADS),
        state_directory=tmp_path / "state",
        run=failing,
        clock=lambda: RUN_AT,
    )
    result = handler.handle()
    assert result.outcome is EnumRefreshOutcome.FAILED
    assert result.reason == "append-failed-error-ledger-said-no-try-again-line-two"
    assert re.fullmatch(r"[a-z0-9-]+", result.reason)
