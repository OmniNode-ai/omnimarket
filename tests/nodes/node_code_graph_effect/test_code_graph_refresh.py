# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh on merge: build only when a repository head moved and the last build is old.

A tick whose heads are unchanged appends nothing and runs no build; a tick whose build is too
young defers; a tick that cannot read a head fails rather than passing as fresh; the schedule
renders its own ten-minute timer; and the writing build's marker is what the refresh reads.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.model_code_graph import (
    EnumGraphFault,
    EnumRefreshOutcome,
    ModelCodeGraphCommand,
    ModelCodeGraphRefreshResult,
    ModelCodeGraphResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_refresh import (
    CodeGraphRefresh,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
)
from omnimarket.nodes.node_code_graph_effect.handlers.schedule import (
    CodeGraphScheduler,
    load_schedule,
)
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolRepoSource,
    RepoSourceError,
)
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import _keep
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    HOST,
    REF,
    RUN_AT,
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
        return self._handler.handle(
            ModelCodeGraphCommand(correlation_id=f"refresh-{self.calls}")
        )


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
