# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev runtime image carries no git history for the merge clone: the merge source refuses by name instead of running git
(rc=128), and a build whose merge history is absent still runs its checks and still alarms, with the suspects marked unknown."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters import git_merge_source
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    GitMergeSource,
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
from omnimarket.nodes.node_build_delegation_checks.protocols.protocol_build_checks import (
    MergeHistoryUnavailableError,
)

B1, B2 = "1" * 40, "2" * 40
T0 = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)


class _NoHistory:
    def merges_between(self, base_sha: str, head_sha: str) -> tuple[()]:
        raise MergeHistoryUnavailableError("merge-history-unavailable: image has none")


def test_a_clone_without_git_history_is_refused_by_name_and_git_is_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "omnimarket"
    clone.mkdir()  # the image's source tree: files, no .git

    def boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("git must not run against a tree with no history")

    monkeypatch.setattr(git_merge_source.subprocess, "run", boom)
    with pytest.raises(MergeHistoryUnavailableError, match="merge-history-unavailable"):
        GitMergeSource(clone).merges_between(B1, B2)


def test_a_git_failure_is_refused_by_name_with_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "omnimarket"
    (clone / ".git").mkdir(parents=True)

    def rc128(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="bad object")

    monkeypatch.setattr(git_merge_source.subprocess, "run", rc128)
    with pytest.raises(MergeHistoryUnavailableError, match="merge-history-unavailable"):
        GitMergeSource(clone).merges_between(B1, B2)


def test_delegation_check_regression_build_with_no_merge_history_still_alarms() -> None:
    green = ModelCheckOutcome(reading=EnumCheckReading.PASS, run_id="p")
    red = ModelCheckOutcome(reading=EnumCheckReading.FAIL, run_id="f")
    sink = RecordingSink()
    handler = HandlerBuildDelegationChecks(
        settings=ModelBuildDelegationChecksOverlay(
            checks=("C14",), owner="delegation-owner"
        ),
        runner=RecordedResultsExecutor({B1: {"C14": green}, B2: {"C14": red}}),
        store=InMemoryBuildStore(),
        merges=_NoHistory(),
        sink=sink,
        clock=lambda: T0 + timedelta(minutes=90),
    )
    handler.handle(ModelRebuildCompletedObserved(git_sha=B1, runtime_lane="dev"))
    result = handler.handle(
        ModelRebuildCompletedObserved(git_sha=B2, runtime_lane="dev")
    )

    assert result.status is EnumBuildChecksStatus.RAN
    assert [r.check_id for r in result.regressions] == ["C14"]
    assert len(sink.alarms) == 1
    assert "unknown" in sink.alarms[0].alarms[0].detail
    assert "the build added no merge" not in sink.alarms[0].alarms[0].detail
    assert any(
        r.check_id == "*" and r.reason.startswith("merge-history-unavailable")
        for r in result.refused
    )
