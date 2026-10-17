# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host tick clone, daemon and maintenance actions through an in-memory host boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, TypedDict

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.nodes.node_lab_daemon_sync_effect.__main__ import (
    DEFAULT_OVERLAY,
    load_overlay,
)
from omnimarket.nodes.node_lab_daemon_sync_effect.handlers.handler_lab_daemon_sync import (
    HandlerLabDaemonSync,
)
from omnimarket.nodes.node_lab_daemon_sync_effect.models.model_lab_daemon_sync import (
    ModelCloneSet,
    ModelDaemonOverlay,
    ModelLabDaemonInstallRefused,
    ModelLabDaemonSyncCommand,
    ModelLabDaemonSyncResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-17T03:00:00Z"
OLD = "a" * 40
NEW = "b" * 40
ROOT = "/registry"
REPO = "tool-repo"
PATH = f"{ROOT}/{REPO}"
VERSION = "0.4.423"
REFUSED = "refused"
COMPLETED = "completed"


class RepoState(TypedDict, total=False):
    exists: bool
    branch: str | None
    origin_head: str | None
    dirty: bool
    head: str | None
    upstream: str | None
    ancestor: bool
    clone_rc: int
    ff_rc: int


class FakeHost:
    """Interpret git argv against repo state, and retain every call and file in memory."""

    def __init__(self, repo: RepoState | None = None) -> None:
        self.repos: dict[str, RepoState] = {
            PATH: {
                "exists": True,
                "branch": "main",
                "origin_head": "origin/main",
                "dirty": False,
                "head": OLD,
                "upstream": NEW,
                "ancestor": True,
                "clone_rc": 0,
                "ff_rc": 0,
                **(repo or {}),
            }
        }
        self.calls: list[tuple[list[str], int]] = []
        self.files: dict[str, str] = {}
        self.absent: set[str] = set()
        self.responses: dict[tuple[str, ...], tuple[int, str, str]] = {}
        self.installed: str | None = None

    def run(self, argv: list[str], timeout_s: int) -> tuple[int, str, str]:
        self.calls.append((list(argv), timeout_s))
        if tuple(argv) in self.responses:
            return self.responses[tuple(argv)]
        if argv[0] == "git":
            if argv[1] == "clone":
                assert argv[2:4] == ["--quiet", f"https://example.test/{REPO}.git"]
                repo = self.repos[argv[4]]
                rc = repo.get("clone_rc", 0)
                if rc == 0:
                    repo["exists"] = True
                    repo["head"] = repo["upstream"]
                return rc, "", "clone blocked" if rc else ""
            assert argv[1] == "-C"
            repo = self.repos[argv[2]]
            args = argv[3:]
            if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                branch = repo["branch"]
                return (0, branch, "") if branch else (1, "", "detached")
            if args == [
                "symbolic-ref",
                "--quiet",
                "--short",
                "refs/remotes/origin/HEAD",
            ]:
                default = repo["origin_head"]
                return (0, default, "") if default else (1, "", "unknown default")
            if args == ["fetch", "--quiet", "origin", repo["branch"]]:
                return 0, "", ""
            if args == ["status", "--porcelain", "--untracked-files=no"]:
                return 0, " M tracked.py\n" if repo["dirty"] else "", ""
            if args[0] == "rev-parse":
                ref = repo["head"] if args[1] == "HEAD" else repo["upstream"]
                return (0, ref, "") if ref else (128, "", "unknown revision")
            if args == [
                "merge-base",
                "--is-ancestor",
                "HEAD",
                f"origin/{repo['branch']}",
            ]:
                return (0 if repo["ancestor"] else 1), "", ""
            if args == ["merge", "--ff-only", "--quiet", f"origin/{repo['branch']}"]:
                rc = repo["ff_rc"]
                if rc == 0:
                    repo["head"] = repo["upstream"]
                return rc, "", "merge blocked" if rc else ""
        if argv[0] == "fix-lane":
            return 0, "detached", ""
        if argv == ["pre-commit", "gc"]:
            return 0, "  3 repos removed\n", ""
        if argv == ["probe-installed"]:
            return (0, self.installed, "") if self.installed else (1, "", "absent")
        if argv == ["probe-source"]:
            return 0, VERSION, ""
        if argv == ["install-plugin"]:
            self.installed = VERSION
            return 0, "", ""
        raise AssertionError(f"unexpected command: {argv}")

    def exists(self, path: str) -> bool:
        if path in self.repos:
            return self.repos[path].get("exists", False)
        return path not in self.absent

    def read_json(self, path: str) -> dict[str, object] | None:
        if path not in self.files:
            return None
        data: dict[str, object] = json.loads(self.files[path])
        return data

    def write_text(self, path: str, text: str) -> None:
        self.files[path] = text

    def lock_busy(self, path: str) -> bool:
        return False

    def commands(self, executable: str) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[0] == executable]


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.events.append((topic, event))

    def alarm(self) -> ModelLabDaemonInstallRefused:
        topic, event = self.events[0]
        assert topic == REFUSED
        assert isinstance(event, ModelLabDaemonInstallRefused)
        return event


def overlay(
    *,
    clones: bool = True,
    chores: bool = False,
    plugin: bool = False,
    absent_is_lag: bool = True,
) -> ModelDaemonOverlay:
    return ModelDaemonOverlay.model_validate(
        {
            "schema_version": 1,
            "fix_lane": ["fix-lane", "{lane}", "{brief}", "{daemon}", "{host}"],
            "clones": {
                "root": ROOT,
                "url": "https://example.test/{repo}.git",
                "repos": [REPO],
                "timeout_s": 42,
            }
            if clones
            else None,
            "chores": [
                {"name": "pre-commit-gc", "argv": ["pre-commit", "gc"], "timeout_s": 17}
            ]
            if chores
            else [],
            "daemons": [
                {
                    "name": "omni-plugin",
                    "installed": {"kind": "command", "argv": ["probe-installed"]},
                    "source": {"kind": "command", "argv": ["probe-source"]},
                    "installer": ["install-plugin"],
                    "absent_is_lag": absent_is_lag,
                }
            ]
            if plugin
            else [],
        }
    )


def run(
    host: FakeHost,
    declaration: ModelDaemonOverlay,
    *,
    mode: Literal["repair", "check"] = "repair",
    now: str = NOW,
) -> tuple[ModelLabDaemonSyncResult, FakePublisher]:
    publisher = FakePublisher()
    handler = HandlerLabDaemonSync(host, publisher, REFUSED, COMPLETED)
    result = handler.handle(
        ModelLabDaemonSyncCommand(
            host="lab",
            state_dir="/state",
            overlay=declaration,
            now=now,
            mode=mode,
        )
    )
    return result, publisher


@pytest.mark.parametrize("mode", ["repair", "check"])
def test_missing_clone(mode: Literal["repair", "check"]) -> None:
    host = FakeHost({"exists": False})
    result, _ = run(host, overlay(), mode=mode)
    assert result.clones[0].verdict == ("created" if mode == "repair" else "missing")
    clone_calls = [argv for argv in host.commands("git") if argv[1] == "clone"]
    if mode == "repair":
        assert clone_calls == [
            ["git", "clone", "--quiet", f"https://example.test/{REPO}.git", PATH]
        ]
        assert result.clones[0].after == NEW
        assert result.changes == [f"clone {REPO}: created at {NEW[:12]}"]
    else:
        assert clone_calls == []
        assert result.changes == []
    assert result.exit_code == 0


def test_clean_behind_clone_fast_forwards() -> None:
    host = FakeHost()
    result, _ = run(host, overlay())
    assert result.clones[0].verdict == "fast_forwarded"
    assert [
        "git",
        "-C",
        PATH,
        "merge",
        "--ff-only",
        "--quiet",
        "origin/main",
    ] in host.commands("git")
    assert result.changes == [f"clone {REPO}: fast-forwarded {OLD[:12]}..{NEW[:12]}"]
    assert all(timeout == 42 for argv, timeout in host.calls if argv[0] == "git")


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        ({"dirty": True}, "dirty"),
        ({"branch": None}, "off_branch"),
        ({"branch": "feature"}, "off_branch"),
        ({"ancestor": False}, "diverged"),
    ],
)
def test_clone_user_work_is_reported_without_merge(
    state: RepoState, verdict: str
) -> None:
    host = FakeHost(state)
    result, publisher = run(host, overlay())
    assert result.clones[0].verdict == verdict
    if state.get("branch", "main") is None:
        assert result.clones[0].branch is None
    assert not any("merge" in argv for argv in host.commands("git"))
    assert host.commands("fix-lane") == []
    assert result.exit_code == 0
    assert result.changes == []
    assert [topic for topic, _ in publisher.events] == [COMPLETED]


def test_failed_clone_alarms_and_deduplicates_fix_lane_across_ticks() -> None:
    host = FakeHost({"exists": False, "clone_rc": 128})
    declaration = overlay()
    result, publisher = run(host, declaration)
    assert result.clones[0].verdict == "refused"
    assert result.exit_code == 1
    alarm = publisher.alarm()
    assert alarm.daemon == f"clone-{REPO}"
    assert alarm.blocker == "git clone rc=128: clone blocked"
    assert alarm.fix_lane_dispatched
    assert alarm.correlation_id == result.correlation_id
    assert len(host.commands("fix-lane")) == 1
    brief = next(text for path, text in host.files.items() if "/briefs/" in path)
    assert all(
        word in brief
        for word in [
            "lab",
            REPO,
            alarm.blocker,
            "remove the blocker so the next host tick converges it",
        ]
    )
    host.calls.clear()
    _, second = run(host, declaration, now="2026-10-17T03:05:00Z")
    assert host.commands("fix-lane") == []
    assert not second.alarm().fix_lane_dispatched
    assert second.alarm().lagging_since == NOW
    _, third = run(host, declaration, now="2026-10-17T10:00:00Z")
    assert third.alarm().fix_lane_dispatched


@pytest.mark.parametrize("mode", ["repair", "check"])
def test_chore_output_or_due(mode: Literal["repair", "check"]) -> None:
    host = FakeHost()
    result, _ = run(host, overlay(clones=False, chores=True), mode=mode)
    assert result.chores[0].verdict == ("ran" if mode == "repair" else "due")
    if mode == "repair":
        assert host.calls == [(["pre-commit", "gc"], 17)]
        assert result.chores[0].output == "3 repos removed"
        assert result.changes == ["pre-commit-gc: 3 repos removed"]
    else:
        assert host.calls == []
        assert result.changes == []


def test_failing_chore_refuses_and_deduplicates_alarm() -> None:
    host = FakeHost()
    host.responses[("pre-commit", "gc")] = (2, "", "cache blocked\n")
    declaration = overlay(clones=False, chores=True)
    result, publisher = run(host, declaration)
    assert result.chores[0].verdict == "refused"
    assert result.exit_code == 1
    assert publisher.alarm().daemon == "chore-pre-commit-gc"
    assert publisher.alarm().blocker == "pre-commit rc=2: cache blocked"
    assert publisher.alarm().fix_lane_dispatched
    host.calls.clear()
    _, second = run(host, declaration, now="2026-10-17T03:05:00Z")
    assert not second.alarm().fix_lane_dispatched
    assert host.commands("fix-lane") == []


@pytest.mark.parametrize(
    ("absent_is_lag", "mode", "verdict"),
    [
        (True, "repair", "installed"),
        (True, "check", "lagging"),
        (False, "repair", "refused"),
    ],
)
def test_absent_plugin(
    absent_is_lag: bool, mode: Literal["repair", "check"], verdict: str
) -> None:
    host = FakeHost()
    result, _ = run(
        host, overlay(clones=False, plugin=True, absent_is_lag=absent_is_lag), mode=mode
    )
    assert result.daemons[0].verdict == verdict
    assert host.commands("install-plugin") == (
        [["install-plugin"]] if verdict == "installed" else []
    )
    assert result.changes == (
        ["omni-plugin: absent -> 0.4.423"] if verdict == "installed" else []
    )
    if mode == "check":
        assert result.daemons[0].installed_revision is None


def test_completed_event_state_and_execution_order() -> None:
    host = FakeHost({"exists": False})
    result, publisher = run(host, overlay(chores=True, plugin=True))
    assert publisher.events[-1] == (COMPLETED, result)
    completed = publisher.events[-1][1]
    assert isinstance(completed, ModelLabDaemonSyncResult)
    assert completed.clones[0].verdict == "created"
    assert completed.chores[0].verdict == "ran"
    assert completed.changes == [
        f"clone {REPO}: created at {NEW[:12]}",
        "omni-plugin: absent -> 0.4.423",
        "pre-commit-gc: 3 repos removed",
    ]
    assert host.calls[0][0][0:2] == ["git", "clone"]
    assert host.calls[-1][0] == ["pre-commit", "gc"]
    state = json.loads(host.files["/state/lab.json"])
    assert state["clones"] == [item.model_dump(mode="json") for item in result.clones]
    assert state["chores"] == [item.model_dump(mode="json") for item in result.chores]
    assert f"clone-{REPO}" in state["daemons"]
    assert "chore-pre-commit-gc" in state["daemons"]


def test_load_overlay_drops_unset_clones_and_chores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LDS_ROOT", raising=False)
    monkeypatch.delenv("LDS_URL", raising=False)
    monkeypatch.setenv("LDS_PRESENT", "/cache")
    path = tmp_path / "overlay.yaml"
    path.write_text("""\
schema_version: 1
fix_lane: [fix-lane]
daemons: []
clones:
  root: ${LDS_ROOT}
  url: ${LDS_URL}/{repo}.git
  repos: [example]
chores:
  - name: dropped
    argv: [gc, $LDS_ROOT]
  - name: kept
    present_when: ${LDS_PRESENT}
    argv: [gc, $LDS_PRESENT]
""")
    diagnostics: list[str] = []
    declaration = load_overlay(path, diagnostics)
    assert declaration.clones is None
    assert "clones: skipped, unset LDS_ROOT, LDS_URL" in diagnostics
    assert "dropped: skipped, unset LDS_ROOT" in diagnostics
    assert [chore.name for chore in declaration.chores] == ["kept"]
    assert declaration.chores[0].argv == ["gc", "/cache"]
    monkeypatch.setenv("LDS_ROOT", ROOT)
    monkeypatch.setenv("LDS_URL", "https://example.test")
    declaration = load_overlay(path, [])
    assert declaration.clones is not None
    assert declaration.clones.root == ROOT
    assert declaration.clones.url == "https://example.test/{repo}.git"


def test_shipped_overlay_declares_host_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_HOME", "/registry")
    monkeypatch.setenv("OMNI_PLUGIN_ROOT", "/plugin")
    monkeypatch.setenv("ONEX_LAB_PRIVATE_CLONES_FILE", "/hosts/private-clones.json")
    declaration = load_overlay(DEFAULT_OVERLAY, [])
    assert declaration.clones is not None
    assert "omnimarket" in declaration.clones.repos
    assert declaration.clones.extra_repos_file == "/hosts/private-clones.json"
    assert "pre-commit-gc" in {chore.name for chore in declaration.chores}
    assert {"omni-plugin", "onex-overlays-plugin"} <= {
        daemon.name for daemon in declaration.daemons
    }


@pytest.mark.parametrize(
    "state", [{"head": OLD, "upstream": OLD}, {"origin_head": None}]
)
def test_current_or_unknown_default_branch(state: RepoState) -> None:
    result, _ = run(FakeHost(state), overlay())
    assert result.clones[0].verdict == (
        "current" if "head" in state else "fast_forwarded"
    )


def test_check_behind_clone_does_not_fetch_or_merge() -> None:
    host = FakeHost()
    result, _ = run(host, overlay(), mode="check")
    assert result.clones[0].verdict == "behind"
    assert not any("fetch" in argv or "merge" in argv for argv in host.commands("git"))
    assert result.changes == []


@pytest.mark.parametrize(
    ("state", "blocker"),
    [
        ({"head": None}, "git rev-parse HEAD unreadable"),
        ({"upstream": None}, "git rev-parse origin/main unreadable"),
        ({"ff_rc": 1}, "git merge rc=1: merge blocked"),
    ],
)
def test_clone_read_or_merge_failure_alarms(state: RepoState, blocker: str) -> None:
    result, publisher = run(FakeHost(state), overlay())
    assert result.clones[0].verdict == "refused"
    assert blocker in publisher.alarm().blocker
    assert result.exit_code == 1


def test_clone_fetch_failure_alarms() -> None:
    host = FakeHost()
    host.responses[("git", "-C", PATH, "fetch", "--quiet", "origin", "main")] = (
        128,
        "",
        "offline",
    )
    result, publisher = run(host, overlay())
    assert result.clones[0].verdict == "refused"
    assert publisher.alarm().blocker == "git fetch rc=128: offline"
    assert result.exit_code == 1


@pytest.mark.parametrize(
    "extra", [{"hosts": ["another-host"]}, {"present_when": "/missing"}]
)
def test_chore_host_and_presence_filters(extra: dict[str, object]) -> None:
    host = FakeHost()
    host.absent.add("/missing")
    raw = overlay(clones=False, chores=True).model_dump()
    raw["chores"][0].update(extra)
    result, _ = run(host, ModelDaemonOverlay.model_validate(raw))
    assert result.chores == []
    assert host.calls == []


def test_chore_output_matching_no_change_is_not_a_change() -> None:
    overlay = ModelDaemonOverlay.model_validate(
        yaml.safe_load(DEFAULT_OVERLAY.read_text())
    )
    gc = next(chore for chore in overlay.chores if chore.name == "pre-commit-gc")
    assert gc.no_change is not None
    assert re.search(gc.no_change, "0 repo(s) removed.")
    assert not re.search(gc.no_change, "3 repo(s) removed.")


def test_extra_repos_file_adds_host_named_clones() -> None:
    declaration = overlay(clones=True, chores=False)
    assert declaration.clones is not None
    clones = declaration.clones.model_copy(
        update={"extra_repos_file": "/hosts/private-clones.json"}
    )
    host = FakeHost()
    host.files["/hosts/private-clones.json"] = json.dumps(
        {"repos": ["second-repo", "../x", "..", ".", "-flag"]}
    )
    diagnostics: list[str] = []
    handler = HandlerLabDaemonSync(
        host=host, publisher=FakePublisher(), refused_topic="r", completed_topic="c"
    )
    assert handler._clone_repos(clones, diagnostics) == [REPO, "second-repo"]
    assert diagnostics == [
        f"clones: {name!r} is not a repository name"
        for name in ("../x", "..", ".", "-flag")
    ]


@pytest.mark.parametrize("repo", ["..", ".", "-flag", "a/b", ""])
def test_overlay_refuses_unsafe_repo_name(repo: str) -> None:
    declaration = overlay(clones=True, chores=False)
    assert declaration.clones is not None
    clones = declaration.clones.model_dump() | {"repos": [repo]}
    with pytest.raises(ValidationError):
        ModelCloneSet.model_validate(clones)


@pytest.mark.parametrize(
    "url", ["http://example.test/{repo}.git", "git@example.test:{repo}.git", "x{repo}"]
)
def test_overlay_refuses_non_https_clone_url(url: str) -> None:
    declaration = overlay(clones=True, chores=False)
    assert declaration.clones is not None
    with pytest.raises(ValidationError):
        ModelCloneSet.model_validate(declaration.clones.model_dump() | {"url": url})


@pytest.mark.parametrize(
    "part", ["{lane.__class__}", "{unknown}", "{host!r}", "{brief:>9}", "{0}"]
)
def test_overlay_refuses_fix_lane_part_outside_the_four_fields(part: str) -> None:
    declaration = overlay(clones=False, chores=False)
    with pytest.raises(ValidationError, match="fix_lane part"):
        ModelDaemonOverlay.model_validate(
            declaration.model_dump() | {"fix_lane": ["runner", part]}
        )


def test_overlay_accepts_the_four_fix_lane_fields() -> None:
    declaration = overlay(clones=False, chores=False)
    parts = ["runner", "{lane}", "{brief}", "{daemon}", "{host}", "plain"]
    assert (
        ModelDaemonOverlay.model_validate(
            declaration.model_dump() | {"fix_lane": parts}
        ).fix_lane
        == parts
    )


def test_absent_plugin_with_no_marketplace_names_the_source_blocker() -> None:
    host = FakeHost()
    host.responses[("probe-source",)] = (
        1,
        "",
        "marketplace omninode-internal not found\n",
    )
    result, publisher = run(
        host, overlay(clones=False, plugin=True, absent_is_lag=True)
    )
    assert result.daemons[0].verdict == "refused"
    assert result.daemons[0].blocker == (
        "source revision unreadable: rc=1 marketplace omninode-internal not found"
    )
    assert publisher.alarm().daemon == "omni-plugin"
    assert host.commands("install-plugin") == []
