# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected effects and real temporary git repositories."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import BaseModel

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileCommand,
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
    GitWorktreeFactsProbe,
    is_junk,
    live_claims,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_remover import (
    GitWorktreeRemover,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcile,
)
from tests.test_worktree_reconcile_compute import facts

pytestmark = pytest.mark.unit
NOW = datetime(2026, 1, 10, tzinfo=UTC)


class Fakes:
    def __init__(self, items: tuple[ModelWorktreeFacts, ...]) -> None:
        self.items = items
        self.fresh: ModelWorktreeFacts | None = None
        self.removed: list[str] = []
        self.pinned: list[str] = []
        self.events: list[tuple[str, BaseModel]] = []
        self.pin_ok = True
        self.decider_fails = False
        self.verdicts: tuple[ModelWorktreeDecisionRecord, ...] = ()
        self.asked: tuple[ModelWorktreeFacts, ...] = ()

    def now(self) -> datetime:
        return NOW

    def discover(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> tuple[ModelWorktreeFacts, ...]:
        return self.items

    def revalidate(self, fact: ModelWorktreeFacts, now: datetime) -> ModelWorktreeFacts:
        return self.fresh or fact

    def pin(self, argv: list[str], fact: ModelWorktreeFacts) -> bool:
        self.pinned.append(fact.path)
        return self.pin_ok

    def remove(self, fact: ModelWorktreeFacts, now: datetime) -> None:
        self.removed.append(fact.path)

    def decide(
        self, argv: list[str], items: tuple[ModelWorktreeFacts, ...]
    ) -> tuple[ModelWorktreeDecisionRecord, ...]:
        self.asked = items
        if self.decider_fails:
            raise ValueError("unavailable")
        return self.verdicts

    def publish(self, topic: str, event: BaseModel) -> None:
        self.events.append((topic, event))

    def handler(self) -> HandlerWorktreeReconcile:
        decided, completed = publish_topics()
        return HandlerWorktreeReconcile(
            probe=self,
            decider=self,
            pinner=self,
            remover=self,
            clock=self,
            publisher=self,
            decided_topic=decided,
            completed_topic=completed,
        )


def command(**updates: object) -> ModelWorktreeReconcileCommand:
    return ModelWorktreeReconcileCommand(
        host="host",
        roots=("trees",),
        policy=ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",)),
        execute=True,
        pin_command=["pin"],
        decider_command=["decide"],
        correlation_id=uuid4(),
    ).model_copy(update=updates)


def test_repositories_act_concurrently_but_publish_in_decision_order() -> None:
    items = tuple(
        facts(
            path=f"trees/task-{n}/repo",
            repo_slug=f"owner/repo-{n % 3}",
            head_on_remote=True,
        )
        for n in range(9)
    )
    fake = Fakes(items)
    result = fake.handler().handle(command())
    decided_topic = publish_topics()[0]
    published = [
        event.facts.path  # type: ignore[attr-defined]
        for topic, event in fake.events
        if topic == decided_topic
    ]
    assert published == [e.facts.path for e in result.decided_events]
    assert sorted(published) == sorted(f.path for f in items)
    assert sorted(fake.removed) == sorted(f.path for f in items)
    assert result.completed_event.removed == 9


def test_one_row_raising_leaves_a_failed_event_and_the_rest_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = tuple(
        facts(path=f"trees/t{n}/repo", repo_slug=f"owner/r{n}", head_on_remote=True)
        for n in range(3)
    )
    fake = Fakes(items)
    handler = fake.handler()
    real = handler._execute

    def flaky(command, fact, row):  # type: ignore[no-untyped-def]
        if fact.path == "trees/t1/repo":
            raise RuntimeError("surprise")
        return real(command, fact, row)

    monkeypatch.setattr(handler, "_execute", flaky)
    result = handler.handle(command())
    outcomes = {e.facts.path: e.outcome for e in result.decided_events}
    assert outcomes == {
        "trees/t0/repo": "removed",
        "trees/t1/repo": "failed",
        "trees/t2/repo": "removed",
    }
    assert "trees/t1/repo" not in fake.removed


def test_revalidation_does_not_measure_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, _ = git_fixture(tmp_path)
    path = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "sized", str(path))
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    probe = GitWorktreeFactsProbe()
    found = probe.discover(command(roots=(str(root),)), NOW)[0]
    assert found.size_bytes
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv: list[str], *args: object, **kwargs: object) -> object:
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.subprocess.run",
        spy,
    )
    fresh = probe.revalidate(found, NOW)
    assert fresh.facts_complete
    assert fresh.size_bytes == found.size_bytes
    assert not [argv for argv in calls if argv and argv[0] == "du"]


def test_dry_run_and_publish() -> None:
    fake = Fakes((facts(head_on_remote=True),))
    result = fake.handler().handle(command(execute=False))
    assert not fake.removed
    assert not fake.pinned
    assert result.decided_events[0].outcome == "dry_run"
    assert result.completed_event.freed_bytes == 0
    assert [topic for topic, _ in fake.events] == list(publish_topics())
    assert fake.events[-1][1] == result.completed_event


def test_pin_failure_keeps() -> None:
    fake = Fakes((facts(commits_not_on_remote=2),))
    fake.pin_ok = False
    result = fake.handler().handle(command())
    assert not fake.removed
    assert result.decided_events[0].decision.reasons == ("pin_failed",)


@pytest.mark.parametrize(
    "change",
    [
        {"live_process": True},
        {"dirty_tracked_count": 1},
        {"head_sha": "changed"},
        {"probe_errors": ("timeout",)},
        {"live_claim": True},
    ],
)
def test_revalidation_refuses_changes(change: dict[str, object]) -> None:
    fact = facts(head_on_remote=True)
    fake = Fakes((fact,))
    fake.fresh = fact.model_copy(update=change)
    result = fake.handler().handle(command())
    assert not fake.removed
    assert result.decided_events[0].decision.reasons == ("revalidation_refused",)


@pytest.mark.parametrize("preserved", [False, True])
def test_clone_requires_all_branches(preserved: bool) -> None:
    fake = Fakes(
        (
            facts(
                kind=Kind.STANDALONE_CLONE,
                has_remote=True,
                head_on_remote=True,
                content_merged=True,
                local_branches_all_on_remote=preserved,
            ),
        )
    )
    fake.handler().handle(command())
    assert bool(fake.removed) == preserved


def test_decider_failure_and_dirty_advice() -> None:
    fact = facts(dirty_tracked_count=1)
    fake = Fakes((fact, facts(path="safe", head_on_remote=True)))
    fake.decider_fails = True
    result = fake.handler().handle(command())
    assert result.decided_events[0].decision.decision == Decision.NEEDS_HUMAN
    assert fake.asked == (fact,)
    fake.decider_fails = False
    fake.verdicts = (
        ModelWorktreeDecisionRecord(
            path=fact.path, decision=Decision.PIN_AND_REMOVE, reasons=()
        ),
    )
    result = fake.handler().handle(command())
    assert result.decided_events[0].decision.decision == Decision.NEEDS_HUMAN
    assert fact.path not in fake.removed


@pytest.mark.parametrize(
    ("name", "junk"),
    [
        (".venv/cache", True),
        ("node_modules/pkg", True),
        ("build/x.egg-info/meta", True),
        (".env", False),
        (".venv/.env.local", False),
        ("dist/key.pem", False),
        ("src/file.py", False),
    ],
)
def test_junk_secrets(name: str, junk: bool) -> None:
    assert is_junk(name) == junk


def test_ledger_heartbeats_and_terminals() -> None:
    text = "\n".join(
        [
            "2026-01-01T00:00:00Z | CLAIM | lane=a | trees/task/repo",
            "2026-01-09T23:00:00Z | STATUS | lane=a | busy",
            "2026-01-09T23:00:00Z | CLAIM | lane=b | gone",
            "2026-01-09T23:01:00Z | RELEASE | lane=b | done",
            "2026-01-01T00:00:00Z | CLAIM | lane=c | stale",
            "2026-01-09T23:00:00Z | RULING | unrelated operator note",
        ]
    )
    assert live_claims(text, NOW, 6) == (text.splitlines()[0],)


def run_git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def git_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    registry = tmp_path / "registry"
    registry.mkdir()
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    clone = registry / "canonical"
    run_git(tmp_path, "clone", str(remote), str(clone))
    run_git(clone, "config", "user.name", "Test")
    run_git(clone, "config", "user.email", "test@example.invalid")
    (clone / "tracked").write_text("base\n")
    run_git(clone, "add", "tracked")
    run_git(clone, "commit", "-m", "base")
    run_git(clone, "push", "-u", "origin", "main")
    run_git(clone, "remote", "set-head", "origin", "-a")
    root = registry / "trees"
    root.mkdir()
    return clone, root, remote


def test_real_git_merged_removed_dirty_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, _ = git_fixture(tmp_path)
    clean, dirty = root / "clean-task" / "repo", root / "dirty-task" / "repo"
    run_git(clone, "worktree", "add", "-b", "clean", str(clean))
    run_git(clone, "worktree", "add", "-b", "dirty", str(dirty))
    (clean / "landed").write_text("landed\n")
    run_git(clean, "add", "landed")
    run_git(clean, "commit", "-m", "landed")
    run_git(clone, "merge", "clean")
    run_git(clone, "push", "origin", "main")
    (dirty / "tracked").write_text("unsaved\n")
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    fake = Fakes(())
    # Use a deterministic future clock so fresh fixture writes are old.
    monkeypatch.setattr(fake, "now", lambda: datetime(2100, 1, 1, tzinfo=UTC))
    decided, completed = publish_topics()
    handler = HandlerWorktreeReconcile(
        probe=GitWorktreeFactsProbe(),
        decider=fake,
        pinner=fake,
        remover=GitWorktreeRemover(),
        clock=fake,
        publisher=fake,
        decided_topic=decided,
        completed_topic=completed,
    )
    result = handler.handle(command(roots=(str(root),), decider_command=None))
    assert not clean.exists()
    assert not clean.parent.exists()
    assert dirty.exists()
    assert result.completed_event.removed == 1
    assert result.completed_event.needs_human == 1
    assert result.completed_event.failures == 0


def test_probe_ignored_secrets_and_remote_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, _ = git_fixture(tmp_path)
    path = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(path))
    (path / ".gitignore").write_text(".env\n.venv/\n")
    run_git(path, "add", ".gitignore")
    run_git(path, "commit", "-m", "ignore")
    (path / ".env").write_text("DO_NOT_TRANSMIT_FILE_CONTENT")
    (path / ".venv").mkdir()
    (path / ".venv" / "cache").write_text("junk")
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    items = GitWorktreeFactsProbe().discover(command(roots=(str(root),)), NOW)
    assert len(items) == 1
    assert items[0].facts_complete
    assert items[0].untracked_nonjunk_count == 1
    assert items[0].commits_not_on_remote == 1
    assert not items[0].head_on_remote
    assert "DO_NOT_TRANSMIT_FILE_CONTENT" not in items[0].model_dump_json()


def test_pin_success_and_summary() -> None:
    fake = Fakes((facts(commits_not_on_remote=1, size_bytes=123),))
    result = fake.handler().handle(command())
    assert fake.removed == [fake.items[0].path]
    assert result.completed_event.pinned_and_removed == 1
    assert result.completed_event.freed_bytes == 123


def test_clone_model_pin_requires_remote_revalidation() -> None:
    fact = facts(kind=Kind.STANDALONE_CLONE, has_remote=False, head_on_remote=False)
    fake = Fakes((fact,))
    fake.verdicts = (
        ModelWorktreeDecisionRecord(
            path=fact.path, decision=Decision.PIN_AND_REMOVE, reasons=()
        ),
    )
    result = fake.handler().handle(command())
    assert not fake.removed
    assert result.decided_events[0].decision.reasons == (
        "model_verdict_refused_by_rail",
    )


def test_git_timeout_is_a_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, _ = git_fixture(tmp_path)
    path = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(path))
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    real_git = adapter_facts.git

    def timeout_fetch(
        path: str, *args: str, ok: tuple[int, ...] = (0,), timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            raise subprocess.TimeoutExpired("git", timeout)
        return real_git(path, *args, ok=ok, timeout=timeout)

    monkeypatch.setattr(adapter_facts, "git", timeout_fetch)
    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: ())
    items = GitWorktreeFactsProbe().discover(command(roots=(str(root),)), NOW)
    assert items[0].probe_errors == ("probe_failed:ValueError",)
    fake = Fakes(items)
    result = fake.handler().handle(command())
    assert not fake.removed
    assert result.decided_events[0].decision.reasons == ("facts_unreadable",)


def test_squash_merge_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clone, root, _ = git_fixture(tmp_path)
    path = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(path))
    (path / "change").write_text("landed content\n")
    run_git(path, "add", "change")
    run_git(path, "commit", "-m", "feature commit")
    run_git(clone, "merge", "--squash", "feature")
    run_git(clone, "commit", "-m", "squashed commit")
    run_git(clone, "push", "origin", "main")
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    fact = GitWorktreeFactsProbe().discover(command(roots=(str(root),)), NOW)[0]
    assert fact.facts_complete
    assert fact.content_merged
    assert not fact.head_on_remote
    assert fact.commits_not_on_remote == 1


def test_standalone_depth_canonical_exclusion_and_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, remote = git_fixture(tmp_path)
    extras = tmp_path / "extras"
    extras.mkdir()
    standalone = extras / "group" / "clone"
    run_git(tmp_path, "clone", str(remote), str(standalone))
    deep = extras / "too" / "deep" / "clone"
    run_git(tmp_path, "clone", str(remote), str(deep))
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    probe = GitWorktreeFactsProbe()
    items = probe.discover(
        command(roots=(str(root),), extra_clone_roots=(str(extras), str(clone.parent))),
        datetime(2100, 1, 1, tzinfo=UTC),
    )
    assert [f.path for f in items] == [str(standalone)]
    assert items[0].local_branches_all_on_remote
    GitWorktreeRemover().remove(items[0], NOW)
    assert not standalone.exists()
    assert deep.exists()
    assert clone.exists()
    assert (extras / ".onex_state" / "worktree-reconcile-trash" / "2026-01-10").is_dir()


def test_pin_is_proven_on_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import (
        adapter_commands,
    )

    clone, root, _ = git_fixture(tmp_path)
    tree = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(tree))
    sha = run_git(tree, "rev-parse", "HEAD")
    fact = facts(path=str(tree), root=str(root), head_sha=sha, host="lab")
    branch = adapter_commands.backup_branch(fact)
    assert branch == "backup/worktree-reconcile/lab/task/repo"
    real_run = subprocess.run
    seen: list[list[str]] = []

    def pin_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "pin-test":
            seen.append(argv)
            real_run(
                [
                    "git",
                    "-C",
                    argv[1],
                    "push",
                    "-q",
                    "origin",
                    f"HEAD:refs/heads/{argv[2]}",
                ],
                check=True,
                env=scrub_git_location_env(os.environ),
            )
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[0] == "pin-lies":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(adapter_commands.subprocess, "run", pin_run)
    assert adapter_commands.CommandWorktreePinner().pin(
        ["pin-test", "{path}", "{branch}"], fact
    )
    assert seen[0][1:] == [str(tree), branch]
    # A command that claims success without pushing is caught by the readback.
    other = fact.model_copy(update={"path": str(tree), "host": "other"})
    assert not adapter_commands.CommandWorktreePinner().pin(["pin-lies"], other)
    # A readback at a different sha is not a pin of this tree.
    assert not adapter_commands.CommandWorktreePinner().pin(
        ["pin-lies"], fact.model_copy(update={"head_sha": "0" * 40})
    )


def test_discovery_failure_still_emits_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fakes(())

    def fail(
        command: ModelWorktreeReconcileCommand, now: datetime
    ) -> tuple[ModelWorktreeFacts, ...]:
        raise OSError("unreadable registry")

    monkeypatch.setattr(fake, "discover", fail)
    result = fake.handler().handle(command())
    assert result.completed_event.failures == 1
    assert result.completed_event.scanned == 0
    assert result.errors == ("discovery_failed:OSError:unreadable registry",)
    assert len(fake.events) == 1


def test_initial_process_snapshot_once_and_ledger_change_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    root = tmp_path / "registry" / "trees"
    one, two = root / "task-one", root / "task-two"
    one.mkdir(parents=True)
    two.mkdir()
    ledger = tmp_path / "ledger"
    ledger.write_text("2026-01-09T23:00:00Z | CLAIM | lane=a | task-one\n")
    calls = []

    def snapshot() -> tuple[Path, ...]:
        calls.append(True)
        return (two,)

    monkeypatch.setattr(adapter_facts, "process_cwds", snapshot)
    probe = GitWorktreeFactsProbe()
    items = probe.discover(command(roots=(str(root),), ledger_path=str(ledger)), NOW)
    assert len(calls) == 1
    assert items[0].live_claim
    assert items[1].live_process
    ledger.write_text(
        ledger.read_text() + "2026-01-09T23:30:00Z | STATUS | lane=b | unrelated\n"
    )
    fresh = probe.revalidate(items[1], NOW)
    assert "ledger_changed" not in fresh.probe_errors
    assert not fresh.live_claim
    # A claim that appears between discovery and removal is seen at removal time.
    ledger.write_text(
        ledger.read_text() + "2026-01-09T23:40:00Z | CLAIM | lane=c | task-two\n"
    )
    assert probe.revalidate(items[1], NOW).live_claim


def test_subprocess_decider_json_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import (
        adapter_commands,
    )

    fact = facts(
        file_names=(".env",), commit_subjects=("subject",), diff_stat=" file | 1 +"
    )
    verdict = ModelWorktreeDecisionRecord(
        path=fact.path,
        decision=Decision.KEEP,
        reasons=("reviewed",),
        model_rationale="retain",
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["configured-decider", "argument"]
        assert kwargs["timeout"] == 300
        assert json.loads(str(kwargs["input"])) == [fact.model_dump(mode="json")]
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(
            argv, 0, "[" + verdict.model_dump_json() + "]", ""
        )

    monkeypatch.setattr(adapter_commands.subprocess, "run", run)
    result = adapter_commands.CommandWorktreeDecider().decide(
        ["configured-decider", "argument"], (fact,)
    )
    assert result == (verdict,)


def test_cli_input_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from omnimarket.nodes.node_worktree_reconcile_effect.__main__ import main

    root = tmp_path / "registry" / "trees"
    root.mkdir(parents=True)
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    command_file = tmp_path / "command.json"
    command_file.write_text(
        command(roots=(str(root),), execute=False).model_dump_json()
    )
    assert main(["--input", str(command_file)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["completed_event"]["scanned"] == 0
    assert json.loads(captured.err)["topic"] == publish_topics()[1]


def test_stash_reflog_is_probed_without_stash_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, _ = git_fixture(tmp_path)
    path = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(path))
    tree = run_git(clone, "rev-parse", "HEAD^{tree}")
    stash_commit = run_git(
        clone, "commit-tree", tree, "-p", "HEAD", "-m", "unpublished snapshot"
    )
    run_git(clone, "update-ref", "--create-reflog", "refs/stash", stash_commit)
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    fact = GitWorktreeFactsProbe().discover(command(roots=(str(root),)), NOW)[0]
    assert fact.facts_complete
    # The stash lives in the clone's common git dir and survives removing this
    # worktree, so it is not this worktree's unsaved work.
    assert fact.unpushed_stash_count == 0
    assert not fact.dirty


def test_a_standalone_clone_counts_its_own_unpushed_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: removing a clone does delete its stashes."""
    _, root, remote = git_fixture(tmp_path)
    extras = tmp_path / "home"
    standalone = extras / "lane" / "clone"
    run_git(tmp_path, "clone", str(remote), str(standalone))
    tree = run_git(standalone, "rev-parse", "HEAD^{tree}")
    stash_commit = run_git(
        standalone,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit-tree",
        tree,
        "-p",
        "HEAD",
        "-m",
        "unpublished snapshot",
    )
    run_git(standalone, "update-ref", "--create-reflog", "refs/stash", stash_commit)
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    items = GitWorktreeFactsProbe().discover(
        command(roots=(str(root),), extra_clone_roots=(str(extras),)), NOW
    )
    assert [f.path for f in items] == [str(standalone)]
    assert items[0].unpushed_stash_count == 1
    assert items[0].dirty


def test_ledger_churn_and_legacy_rows_never_block_a_run() -> None:
    from datetime import UTC, datetime

    from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
        _claimed,
        live_claims,
    )

    now = datetime(2026, 1, 2, tzinfo=UTC)
    ledger = "\n".join(
        [
            "| legacy | table | row |",
            "2026-01-02T00:00:00 | CLAIM | no timezone",
            "not-a-date | CLAIM | lane=x",
            "2026-01-01T23:00:00Z | CLAIM | lane=live | ticket=TASK-1 | work",
            "2026-01-01T01:00:00Z | CLAIM | lane=dead | ticket=TASK-2 | work",
            "2026-01-01T23:30:00Z | CLAIM | lane=done | ticket=TASK-3 | work",
            "2026-01-01T23:40:00Z | TERMINAL | lane=done | friction=none",
        ]
    )
    claims = live_claims(ledger, now, quiet_hours=6.0)
    assert len(claims) == 1
    assert "lane=live" in claims[0]
    root = Path("/trees")
    assert _claimed(root / "TASK-1" / "repo", root, claims)
    # A ticket id that is a prefix of the claimed one is not claimed.
    assert not _claimed(root / "TASK" / "repo", root, claims)
    assert not _claimed(root / "TASK-2" / "repo", root, claims)


def test_an_error_with_no_tree_to_carry_it_is_never_a_clean_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    def no_snapshot() -> tuple[Path, ...]:
        raise RuntimeError("process snapshot incomplete")

    monkeypatch.setattr(adapter_facts, "process_cwds", no_snapshot)
    root = tmp_path / "registry" / "trees"
    root.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="process_snapshot_failed"):
        GitWorktreeFactsProbe().discover(command(roots=(str(root),)), NOW)


def test_an_unreadable_directory_under_a_clone_root_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: (tmp_path,))
    home = tmp_path / "home"
    locked = home / "private"
    locked.mkdir(parents=True)
    clone = home / "lane" / "repo"
    run_git(tmp_path, "init", "-q", str(clone))
    locked.chmod(0)
    try:
        registry = tmp_path / "registry" / "trees"
        registry.mkdir(parents=True)
        items = GitWorktreeFactsProbe().discover(
            command(roots=(str(registry),), extra_clone_roots=(str(home),)), NOW
        )
    finally:
        locked.chmod(0o700)
    assert [Path(item.path).name for item in items] == ["repo"]
    assert "clone_discovery_failed" not in items[0].probe_errors


def test_ignored_caches_are_not_work_but_an_ignored_secret_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: (tmp_path,))
    clone, root, _ = git_fixture(tmp_path)
    tree = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(tree))
    (tree / ".gitignore").write_text(".hypothesis/\n.coverage\n.env\n")
    run_git(tree, "add", ".gitignore")
    run_git(tree, "commit", "-m", "ignore")
    (tree / ".hypothesis").mkdir()
    (tree / ".hypothesis" / "cache").write_text("x")
    (tree / ".coverage").write_text("x")
    probe = GitWorktreeFactsProbe()
    items = probe.discover(command(roots=(str(root),)), NOW)
    assert items[0].untracked_nonjunk_count == 0
    (tree / ".env").write_text("TOKEN=x")
    assert probe.revalidate(items[0], NOW).untracked_nonjunk_count == 1


def test_a_remote_of_another_repository_is_not_a_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: (tmp_path,))
    clone, root, _ = git_fixture(tmp_path)
    other = tmp_path / "other.git"
    run_git(tmp_path, "init", "--bare", "--initial-branch=main", str(other))
    seed = tmp_path / "seed"
    run_git(tmp_path, "clone", str(other), str(seed))
    run_git(seed, "config", "user.name", "Test")
    run_git(seed, "config", "user.email", "test@example.invalid")
    (seed / "unrelated").write_text("x\n")
    run_git(seed, "add", "unrelated")
    run_git(seed, "commit", "-m", "unrelated")
    run_git(seed, "push", "-q", "origin", "main")
    run_git(clone, "remote", "add", "aaa-other", str(other))
    run_git(clone, "fetch", "-q", "aaa-other")
    run_git(clone, "remote", "set-head", "aaa-other", "main")
    tree = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(tree))
    items = GitWorktreeFactsProbe().discover(command(roots=(str(root),)), NOW)
    assert items[0].probe_errors == ()
    assert items[0].content_merged
