# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tick's exit reads real failures, not load or a standing fact about the host (OMN-22799).

Recorded case: on a lab host every worktree reconcile tick exited 1. The 22:15Z tick scanned
517 trees, removed none and logged 43 failures: 30 git timeouts under lane load, 11 value
errors (one an orphan directory holding a nested repository, ten a clone refresh) and 2
reader failures for a repository the host holds no canonical clone of. The fixture is that
tick's own probe errors.
"""

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import ModelWorktreeFacts
from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_commands import (
    CommandMergedPrReader,
    MergedPrEvidenceUnavailableError,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
    GitWorktreeFactsProbe,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.probe_classes import (
    PROBE_DEFERRED_PREFIX,
    PROBE_NEEDS_HUMAN_PREFIX,
    probe_class,
)
from tests.test_worktree_reconcile_compute import facts
from tests.test_worktree_reconcile_effect import (
    NO_UNITS,
    NOW,
    FakeMergedPrReader,
    Fakes,
    command,
    git_fixture,
    probe_for,
    run_git,
    run_with,
    squash_merged_then_moved_on,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent / "fixtures/worktree_reconcile/tick_2215_probe_errors.json"
)
DEFERRED_TIMEOUT = f"{PROBE_DEFERRED_PREFIX}probe_timeout"
NESTED = f"{PROBE_NEEDS_HUMAN_PREFIX}nested_repository"
NO_EVIDENCE = f"{PROBE_NEEDS_HUMAN_PREFIX}merged_pr_evidence_unavailable"


def inject_git_timeout(monkeypatch: pytest.MonkeyPatch, subcommand: str) -> None:
    real_git = adapter_facts.git

    def slow(
        path: str, *args: str, ok: tuple[int, ...] = (0,), timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == subcommand or subcommand in args:
            raise subprocess.TimeoutExpired("git", timeout)
        return real_git(path, *args, ok=ok, timeout=timeout)

    monkeypatch.setattr(adapter_facts, "git", slow)


def test_the_recorded_tick_reads_exit_zero_for_load_and_host_facts_only() -> None:
    """The 22:15Z tick's rows, each recorded class given the error the adapter now writes
    for that cause (the adapter cases below prove each name): load is deferred, the
    nested repository and the missing evidence source go to a person, and the ten clone
    refresh errors, whose cause the tick did not record, are still failures."""
    recorded = json.loads(FIXTURE.read_text())
    assert recorded["recorded_completed"]["failures"] == 43
    now_reads = {
        ("linked_worktree", "probe_failed:TimeoutExpired"): DEFERRED_TIMEOUT,
        ("orphan_dir", "probe_failed:ValueError"): NESTED,
        ("linked_worktree", "merged_pr_read_failed"): NO_EVIDENCE,
        ("linked_worktree", "probe_failed:ValueError"): "probe_failed:ValueError",
    }
    items: list[ModelWorktreeFacts] = []
    for row in recorded["rows"]:
        (error,) = row["probe_errors"]
        items.append(
            facts(
                path=f"/trees/{row['tree']}",
                kind=Kind(row["kind"]),
                probe_errors=(now_reads[(row["kind"], error)],),
            )
        )
    fake = Fakes(tuple(items))
    result = fake.handler().handle(command(decider_command=None))
    done = result.completed_event
    assert Counter(row["probe_errors"][0] for row in recorded["rows"]) == {
        "probe_failed:TimeoutExpired": 30,
        "probe_failed:ValueError": 11,
        "merged_pr_read_failed": 2,
    }
    deferred = [r for r in result.decided_events if probe_class(r.facts) == "deferred"]
    assert len(deferred) == 30
    assert {r.decision.reasons for r in deferred} == {("facts_unreadable",)}
    assert {r.outcome for r in deferred} == {"kept"}
    assert done.needs_human == 3
    assert done.failures == 10
    assert done.deferred == 30
    assert done.deferred_paths == tuple(r.facts.path for r in deferred)
    assert not fake.removed
    assert not fake.pinned
    assert done.removed == 0


def test_only_load_and_host_facts_leave_a_run_with_no_failure() -> None:
    items = (
        facts(path="/trees/a", probe_errors=(DEFERRED_TIMEOUT,)),
        facts(path="/trees/b", kind=Kind.ORPHAN_DIR, probe_errors=(NESTED,)),
        facts(path="/trees/c", probe_errors=(NO_EVIDENCE,)),
    )
    result = Fakes(items).handler().handle(command())
    done = result.completed_event
    assert (done.failures, done.needs_human) == (0, 2)
    assert (done.deferred, done.deferred_paths) == (1, ("/trees/a",))
    assert result.decided_events[0].outcome == "kept"
    assert done.needs_human_paths == ("/trees/b", "/trees/c")
    reasons = {row.facts.path: row.decision.reasons for row in result.decided_events}
    assert reasons["/trees/a"] == ("facts_unreadable",)
    assert reasons["/trees/b"] == ("nested_repository",)
    assert reasons["/trees/c"] == ("merged_pr_evidence_unavailable",)


@pytest.mark.parametrize(
    "errors",
    [
        ("probe_failed:ValueError",),
        ("merged_pr_read_failed",),
        ("process_snapshot_failed", DEFERRED_TIMEOUT),
        (NESTED, "unit_snapshot_failed"),
    ],
)
def test_any_other_probe_error_alone_or_beside_a_class_is_a_failure(
    errors: tuple[str, ...],
) -> None:
    result = (
        Fakes((facts(path="/trees/a", probe_errors=errors),))
        .handler()
        .handle(command())
    )
    assert result.completed_event.failures == 1
    assert result.decided_events[0].decision.reasons == ("facts_unreadable",)


def test_a_held_tree_with_a_needs_human_error_stays_a_keep() -> None:
    held = facts(path="/trees/a", probe_errors=(NESTED,), live_process=True)
    result = Fakes((held,)).handler().handle(command())
    assert result.decided_events[0].decision.reasons == ("facts_unreadable",)
    assert result.completed_event.needs_human == 0


def test_a_git_timeout_defers_the_tree_and_a_removable_tree_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = tmp_path / "again"
    second.mkdir()
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    # Control: the same tree, with nothing slow, is removed.
    control = run_with(probe_for(reader, monkeypatch), root, monkeypatch)
    assert control.completed_event.removed == 1
    assert not path.exists()

    root, path, head = squash_merged_then_moved_on(second)
    inject_git_timeout(monkeypatch, "status")
    result = run_with(
        probe_for(FakeMergedPrReader(frozenset({head})), monkeypatch),
        root,
        monkeypatch,
        execute=True,
    )
    row = result.decided_events[0]
    assert row.facts.probe_errors == (DEFERRED_TIMEOUT,)
    assert row.decision.reasons == ("facts_unreadable",)
    done = result.completed_event
    assert (done.failures, done.removed) == (0, 0)
    assert path.exists()


def test_a_clone_refresh_that_timed_out_defers_and_one_that_failed_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, _ = git_fixture(tmp_path)
    path = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(path))
    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: ())
    for status, expected in (
        ("timeout", (f"{PROBE_DEFERRED_PREFIX}remote_refresh_timeout",)),
        ("failed", ("probe_failed:ValueError",)),
    ):
        monkeypatch.setattr(adapter_facts, "_fetch_all", lambda _clone, s=status: s)
        (item,) = GitWorktreeFactsProbe(unit_reader=NO_UNITS).discover(
            command(roots=(str(root),)), NOW
        )
        assert item.probe_errors == expected
        assert probe_class(item) == ("deferred" if status == "timeout" else "failed")


def test_a_nested_repository_is_a_person_s_call_not_a_failure(tmp_path: Path) -> None:
    clone, root, _ = git_fixture(tmp_path)
    task = root / "slice"
    nested = task / "inner"
    nested.mkdir(parents=True)
    run_git(tmp_path, "init", "--quiet", str(nested))
    (task / "notes.txt").write_text("loose\n")
    probe = GitWorktreeFactsProbe(unit_reader=NO_UNITS)
    probe._command = command(roots=(str(root),))
    item = probe._probe(task, root, NOW)
    assert item.kind == Kind.ORPHAN_DIR
    assert item.probe_errors == (NESTED,)
    assert probe_class(item) == "needs_human"
    assert clone.exists()


def test_a_tree_that_vanishes_mid_probe_defers_and_a_missing_file_in_a_live_tree_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """h053 tick 2026-11-09T01:45Z: three orphan directories a lane closed out during the scan
    raised FileNotFoundError and made the tick exit 1."""
    _, root, _ = git_fixture(tmp_path)
    probe = GitWorktreeFactsProbe(unit_reader=NO_UNITS)
    probe._command = command(roots=(str(root),))
    for vanishes, expected, klass in (
        (True, (f"{PROBE_DEFERRED_PREFIX}path_vanished",), "deferred"),
        (False, ("probe_failed:FileNotFoundError",), "failed"),
    ):
        task = root / ("gone" if vanishes else "present")
        task.mkdir()
        (task / "notes.txt").write_text("loose\n")

        def walk(
            path: Path,
            *_args: object,
            _task: Path = task,
            _gone: bool = vanishes,
            **_kw: object,
        ) -> None:
            if _gone:
                (_task / "notes.txt").unlink()
                _task.rmdir()
            raise FileNotFoundError(str(path))

        monkeypatch.setattr(adapter_facts.os, "walk", walk)
        item = probe._probe(task, root, NOW)
        assert item.probe_errors == expected
        assert probe_class(item) == klass
        monkeypatch.undo()


def test_a_reader_with_no_evidence_source_is_needs_human_and_a_broken_one_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = squash_merged_then_moved_on(tmp_path)

    class NoSource(FakeMergedPrReader):
        def merged_heads(self, argv: list[str], slug: str) -> frozenset[str]:
            raise MergedPrEvidenceUnavailableError(slug)

    result = run_with(probe_for(NoSource(), monkeypatch), root, monkeypatch)
    assert result.decided_events[0].facts.probe_errors == (NO_EVIDENCE,)
    assert result.decided_events[0].decision.reasons == (
        "merged_pr_evidence_unavailable",
    )
    assert (result.completed_event.failures, result.completed_event.needs_human) == (
        0,
        1,
    )

    broken = run_with(
        probe_for(FakeMergedPrReader(fails=True), monkeypatch), root, monkeypatch
    )
    assert broken.decided_events[0].facts.probe_errors == ("merged_pr_read_failed",)
    assert broken.completed_event.failures == 1


def test_the_command_reader_names_a_missing_evidence_source_from_its_own_message() -> (
    None
):
    reader = CommandMergedPrReader()
    absent = [
        "bash",
        "-c",
        "echo 'local: no canonical clone of {slug} on this host' >&2; exit 3",
    ]
    with pytest.raises(MergedPrEvidenceUnavailableError):
        reader.merged_heads(absent, "owner/repo")
    with pytest.raises(RuntimeError) as broken:
        reader.merged_heads(["bash", "-c", "echo boom >&2; exit 3"], "owner/repo")
    assert not isinstance(broken.value, MergedPrEvidenceUnavailableError)
