# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh on merge: build only when a repository head moved and the last build is old.

A tick whose heads are unchanged appends nothing and runs no build; a tick whose build is too
young defers; a tick that cannot read a head fails rather than passing as fresh; the schedule
renders its own ten-minute timer; and the writing build's marker is what the refresh reads.
"""

from __future__ import annotations

import json
import re
import subprocess
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


def test_contract_states_the_four_ticket_criteria_with_the_architecture_graph() -> None:
    document = _contract()
    assert "architecture graph" in str(document["title"])
    requirements = document["requirements"]
    assert isinstance(requirements, list)
    criteria = {item["id"]: item["statement"] for item in requirements[0]["acceptance"]}
    assert list(criteria) == ["AC1", "AC2", "AC3", "AC4"]
    assert "architecture graph" in criteria["AC1"]
    assert "NOT MET" in criteria["AC1"]
    assert "OMN-14572" in criteria["AC1"]
    assert (
        "installed from its branch and observed running before the pull request is handed off"
        in criteria["AC4"]
    )
    assert "polling timer" in criteria["AC3"]


def test_contract_binds_the_installed_unit_to_a_local_done_gate_check_on_h053() -> None:
    gate = _dod(_contract(), "dod-omn24642-ac4-installed-unit")
    assert gate["execution_scope"] == "local_done_gate"
    assert gate["binds_ac"] == ["AC4"]
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
