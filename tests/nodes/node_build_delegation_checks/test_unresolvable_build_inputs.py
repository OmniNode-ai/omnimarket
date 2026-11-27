# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two failures the dev container logged on every rebuild-completed event of 2026-11-26 (OMN-26121).

12:00:57Z: ``ValidationError ModelRebuildCompletedObserved git_sha String should have at least 1 character``.
11:12:32Z: ``OSError: git log c0622a1dbb01..0c38cce0230c failed: rc=128`` (the previous build is absent from the clone).
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    GitMergeSource,
    UnresolvableRangeError,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    InMemoryBuildStore,
    RecordedResultsExecutor,
    RecordingSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    EnumCheckReading,
    ModelCheckOutcome,
    ModelRebuildCompletedObserved,
)

T0 = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)
PASS = ModelCheckOutcome(reading=EnumCheckReading.PASS, run_id="r1")
ABSENT = "c0622a1dbb01" + "0" * 28  # a build sha the clone does not hold


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under a git hook GIT_DIR and friends override ``-C``: the temporary clone must be the one read, never the invoking worktree.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)


def _settings() -> ModelBuildDelegationChecksOverlay:
    return ModelBuildDelegationChecksOverlay(checks=("C14",), owner="delegation-owner")


def _clone(path: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            env=scrub_git_location_env(os.environ),
            capture_output=True,
            text=True,
        ).stdout.strip()

    path.mkdir()
    git("init", "-q")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "head (#1)",
    )
    return git("rev-parse", "HEAD")


def _handler(
    clone: Path, runner: RecordedResultsExecutor
) -> HandlerBuildDelegationChecks:
    return HandlerBuildDelegationChecks(
        settings=_settings(),
        runner=runner,
        store=InMemoryBuildStore(),
        merges=GitMergeSource(clone),
        sink=RecordingSink(),
        clock=lambda: T0 + timedelta(minutes=90),
    )


def test_an_empty_git_sha_event_is_a_typed_refusal_not_a_validation_error(
    tmp_path: Path,
) -> None:
    runner = RecordedResultsExecutor({})
    handler = _handler(tmp_path, runner)
    # the payload shape the deploy agent published when a job died before git_pull
    event = ModelRebuildCompletedObserved.model_validate(
        {"git_sha": "", "runtime_lane": "dev", "status": "success"}
    )
    result = handler.handle(event)
    assert result.status is EnumBuildChecksStatus.REFUSED
    assert "git_sha" in result.reason
    assert runner.calls == []


def test_a_previous_build_absent_from_the_clone_runs_the_checks_with_merges_unknown(
    tmp_path: Path,
) -> None:
    head = _clone(tmp_path / "clone")
    runner = RecordedResultsExecutor({ABSENT: {"C14": PASS}, head: {"C14": PASS}})
    handler = _handler(tmp_path / "clone", runner)
    handler.handle(ModelRebuildCompletedObserved(git_sha=ABSENT, runtime_lane="dev"))

    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=head, runtime_lane="dev")
    )

    assert result.status is EnumBuildChecksStatus.RAN
    assert result.previous_build_id == ABSENT
    assert [r.reading for r in result.records] == [EnumCheckReading.PASS]
    assert all(r.merges_unknown for r in result.records)
    assert all(r.merges == () for r in result.records)


def test_the_git_adapter_names_an_unresolvable_range_by_type(tmp_path: Path) -> None:
    head = _clone(tmp_path / "clone")
    with pytest.raises(UnresolvableRangeError, match="rc=128"):
        GitMergeSource(tmp_path / "clone").merges_between(ABSENT, head)


def test_a_resolvable_range_still_reads_its_merges_as_known(tmp_path: Path) -> None:
    head = _clone(tmp_path / "clone")
    runner = RecordedResultsExecutor({head: {"C14": PASS}})
    handler = _handler(tmp_path / "clone", runner)
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=head, runtime_lane="dev")
    )
    assert result.status is EnumBuildChecksStatus.RAN
    assert not any(r.merges_unknown for r in result.records)
