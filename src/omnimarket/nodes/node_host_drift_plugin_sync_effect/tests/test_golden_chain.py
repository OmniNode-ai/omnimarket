# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests for the data-plugin revision sync (OMN-20689).

The real backend: a real git repository holds an onex-overlays plugin at an UNCHANGED version 0.1.30
across two revisions; the installed record points at the older one. The failing control installs
nothing when the sync is replaced by the version compare (`claude plugin update` semantics), and the
real sync moves the installed tree and its recorded revision to the tip.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_drift_plugin_sync_effect.handlers.handler_plugin_sync import (
    HandlerHostDriftPluginSync,
    SubprocessPluginSyncGit,
    load_settings,
)
from omnimarket.nodes.node_host_drift_plugin_sync_effect.models.model_plugin_sync import (
    EnumPluginSyncOutcome as Outcome,
)
from omnimarket.nodes.node_host_drift_plugin_sync_effect.models.model_plugin_sync import (
    ModelPluginSyncRequest,
)

NAME = "onex-overlays@omninode-internal"
MANIFEST = "plugins/onex-overlays/.claude-plugin/plugin.json"
CLOCK = datetime(2026, 11, 8, 22, 0, 0, tzinfo=UTC)


def sh(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def commit(repo: Path, rel: str, text: str, version: str = "0.1.30") -> str:
    plugin = repo / "plugins/onex-overlays"
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "onex-overlays", "version": version})
    )
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    sh(repo, "add", "-A")
    sh(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        text,
    )
    return sh(repo, "rev-parse", "HEAD")


class Fixture:
    def __init__(self, tmp: Path) -> None:
        self.remote = tmp / "remote"
        self.remote.mkdir()
        sh(self.remote, "init", "-q", "-b", "main")
        self.old = commit(self.remote, "plugins/onex-overlays/data.txt", "old")
        self.clone = tmp / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(self.clone)], check=True
        )
        self.home = tmp / "home"
        self.cache = (
            self.home / ".claude/plugins/cache/omninode-internal/onex-overlays/0.1.30"
        )
        self.cache.mkdir(parents=True)
        sh(
            self.clone,
            "archive",
            "-o",
            str(tmp / "old.tar"),
            f"{self.old}:plugins/onex-overlays",
        )
        subprocess.run(
            ["tar", "-xf", str(tmp / "old.tar"), "-C", str(self.cache)], check=True
        )
        self.registry = self.home / ".claude/plugins/installed_plugins.json"
        self.registry.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        NAME: [
                            {
                                "scope": "user",
                                "installPath": str(self.cache),
                                "version": "0.1.30",
                                "gitCommitSha": self.old,
                            }
                        ]
                    },
                }
            )
        )

    def merge_same_version(self) -> str:
        return commit(self.remote, "plugins/onex-overlays/data.txt", "new")

    def request(self) -> ModelPluginSyncRequest:
        return ModelPluginSyncRequest(
            name=NAME,
            home=str(self.home),
            clone_path=str(self.clone),
            ref="main",
            manifest_path=MANIFEST,
        )

    def recorded(self) -> dict[str, str]:
        entry: dict[str, str] = json.loads(self.registry.read_text())["plugins"][NAME][
            0
        ]
        return entry


def _at(moment: datetime) -> datetime:
    return moment


def handler(git: SubprocessPluginSyncGit | None = None) -> HandlerHostDriftPluginSync:
    return HandlerHostDriftPluginSync(git=git, clock=lambda: CLOCK)


def version_compare_sync(fx: Fixture) -> bool:
    """The injected break: the old update-plugin step, which moves only when the versions differ."""
    merged: str = json.loads((fx.remote / MANIFEST).read_text())["version"]
    return bool(fx.recorded()["version"] != merged)


def test_failing_control_a_version_compare_never_moves_the_same_version(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path)
    fx.merge_same_version()
    assert version_compare_sync(fx) is False  # reads latest, so it does nothing
    assert (
        fx.cache / "data.txt"
    ).read_text() == "old"  # still 1 revision behind the merged tip
    assert fx.recorded()["gitCommitSha"] == fx.old
    # the sync by revision moves the same fixture
    assert handler().handle(fx.request()).outcome == Outcome.APPLIED
    assert (fx.cache / "data.txt").read_text() == "new"


def test_golden_same_version_older_revision_syncs_to_the_tip(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    tip = fx.merge_same_version()
    result = handler().handle(fx.request())
    assert result.outcome == Outcome.APPLIED, result.detail
    assert (result.previous_revision, result.installed_revision) == (fx.old, tip)
    assert fx.recorded()["gitCommitSha"] == tip
    assert (fx.cache / "data.txt").read_text() == "new"
    rollbacks = [p.name for p in fx.cache.parent.iterdir() if ".rollback-" in p.name]
    assert rollbacks == ["0.1.30.rollback-20261108T220000Z"]
    assert (fx.cache.parent / rollbacks[0] / "data.txt").read_text() == "old"


def test_golden_a_second_run_at_the_tip_changes_nothing(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.merge_same_version()
    assert handler().handle(fx.request()).outcome == Outcome.APPLIED
    again = handler().handle(fx.request())
    assert again.outcome == Outcome.ALREADY_AT_TIP


def test_golden_a_new_version_lands_in_its_own_directory(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    commit(fx.remote, "plugins/onex-overlays/data.txt", "v2", version="0.1.31")
    result = handler().handle(fx.request())
    assert result.outcome == Outcome.APPLIED, result.detail
    assert fx.recorded()["version"] == "0.1.31"
    assert Path(fx.recorded()["installPath"]).name == "0.1.31"
    assert (fx.cache.parent / "0.1.30" / "data.txt").read_text() == "old"


def test_golden_rollback_trees_are_pruned_to_the_overlay_bound(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    keep = load_settings().rollback_keep
    for i in range(keep + 2):
        commit(fx.remote, "plugins/onex-overlays/data.txt", f"r{i}")
        stamp = datetime(2026, 11, 8, 22, i, 0, tzinfo=UTC)
        assert (
            HandlerHostDriftPluginSync(clock=partial(_at, stamp))
            .handle(fx.request())
            .outcome
            == Outcome.APPLIED
        )
    rollbacks = [p for p in fx.cache.parent.iterdir() if ".rollback-" in p.name]
    assert len(rollbacks) == keep


def test_error_an_installed_revision_carrying_unmerged_plugin_commits_is_never_dropped(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path)
    sh(fx.clone, "checkout", "-q", "-b", "branch-fix")
    branch = commit(fx.clone, "plugins/onex-overlays/branch.txt", "branch fix")
    sh(fx.clone, "checkout", "-q", "main")
    fx.merge_same_version()
    entry = json.loads(fx.registry.read_text())
    entry["plugins"][NAME][0]["gitCommitSha"] = branch
    fx.registry.write_text(json.dumps(entry))
    result = handler().handle(fx.request())
    assert result.outcome == Outcome.ALERT_ONLY
    assert "would drop them" in result.detail
    assert "branch fix" in result.detail
    assert fx.recorded()["gitCommitSha"] == branch
    assert (fx.cache / "data.txt").read_text() == "old"


def test_error_an_unresolvable_ref_fails_and_installs_nothing(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    req = fx.request().model_copy(update={"ref": "no-such-ref", "fetch": False})
    result = handler().handle(req)
    assert result.outcome == Outcome.FAILED
    assert "does not resolve" in result.detail
    assert fx.recorded()["gitCommitSha"] == fx.old


def test_error_a_plugin_missing_from_the_record_is_alert_only(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.registry.write_text(json.dumps({"plugins": {}}))
    assert handler().handle(fx.request()).outcome == Outcome.ALERT_ONLY


def test_error_a_tip_whose_manifest_names_another_plugin_installs_nothing(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path)
    plugin = fx.remote / "plugins/onex-overlays/.claude-plugin/plugin.json"
    plugin.write_text(json.dumps({"name": "something-else", "version": "0.1.30"}))
    sh(fx.remote, "add", "-A")
    sh(
        fx.remote,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "wrong name",
    )
    result = handler().handle(fx.request())
    assert result.outcome == Outcome.FAILED
    assert "no readable manifest" in result.detail
    assert fx.recorded()["gitCommitSha"] == fx.old
    assert (fx.cache / "data.txt").read_text() == "old"
    assert not [p for p in fx.cache.parent.iterdir() if ".staging-" in p.name]


class FailingArchiveGit:
    def run(self, args: Sequence[str]) -> tuple[int, str]:
        if "archive" in args:
            return 128, "fatal: not a valid object name"
        return SubprocessPluginSyncGit().run(args)


def test_error_a_failed_archive_leaves_the_installed_tree_in_place(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path)
    fx.merge_same_version()
    result = HandlerHostDriftPluginSync(
        git=FailingArchiveGit(), clock=lambda: CLOCK
    ).handle(fx.request())
    assert result.outcome == Outcome.FAILED
    assert fx.recorded()["gitCommitSha"] == fx.old
    assert (fx.cache / "data.txt").read_text() == "old"


def test_error_a_manifest_path_outside_the_plugin_layout_is_refused(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path)
    req = fx.request().model_copy(
        update={"manifest_path": "plugins/onex-overlays/plugin.json"}
    )
    assert handler().handle(req).outcome == Outcome.ALERT_ONLY


@pytest.mark.parametrize(
    "field",
    [
        "rollback_keep",
        "rollback_infix",
        "staging_infix",
        "registry_rel",
        "manifest_rel",
        "sha_pattern",
    ],
)
def test_every_setting_comes_from_the_overlay(field: str) -> None:
    assert getattr(load_settings(), field)
