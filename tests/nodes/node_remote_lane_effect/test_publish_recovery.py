# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish recovery through the effect contract and real, local Git remotes.

The recorded outcomes were push-non-fast-forward, push-failed and pr-head-moved.
The histories below reproduce those interleavings; no network or PR API is used.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_remote_lane_effect.models.model_remote_lane_publish import (
    ModelRemoteLanePublishRequest,
    ModelRemoteLanePublishResult,
)
from omnimarket.nodes.node_remote_lane_effect.protocols.local_remote_lane_adapters import (
    LocalRemoteLaneGit,
)
from omnimarket.nodes.node_remote_lane_effect.protocols.protocol_remote_lane_effect import (
    RemoteLaneCommandOutcome,
    RemoteLanePortError,
)


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


@dataclass
class History:
    tree: Path
    remote: Path
    scratch: Path
    base: str
    commit: str
    peer: str

    def request(self, **updates: object) -> ModelRemoteLanePublishRequest:
        data: dict[str, object] = {
            "worktree": str(self.tree),
            "replay_root": str(self.scratch),
            "branch": "fix",
            "base_sha": self.base,
            "commit_sha": self.commit,
            "observed_head": self.base,
            "lane": "recorded-lane",
        }
        data.update(updates)
        return ModelRemoteLanePublishRequest.model_validate(data)

    def advance(self, sha: str) -> None:
        git("-C", str(self.tree), "push", "origin", f"{sha}:refs/heads/fix")

    def head(self, branch: str = "fix") -> str:
        return git("--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}")


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> History:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.test")
    tree, remote, scratch = (
        tmp_path / "tree",
        tmp_path / "remote.git",
        tmp_path / "replay",
    )
    scratch.mkdir()
    git("init", "-q", "-b", "lane", str(tree))
    git("init", "-q", "--bare", str(remote))
    git("-C", str(tree), "remote", "add", "origin", str(remote))
    (tree / "shared.txt").write_text("base\n")
    git("-C", str(tree), "add", "shared.txt")
    git("-C", str(tree), "commit", "-qm", "base")
    base = git("-C", str(tree), "rev-parse", "HEAD")
    git("-C", str(tree), "push", "origin", "HEAD:refs/heads/fix")
    (tree / "lane.txt").write_text("finished lane work\n")
    git("-C", str(tree), "add", "lane.txt")
    git("-C", str(tree), "commit", "-qm", "finished lane")
    commit = git("-C", str(tree), "rev-parse", "HEAD")
    git("-C", str(tree), "switch", "--detach", base)
    (tree / "peer.txt").write_text("someone else's work\n")
    git("-C", str(tree), "add", "peer.txt")
    git("-C", str(tree), "commit", "-qm", "peer")
    peer = git("-C", str(tree), "rev-parse", "HEAD")
    git("-C", str(tree), "switch", "lane")
    return History(tree, remote, scratch, base, commit, peer)


def handler(port: LocalRemoteLaneGit | None = None):
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_remote_lane_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        item
        for item in contract["handler_routing"]["handlers"]
        if item["operation"] == "publish_remote_lane_commit"
    )
    cls = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    module, _, name = entry["input_model"].rpartition(".")
    assert (
        getattr(importlib.import_module(module), name) is ModelRemoteLanePublishRequest
    )
    return cls(port)


def test_unmoved_head_publishes_the_exact_lane_commit(history: History) -> None:
    result = handler().handle(history.request())
    assert result.outcome == "published"
    assert result.published_sha == history.commit == history.head()
    assert result.recovery_pushed
    assert history.head(result.recovery_branch) == history.commit


_RECORDED_CASES = json.loads(
    (
        Path(__file__).parents[2]
        / "fixtures/remote_lane_publish/recorded_outcomes.json"
    ).read_text()
)["cases"]


@pytest.mark.parametrize(
    "recorded_shape", [case["failure_shape"] for case in _RECORDED_CASES]
)
def test_recorded_moved_heads_replay_without_losing_either_change(
    history: History, recorded_shape: str
) -> None:
    class MovingHead(LocalRemoteLaneGit):
        moved = False

        def run(
            self, argv: Sequence[str], timeout_s: float
        ) -> RemoteLaneCommandOutcome:
            target_push = "push" in argv and any(
                str(arg).endswith(":refs/heads/fix") for arg in argv
            )
            if target_push and not self.moved and recorded_shape != "pr-head-moved":
                self.moved = True
                history.advance(history.peer)
                if recorded_shape == "push-failed":
                    return RemoteLaneCommandOutcome(
                        1, "", "error: failed to push some refs"
                    )
            return super().run(argv, timeout_s)

    if recorded_shape == "pr-head-moved":
        history.advance(history.peer)
    result = handler(MovingHead()).handle(history.request())
    assert result.outcome == "published"
    head = history.head()
    assert result.published_sha == head
    assert (
        git("-C", str(history.tree), "show", f"{head}:lane.txt") == "finished lane work"
    )
    assert (
        git("-C", str(history.tree), "show", f"{head}:peer.txt")
        == "someone else's work"
    )
    assert git("-C", str(history.tree), "rev-parse", f"{head}^") == history.peer
    assert history.head(result.recovery_branch) == history.commit
    assert git("-C", str(history.tree), "rev-parse", "HEAD") == history.commit
    assert git("-C", str(history.tree), "status", "--porcelain") == ""
    assert not list(history.scratch.iterdir())


def test_conflict_names_a_pushed_recovery_branch_and_leaves_the_pr_untouched(
    history: History,
) -> None:
    (history.tree / "shared.txt").write_text("lane edit\n")
    git("-C", str(history.tree), "add", "shared.txt")
    git("-C", str(history.tree), "commit", "-qm", "lane conflict")
    history.commit = git("-C", str(history.tree), "rev-parse", "HEAD")
    git("-C", str(history.tree), "switch", "--detach", history.peer)
    (history.tree / "shared.txt").write_text("peer edit\n")
    git("-C", str(history.tree), "add", "shared.txt")
    git("-C", str(history.tree), "commit", "-qm", "peer conflict")
    history.peer = git("-C", str(history.tree), "rev-parse", "HEAD")
    git("-C", str(history.tree), "switch", "lane")
    history.advance(history.peer)
    result = handler().handle(history.request())
    assert result.outcome == "conflict"
    assert result.recovery_pushed
    assert result.recovery_branch in result.message
    assert history.head(result.recovery_branch) == history.commit
    assert history.head() == history.peer
    assert result.published_sha is None
    assert git("-C", str(history.tree), "status", "--porcelain") == ""
    assert not list(history.scratch.iterdir())


def test_a_second_call_does_not_duplicate_the_replayed_change(history: History) -> None:
    history.advance(history.peer)
    request = history.request()
    first = handler().handle(request)
    second = handler().handle(request)
    assert first.outcome == "published"
    assert second.outcome == "already-published"
    assert second.published_sha == first.published_sha == history.head()


def test_push_refusal_preserves_work_on_remote_recovery_branch(
    history: History,
) -> None:
    class RefuseTarget(LocalRemoteLaneGit):
        def run(
            self, argv: Sequence[str], timeout_s: float
        ) -> RemoteLaneCommandOutcome:
            if "push" in argv and any(
                str(arg).endswith(":refs/heads/fix") for arg in argv
            ):
                return RemoteLaneCommandOutcome(1, "", "remote: policy refused")
            return super().run(argv, timeout_s)

    result = handler(RefuseTarget()).handle(history.request())
    assert result.outcome == "push-failed"
    assert result.recovery_pushed
    assert result.recovery_branch in result.message
    assert history.head(result.recovery_branch) == history.commit
    assert history.head() == history.base


def test_recovery_push_failure_requires_the_original_worktree_to_be_kept(
    history: History,
) -> None:
    class Offline(LocalRemoteLaneGit):
        def run(
            self, argv: Sequence[str], timeout_s: float
        ) -> RemoteLaneCommandOutcome:
            if "push" in argv:
                raise RemoteLanePortError("connection timed out")
            return super().run(argv, timeout_s)

    result = handler(Offline()).handle(history.request())
    assert result.outcome == "recovery-push-failed"
    assert not result.recovery_pushed
    assert result.keep_worktree
    assert history.head() == history.base
    assert git("-C", str(history.tree), "rev-parse", "HEAD") == history.commit


def test_target_push_is_pinned_to_an_ancestor_of_the_candidate(
    history: History,
) -> None:
    class Audit(LocalRemoteLaneGit):
        pushes: list[list[str]] = []

        def run(
            self, argv: Sequence[str], timeout_s: float
        ) -> RemoteLaneCommandOutcome:
            if "push" in argv:
                self.pushes.append(list(argv))
                assert "--force" not in argv
                assert "-f" not in argv
                if any(str(arg).endswith(":refs/heads/fix") for arg in argv):
                    pin = next(
                        arg for arg in argv if arg.startswith("--force-with-lease=")
                    )
                    sha = pin.rsplit(":", 1)[1]
                    candidate = argv[-1].split(":", 1)[0]
                    git(
                        "-C",
                        str(history.tree),
                        "merge-base",
                        "--is-ancestor",
                        sha,
                        candidate,
                    )
            return super().run(argv, timeout_s)

    port = Audit()
    history.advance(history.peer)
    result = handler(port).handle(history.request())
    assert result.outcome == "published"
    assert any(
        f"--force-with-lease=refs/heads/fix:{history.peer}" in call
        for call in port.pushes
    )


def test_replay_runs_the_repository_commit_hook(history: History) -> None:
    marker = history.tree.parent / "hook-ran"
    hook = history.tree / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    history.advance(history.peer)
    result = handler().handle(history.request())
    assert result.outcome == "replay-failed"
    assert marker.read_text() == "ran\n"
    assert result.recovery_pushed
    assert history.head(result.recovery_branch) == history.commit
    assert history.head() == history.peer


def test_overlapping_phase_commits_replay_as_one_delta_and_repeat_safely(
    history: History,
) -> None:
    for text in ("phase one\n", "phase two\n"):
        (history.tree / "shared.txt").write_text(text)
        git("-C", str(history.tree), "add", "shared.txt")
        git("-C", str(history.tree), "commit", "-qm", text.strip())
    history.commit = git("-C", str(history.tree), "rev-parse", "HEAD")
    history.advance(history.peer)
    first = handler().handle(history.request())
    second = handler().handle(history.request())
    assert first.outcome == "published"
    assert second.outcome == "already-published"
    assert second.published_sha == first.published_sha == history.head()
    assert (
        git("-C", str(history.tree), "show", f"{history.head()}:shared.txt")
        == "phase two"
    )
    assert history.head(first.recovery_branch) == history.commit


def test_remote_only_head_is_fetched_by_its_exact_sha(history: History) -> None:
    peer_tree = history.tree.parent / "other"
    git("clone", "-q", "--branch", "fix", str(history.remote), str(peer_tree))
    (peer_tree / "remote-only.txt").write_text("remote change\n")
    git("-C", str(peer_tree), "add", "remote-only.txt")
    git("-C", str(peer_tree), "commit", "-qm", "new remote head")
    git("-C", str(peer_tree), "push", "origin", "HEAD:refs/heads/fix")
    new_head = history.head()
    with pytest.raises(subprocess.CalledProcessError):
        git("-C", str(history.tree), "cat-file", "-e", new_head)
    result = handler().handle(history.request())
    assert result.outcome == "published"
    assert git("-C", str(history.tree), "rev-parse", f"{history.head()}^") == new_head
    assert (
        git("-C", str(history.tree), "show", f"{history.head()}:remote-only.txt")
        == "remote change"
    )


def test_recovery_ref_collision_cannot_overwrite_another_commit(
    history: History,
) -> None:
    recovery = f"recovery/recorded-lane/{history.commit}"
    git(
        "-C",
        str(history.tree),
        "push",
        "origin",
        f"{history.peer}:refs/heads/{recovery}",
    )
    result = handler().handle(history.request())
    assert result.outcome == "recovery-push-failed"
    assert not result.recovery_pushed
    assert result.keep_worktree
    assert history.head(recovery) == history.peer
    assert history.head() == history.base


def test_a_continually_moving_target_is_bounded_and_preserved(history: History) -> None:
    git("-C", str(history.tree), "switch", "--detach", history.peer)
    peers = [history.peer]
    for name in ("second", "third"):
        (history.tree / f"{name}.txt").write_text(name)
        git("-C", str(history.tree), "add", f"{name}.txt")
        git("-C", str(history.tree), "commit", "-qm", name)
        peers.append(git("-C", str(history.tree), "rev-parse", "HEAD"))
    git("-C", str(history.tree), "switch", "lane")

    class AlwaysMoving(LocalRemoteLaneGit):
        pushes = 0

        def run(
            self, argv: Sequence[str], timeout_s: float
        ) -> RemoteLaneCommandOutcome:
            if "push" in argv and any(arg.endswith(":refs/heads/fix") for arg in argv):
                history.advance(peers[self.pushes])
                self.pushes += 1
            return super().run(argv, timeout_s)

    port = AlwaysMoving()
    result = handler(port).handle(history.request())
    assert result.outcome == "head-keeps-moving"
    assert result.attempts == port.pushes == 3
    assert history.head() == peers[-1]
    assert history.head(result.recovery_branch) == history.commit
    assert result.recovery_branch in result.message
    assert not list(history.scratch.iterdir())


def test_uncommitted_work_in_the_original_tree_is_preserved(history: History) -> None:
    (history.tree / "shared.txt").write_text("unstaged work\n")
    (history.tree / "untracked.txt").write_text("untracked work\n")
    (history.tree / "staged.txt").write_text("staged work\n")
    git("-C", str(history.tree), "add", "staged.txt")
    before = git("-C", str(history.tree), "status", "--porcelain")
    history.advance(history.peer)
    result = handler().handle(history.request())
    assert result.outcome == "published"
    assert result.keep_worktree
    assert git("-C", str(history.tree), "status", "--porcelain") == before
    assert (history.tree / "shared.txt").read_text() == "unstaged work\n"
    assert (history.tree / "untracked.txt").read_text() == "untracked work\n"
    assert (history.tree / "staged.txt").read_text() == "staged work\n"


def test_invalid_base_keeps_work_locally_and_never_pushes(history: History) -> None:
    result = handler().handle(history.request(base_sha=history.peer))
    assert result.outcome == "invalid-history"
    assert not result.recovery_pushed
    assert result.keep_worktree
    assert history.head() == history.base
    assert git("-C", str(history.tree), "rev-parse", "HEAD") == history.commit


def test_deleted_target_is_not_recreated_from_a_stale_snapshot(
    history: History,
) -> None:
    git("-C", str(history.tree), "push", "origin", ":refs/heads/fix")
    result = handler().handle(history.request())
    assert result.outcome == "target-unreadable"
    assert result.recovery_pushed
    assert result.recovery_branch in result.message
    assert history.head(result.recovery_branch) == history.commit
    assert git("--git-dir", str(history.remote), "for-each-ref", "refs/heads/fix") == ""


def test_cleanup_failure_names_the_replay_tree_to_keep(history: History) -> None:
    class KeepReplay(LocalRemoteLaneGit):
        def run(
            self, argv: Sequence[str], timeout_s: float
        ) -> RemoteLaneCommandOutcome:
            if "worktree" in argv and "remove" in argv:
                return RemoteLaneCommandOutcome(1, "", "worktree busy")
            return super().run(argv, timeout_s)

    history.advance(history.peer)
    result = handler(KeepReplay()).handle(history.request())
    assert result.outcome == "published"
    assert result.keep_worktree
    assert len(result.cleanup_errors) == 1
    replay = next(history.scratch.iterdir())
    assert str(replay) in result.cleanup_errors[0]
    assert history.head(result.recovery_branch) == history.commit
    git("-C", str(history.tree), "worktree", "remove", str(replay))


@pytest.mark.asyncio
async def test_publish_operation_runs_through_the_canonical_runtime_bus(
    history: History,
) -> None:
    from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
    from omnimarket.adapters.codex.runtime_client import (
        ModelDispatchBusCommand,
        default_command_topic,
        default_response_topic,
    )

    history.advance(history.peer)
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(),
        state_root=history.tree.parent / "runtime",
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="node_remote_lane_effect.publish_remote_lane_commit",
            requester="publish-recovery-test",
            correlation_id=uuid4(),
            payload=history.request().model_dump(mode="json"),
            response_topic=default_response_topic(),
            timeout_seconds=60,
        )
    )
    assert terminal.status == "completed", terminal.error_message
    assert evidence.runtime_observation.status == "OBSERVED"
    assert evidence.adapter_dispatch_binding.node_contract.status == "RESOLVED"
    result = ModelRemoteLanePublishResult.model_validate(terminal.payload)
    assert result.outcome == "published"
    assert history.head() == result.published_sha
    assert history.head(result.recovery_branch) == history.commit
