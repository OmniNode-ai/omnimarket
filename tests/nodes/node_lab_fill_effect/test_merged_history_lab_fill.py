# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill reads merged work from the canonical clones beside the PR watcher, and never dispatches a ticket either holds (OMN-23376)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_merged_history import (
    ModelLabFillMergedHistory,
    load_merged_history,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillPortError
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalMergedWork,
    LocalOwnerReader,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_merged_history import (
    LocalMergedHistory,
)

pytestmark = pytest.mark.unit

SETTINGS = ModelLabFillMergedHistory(
    repos=("repo_a", "repo_b"),
    base_refs=("origin/dev", "origin/main"),
    since_days=120,
    evidence_prefix="evidence(",
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def _clone(root: Path, name: str, subjects: list[str], ref: str = "dev") -> Path:
    path = root / name
    path.mkdir()
    _git(path, "init", "-q", "-b", ref)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    for i, subject in enumerate(subjects):
        (path / f"f{i}").write_text(str(i))
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", subject)
    # The canonical clones read origin/<branch>: point that ref at the branch.
    _git(path, "update-ref", f"refs/remotes/origin/{ref}", "HEAD")
    return path


def test_the_settings_come_from_the_nodes_contract() -> None:
    settings = load_merged_history()
    assert "omnimarket" in settings.repos
    assert settings.base_refs[0] == "origin/dev"
    assert settings.evidence_prefix == "evidence("


def test_the_deployment_overlay_adds_the_private_clones(tmp_path: Path) -> None:
    overlay = tmp_path / "node_lab_fill_effect"
    overlay.mkdir()
    (overlay / "overlay.yaml").write_text(
        "extra_repos: [private_x]\n", encoding="utf-8"
    )
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    assert load_merged_history(env).repos[-1] == "private_x"
    assert "private_x" not in load_merged_history({}).repos
    (overlay / "overlay.yaml").write_text("extra_repos: nope\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError, match="extra_repos"):
        load_merged_history(env)


def test_a_merge_only_a_clone_holds_is_read(tmp_path: Path) -> None:
    _clone(tmp_path, "repo_a", ["fix(OMN-22623): the trigger key (#5177)", "other"])
    _clone(tmp_path, "repo_b", ["feat(OMN-22651): piece 1 (#12)"], ref="main")
    history = LocalMergedHistory(SETTINGS, root=tmp_path)
    assert history.merged("OMN-22623") == ("repo_a#5177",)
    assert history.merged("OMN-22651") == ("repo_b#12",)
    assert history.merged("OMN-1") == ()


def test_a_longer_ticket_number_is_not_the_ticket_asked_about(tmp_path: Path) -> None:
    _clone(tmp_path, "repo_a", ["fix(OMN-226230): other ticket (#9)"])
    assert LocalMergedHistory(SETTINGS, root=tmp_path).merged("OMN-22623") == ()


def test_an_evidence_commit_does_not_count(tmp_path: Path) -> None:
    _clone(tmp_path, "repo_a", ["evidence(OMN-22623): bind the receipt (#901)"])
    assert LocalMergedHistory(SETTINGS, root=tmp_path).merged("OMN-22623") == ()


def test_no_readable_clone_is_unread_never_clear(tmp_path: Path) -> None:
    with pytest.raises(LabFillPortError, match="no clone could be read"):
        LocalMergedHistory(SETTINGS, root=tmp_path).merged("OMN-22623")


class _Watcher(LocalOwnerReader):
    """The owner reader with its watcher read scripted."""

    def __init__(
        self, merged: dict[str, list[str]] | None = None, fail: bool = False
    ) -> None:
        super().__init__()
        self._merged, self._fail = merged or {}, fail

    def watcher_merged(self, state_path: str) -> Mapping[str, Sequence[str]]:
        if self._fail:
            raise LabFillPortError("unreadable-from-snapshot")
        return self._merged


def _work(tmp_path: Path, watcher: _Watcher) -> LocalMergedWork:
    return LocalMergedWork(
        reader=watcher,
        history=LocalMergedHistory(SETTINGS, root=tmp_path),
    )


def test_a_ticket_merged_before_the_watchers_window_is_still_merged(
    tmp_path: Path,
) -> None:
    """The failing control: the watcher no longer holds the merge, so the old read answered nothing and the ticket was dispatched."""
    _clone(tmp_path, "repo_a", ["fix(OMN-22623): the trigger key (#5177)"])
    assert _work(tmp_path, _Watcher()).merged_prs("OMN-22623", "") == ("repo_a#5177",)


def test_the_watcher_and_the_clones_join(tmp_path: Path) -> None:
    _clone(tmp_path, "repo_a", ["fix(OMN-22623): a (#1)"])
    work = _work(tmp_path, _Watcher({"OMN-22623": ["repo_b#2"]}))
    assert work.merged_prs("OMN-22623", "") == ("repo_a#1", "repo_b#2")


def test_a_stale_watcher_does_not_hide_a_merge_a_clone_holds(tmp_path: Path) -> None:
    _clone(tmp_path, "repo_a", ["fix(OMN-22623): a (#1)"])
    assert _work(tmp_path, _Watcher(fail=True)).merged_prs("OMN-22623", "") == (
        "repo_a#1",
    )


def test_a_stale_watcher_and_no_merge_in_the_clones_is_unread(tmp_path: Path) -> None:
    _clone(tmp_path, "repo_a", ["something else"])
    with pytest.raises(LabFillPortError, match="unreadable-from-snapshot"):
        _work(tmp_path, _Watcher(fail=True)).merged_prs("OMN-22623", "")


def test_built_reports_read_the_ledger_rows_of_the_ticket(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-11-09T14:29:11Z | TERMINAL | lane=fill-ticket-OMN-9-x | ticket=OMN-9 | outcome=skipped-already-built | falsifiers=pass | evidence=ran it\n"
        "2026-11-09T14:30:00Z | TERMINAL | lane=fill-ticket-OMN-8-x | ticket=OMN-8 | outcome=skipped-already-built | falsifiers=pass\n",
        encoding="utf-8",
    )
    reports = _work(tmp_path, _Watcher()).built_reports("OMN-9", str(ledger))
    assert [(r.ticket, r.falsifiers_passed, r.evidence) for r in reports] == [
        ("OMN-9", True, "ran it")
    ]
    with pytest.raises(LabFillPortError, match="ledger unreadable"):
        _work(tmp_path, _Watcher()).built_reports("OMN-9", str(tmp_path / "none.md"))
