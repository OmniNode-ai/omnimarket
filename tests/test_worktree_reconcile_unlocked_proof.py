# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No tree, locked or not, leaves the disk without a fresh ledger read (OMN-24560).

Every case is paired with a CONTROL that differs in exactly one fact and gives
the opposite answer.
"""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileDecisions,
)
from omnimarket.handlers.rules_ledger_claims import claimed, read_ledger
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_remover
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_remover import (
    GitWorktreeRemover,
)
from omnimarket.worktree_reconcile.rules import apply_model_verdicts, classify
from tests.test_worktree_reconcile_compute import facts
from tests.test_worktree_reconcile_effect import NO_UNITS, READER, Fakes, command

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 10, tzinfo=UTC)


def model_pin(
    fact: ModelWorktreeFacts, policy: ModelWorktreeReconcilePolicy
) -> ModelWorktreeReconcileDecisions:
    """The model's PIN_AND_REMOVE verdict on an ambiguous row for the tree."""
    row = ModelWorktreeDecisionRecord(
        path=fact.path, decision=Decision.NEEDS_HUMAN, reasons=("ambiguous",)
    )
    return apply_model_verdicts(
        ModelWorktreeReconcileDecisions(decisions=(row,), policy=policy),
        (fact,),
        (
            row.model_copy(
                update={
                    "decision": Decision.PIN_AND_REMOVE,
                    "reasons": ("model_verdict",),
                }
            ),
        ),
    )


class RecordingGit:
    """Stands in for the remover's git runner: records argv, answers rev-parse.

    The ``remove`` subcommand always fails with the recorded error; ``lock``
    fails too when the test asks for a failed relock.
    """

    def __init__(self, common_dir: str, *, fail_lock: bool = False) -> None:
        self.common_dir = common_dir
        self.fail_lock = fail_lock
        self.calls: list[tuple[str, ...]] = []
        self.removal_error = RuntimeError("git worktree remove failed (128)")

    def git(
        self, path: str, *args: str, ok: tuple[int, ...] = (0,), timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((path, *args))
        subcommand = args[1] if args[0] == "worktree" else args[0]
        if subcommand == "rev-parse":
            return subprocess.CompletedProcess(args, 0, f"{self.common_dir}\n", "")
        if subcommand == "remove":
            raise self.removal_error
        if subcommand == "lock" and self.fail_lock:
            raise RuntimeError("git worktree lock failed (128)")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_an_unread_ledger_holds_a_merged_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # The rules call the tree merged, but the ledger was never read: no removal.
    unread = facts(content_merged=True, ledger_known=False)
    assert classify(unread, policy) == (Decision.KEEP, "ledger_unread")
    # CONTROL: the same tree with the fresh read in hand leaves the disk.
    read = unread.model_copy(update={"ledger_known": True})
    assert classify(read, policy) == (Decision.REMOVE, "content_merged")


def test_an_unread_ledger_holds_a_tree_at_a_merged_pull_request_head() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # HEAD is a merged pull request's head, but the ledger was never read.
    unread = facts(merged_pr_head=True, ledger_known=False)
    assert classify(unread, policy) == (Decision.KEEP, "ledger_unread")
    # CONTROL: the fresh read in hand, the merged head proves the removal.
    read = unread.model_copy(update={"ledger_known": True})
    assert classify(read, policy) == (Decision.REMOVE, "pr_merged_head")


def test_an_owner_not_shown_finished_holds_pushed_work() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # The branch is pushed, but the fresh read does not show the owner finished.
    unfinished = facts(head_on_remote=True, owner_finished=False)
    assert classify(unfinished, policy) == (Decision.KEEP, "owner_unfinished")
    # CONTROL: the read shows the owner finished, so the tree leaves.
    finished = unfinished.model_copy(update={"owner_finished": True})
    assert classify(finished, policy) == (Decision.REMOVE, "work_on_remote")


def test_an_unread_ledger_holds_a_finished_pushed_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # A finished tree whose branch is pushed is held while the read is not fresh.
    unread = facts(head_on_remote=True, ledger_known=False)
    assert classify(unread, policy) == (Decision.KEEP, "ledger_unread")
    # CONTROL: the fresh read of the same ledger lets the tree go.
    read = unread.model_copy(update={"ledger_known": True})
    assert classify(read, policy) == (Decision.REMOVE, "work_on_remote")


def test_an_owner_not_shown_finished_holds_unpushed_stale_work() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # Stale unpushed work would be pinned to a backup branch and removed, but
    # the fresh read does not show the owner finished.
    unfinished = facts(commits_not_on_remote=2, owner_finished=False)
    assert classify(unfinished, policy) == (Decision.KEEP, "owner_unfinished")
    # CONTROL: the read shows the owner finished, so the work is pinned and the
    # tree leaves.
    finished = unfinished.model_copy(update={"owner_finished": True})
    assert classify(finished, policy) == (
        Decision.PIN_AND_REMOVE,
        "unpushed_work_stale",
    )


def test_an_unread_ledger_holds_even_an_empty_orphan_directory() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # An empty orphan directory holds no work, yet no tree leaves unread.
    orphan = facts(
        kind=Kind.ORPHAN_DIR,
        orphan_empty=True,
        ledger_known=False,
        owner_finished=False,
    )
    assert classify(orphan, policy) == (Decision.KEEP, "ledger_unread")
    # CONTROL: the read in hand, the empty directory leaves with no owner
    # verdict at all — an orphan holds nothing a verdict could judge.
    read = orphan.model_copy(update={"ledger_known": True})
    assert classify(read, policy) == (Decision.REMOVE, "orphan_empty")


def test_the_model_cannot_pin_work_the_ledger_does_not_prove_finished() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # The fresh read does not show the owner finished: the model's pin is refused.
    refused = model_pin(facts(commits_not_on_remote=2, owner_finished=False), policy)
    assert refused.decisions[0].decision == Decision.NEEDS_HUMAN
    assert refused.decisions[0].reasons == ("model_verdict_refused_by_rail",)
    # CONTROL: the read shows the owner finished, so the model's pin stands.
    accepted = model_pin(facts(commits_not_on_remote=2, owner_finished=True), policy)
    assert accepted.decisions[0].decision == Decision.PIN_AND_REMOVE
    assert accepted.decisions[0].reasons == ("model_verdict",)


def test_the_model_cannot_pin_a_dead_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # A gitdir git cannot read is never pinned and removed by the model.
    refused = model_pin(facts(gitdir_missing=True), policy)
    assert refused.decisions[0].decision == Decision.NEEDS_HUMAN
    assert refused.decisions[0].reasons == ("model_verdict_refused_by_rail",)
    # CONTROL: a tree whose gitdir git can read carries the model's pin.
    accepted = model_pin(facts(gitdir_missing=False), policy)
    assert accepted.decisions[0].decision == Decision.PIN_AND_REMOVE
    assert accepted.decisions[0].reasons == ("model_verdict",)


def test_revalidation_of_an_unread_ledger_refuses_the_removal() -> None:
    fact = facts(head_on_remote=True)
    fake = Fakes((fact,))
    # Between the decision and the removal the ledger read is no longer fresh.
    fake.fresh = fact.model_copy(update={"ledger_known": False})
    result = fake.handler().handle(command())
    assert fake.removed == []
    assert result.decided_events[0].outcome == "kept"
    assert result.decided_events[0].decision.reasons == ("revalidation_refused",)
    # CONTROL: the same tree, revalidated with the read still fresh, is removed.
    control = Fakes((fact,))
    control.fresh = fact
    control_result = control.handler().handle(command())
    assert control.removed == [fact.path]
    assert control_result.decided_events[0].outcome == "removed"


def test_the_remover_lifts_a_lane_lock_and_relocks_a_failed_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "trees"
    tree = root / "task" / "repo"
    tree.mkdir(parents=True)
    (tree / ".git").write_text("gitdir: /elsewhere/common\n")
    fact = facts(
        path=str(tree),
        root=str(root),
        kind=Kind.LINKED_WORKTREE,
        worktree_locked=True,
        worktree_lock_reason="lane",
    )
    expected_calls = [
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        ("worktree", "unlock", str(tree)),
        ("worktree", "remove", str(tree)),
        ("worktree", "lock", "--reason", "lane", str(tree)),
    ]
    git = RecordingGit(str(tmp_path / "common.git"))
    monkeypatch.setattr(adapter_remover, "git", git.git)
    with pytest.raises(RuntimeError) as removal_failure:
        GitWorktreeRemover().remove(fact, NOW)
    # The unlock ran before the removal, the lock went back on with its reason
    # afterwards, and the original error is raised unchanged.
    assert removal_failure.value is git.removal_error
    assert [call[1:] for call in git.calls] == expected_calls
    # CONTROL: when the relock fails too, the failure names the relock and
    # carries the original removal error as its cause.
    failing = RecordingGit(str(tmp_path / "common.git"), fail_lock=True)
    monkeypatch.setattr(adapter_remover, "git", failing.git)
    with pytest.raises(adapter_remover.RelockFailedError) as relock_failure:
        GitWorktreeRemover().remove(fact, NOW)
    assert relock_failure.value.__cause__ is failing.removal_error
    assert [call[1:] for call in failing.calls] == expected_calls


def test_a_claim_rolled_into_the_archive_still_names_its_tree(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    (ledger_dir / "archive").mkdir(parents=True)
    live = ledger_dir / "ledger.md"
    live.write_text("2099-12-31T23:30:00Z | STATUS | lane=a | working\n")
    rolled = ledger_dir / "archive" / "older.md"
    rolled.write_text(
        "2099-12-30T10:00:00Z | CLAIM | lane=a | ticket=OMN-5 | /trees/task-one\n"
    )
    os.utime(rolled, (datetime(2099, 12, 30, 10, tzinfo=UTC).timestamp(),) * 2)
    now = datetime(2100, 1, 1, tzinfo=UTC)
    read = read_ledger(
        live,
        now=now,
        quiet_hours=6.0,
        max_age_hours=6.0,
        lookback_days=3,
        max_bytes=10_000_000,
    )
    assert read.fresh
    assert len(read.live_claims) == 1
    assert claimed(Path("/trees/task-one"), Path("/trees"), read.live_claims)
    # CONTROL: the same files with no lookback skip the archive file, so the
    # rolled claim is invisible and nothing is named claimed.
    short = read_ledger(
        live,
        now=now,
        quiet_hours=6.0,
        max_age_hours=6.0,
        lookback_days=0,
        max_bytes=10_000_000,
    )
    assert short.fresh
    assert short.live_claims == ()
    assert not claimed(Path("/trees/task-one"), Path("/trees"), short.live_claims)


def test_the_policy_never_lets_a_host_turn_the_ledger_lookback_off() -> None:
    # A CLAIM that rolled into an archive file must stay readable: a lookback
    # of 0 days would hide it, so no overlay may set it.
    with pytest.raises(ValidationError):
        ModelWorktreeReconcilePolicy(ledger_lookback_days=0)
    # CONTROL: one day of lookback is a valid overlay.
    assert (
        ModelWorktreeReconcilePolicy(ledger_lookback_days=1).ledger_lookback_days == 1
    )


def _orphan_tree_with_ledger(
    tmp_path: Path, *, newest: str, archive: bool
) -> tuple[Path, Path]:
    """A registry root with one orphan directory, a live ledger and (optionally) an archive CLAIM."""
    root = tmp_path / "registry" / "trees"
    (root / "task-one").mkdir(parents=True)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    ledger = ledger_dir / "ledger.md"
    ledger.write_text(f"{newest} | STATUS | lane=a | working\n")
    if archive:
        (ledger_dir / "archive").mkdir()
        rolled = ledger_dir / "archive" / "older.md"
        rolled.write_text("2026-01-08T10:00:00Z | CLAIM | lane=a | task-one\n")
        os.utime(rolled, (NOW.timestamp() - 3600,) * 2)
    return root, ledger


def test_a_claim_in_a_rolled_file_keeps_an_unlocked_tree_at_discovery_and_at_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: ())
    probe = adapter_facts.GitWorktreeFactsProbe(
        unit_reader=NO_UNITS, ledger_reader=READER
    )
    root, ledger = _orphan_tree_with_ledger(
        tmp_path, newest="2026-01-09T23:30:00Z", archive=True
    )
    item = probe.discover(command(roots=(str(root),), ledger_path=str(ledger)), NOW)[0]
    assert item.ledger_known
    assert item.live_claim
    # The revalidation just before a removal reads the rolled file too, for a tree with no lock.
    assert not item.worktree_locked
    assert probe.revalidate(item, NOW).live_claim
    # CONTROL: the same ledger with no archive file names no claim on the tree.
    probe2 = adapter_facts.GitWorktreeFactsProbe(
        unit_reader=NO_UNITS, ledger_reader=READER
    )
    root2, ledger2 = _orphan_tree_with_ledger(
        tmp_path / "control", newest="2026-01-09T23:30:00Z", archive=False
    )
    item2 = probe2.discover(
        command(roots=(str(root2),), ledger_path=str(ledger2)), NOW
    )[0]
    assert item2.ledger_known
    assert not item2.live_claim
    assert not probe2.revalidate(item2, NOW).live_claim


def test_a_frozen_ledger_reads_unread_and_is_no_deferred_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts

    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: ())
    probe = adapter_facts.GitWorktreeFactsProbe(
        unit_reader=NO_UNITS, ledger_reader=READER
    )
    # Newest row 10 hours before the clock; the policy allows 6.
    root, ledger = _orphan_tree_with_ledger(
        tmp_path, newest="2026-01-09T14:00:00Z", archive=False
    )
    item = probe.discover(command(roots=(str(root),), ledger_path=str(ledger)), NOW)[0]
    assert not item.ledger_known
    assert item.ledger_unread_reason == "frozen"
    # Waiting does not read a frozen ledger, so it is no probe_deferred error.
    assert item.probe_errors == ()
    assert probe.revalidate(item, NOW).ledger_unread_reason == "frozen"
    # CONTROL: a host that names no ledger says so, in its own words.
    none = adapter_facts.GitWorktreeFactsProbe(
        unit_reader=NO_UNITS, ledger_reader=READER
    )
    undeclared = none.discover(command(roots=(str(root),)), NOW)[0]
    assert undeclared.ledger_unread_reason == "not_declared"
    assert undeclared.probe_errors == ()
