# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reconcile run that removes needs a claim read that succeeded (OMN-26648).

On 2026-11-24T15:26:39Z the h202 reconcile tick removed two trees of lane taskset-1540, whose CLAIM
(15:01:03Z) was open and whose last row was written at 15:25:13Z. The host's overlay declares no
``ledger_path``; the adapter then reads no ledger, holds an empty claim set, and says nothing: every
tree reads "no live claim". A host that removes with no claim read, an unreadable ledger or a ledger
no live lane's CLAIM can be in (its newest row is past the bound) keeps every tree and reports the
read as a probe failure, which is how the run's completed event counts it.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_ledger_claims import ledger_refusal
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
    GitWorktreeFactsProbe,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_remover import (
    GitWorktreeRemover,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcile,
)
from tests.test_worktree_reconcile_effect import (
    NO_UNITS,
    Fakes,
    command,
    git_fixture,
    publish_topics,
    run_git,
)

pytestmark = pytest.mark.unit

LATE = datetime(2100, 1, 1, tzinfo=UTC)


def _merged_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """One clean, pushed, merged tree: the reconcile rules remove it unless a live lane holds it."""
    clone, root, _ = git_fixture(tmp_path)
    tree = root / "taskset" / "repo"
    run_git(clone, "worktree", "add", "-b", "landed", str(tree))
    (tree / "landed").write_text("landed\n")
    run_git(tree, "add", "landed")
    run_git(tree, "commit", "-m", "landed")
    run_git(clone, "merge", "landed")
    run_git(clone, "push", "origin", "main")
    monkeypatch.setattr(
        "omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts.process_cwds",
        lambda: (),
    )
    return root, tree


def _run(root: Path, monkeypatch: pytest.MonkeyPatch, **updates: object):  # type: ignore[no-untyped-def]
    fake = Fakes(())
    monkeypatch.setattr(fake, "now", lambda: LATE)
    decided, completed = publish_topics()
    handler = HandlerWorktreeReconcile(
        probe=GitWorktreeFactsProbe(unit_reader=NO_UNITS),
        decider=fake,
        pinner=fake,
        remover=GitWorktreeRemover(),
        clock=fake,
        publisher=fake,
        decided_topic=decided,
        completed_topic=completed,
    )
    return handler.handle(command(roots=(str(root),), decider_command=None, **updates))


def _ledger(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("".join(f"{row}\n" for row in rows))
    return path


FRESH = "2099-12-31T23:30:00Z | STATUS | lane=someone | ticket=OMN-1 | a row"
STALE = "2026-11-05T13:40:11Z | STATUS | lane=someone | ticket=OMN-1 | the published snapshot ends"


def test_a_removing_host_with_no_declared_ledger_keeps_every_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _merged_tree(tmp_path, monkeypatch)
    result = _run(root, monkeypatch, ledger_path=None)
    assert tree.exists()
    assert result.completed_event.removed == 0
    assert result.completed_event.failures >= 1
    (event,) = result.decided_events
    assert any(
        error.startswith("ledger_unreadable") for error in event.facts.probe_errors
    )


def test_a_ledger_whose_newest_row_is_past_the_bound_keeps_every_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _merged_tree(tmp_path, monkeypatch)
    result = _run(root, monkeypatch, ledger_path=str(_ledger(tmp_path, STALE)))
    assert tree.exists()
    assert result.completed_event.removed == 0
    (event,) = result.decided_events
    assert any("bound" in error for error in event.facts.probe_errors)


def test_a_ledger_with_no_dated_row_keeps_every_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _merged_tree(tmp_path, monkeypatch)
    result = _run(
        root, monkeypatch, ledger_path=str(_ledger(tmp_path, "", "no row here"))
    )
    assert tree.exists()
    assert result.completed_event.removed == 0


def test_a_missing_ledger_file_keeps_every_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _merged_tree(tmp_path, monkeypatch)
    result = _run(root, monkeypatch, ledger_path=str(tmp_path / "absent.md"))
    assert tree.exists()
    assert result.completed_event.removed == 0


def test_a_fresh_ledger_that_names_no_claim_still_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: what withholds the removal is the failed read, not the lack of a claim."""
    root, tree = _merged_tree(tmp_path, monkeypatch)
    result = _run(root, monkeypatch, ledger_path=str(_ledger(tmp_path, STALE, FRESH)))
    assert not tree.exists()
    assert result.completed_event.removed == 1
    assert result.completed_event.failures == 0


def test_a_fresh_ledger_with_an_open_claim_on_the_tree_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _merged_tree(tmp_path, monkeypatch)
    claim = "2099-12-31T23:00:00Z | CLAIM | lane=taskset | ticket=OMN-2 | worktree=taskset/repo"
    result = _run(root, monkeypatch, ledger_path=str(_ledger(tmp_path, claim, FRESH)))
    assert tree.exists()
    assert result.completed_event.removed == 0


def test_a_dry_run_with_no_declared_ledger_reports_no_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _merged_tree(tmp_path, monkeypatch)
    result = _run(root, monkeypatch, ledger_path=None, execute=False)
    assert tree.exists()
    (event,) = result.decided_events
    assert event.outcome == "dry_run"
    assert not any(
        error.startswith("ledger_unreadable") for error in event.facts.probe_errors
    )


def test_the_bound_is_a_positive_policy_field_with_a_default() -> None:
    assert ModelWorktreeReconcilePolicy().claims_max_age_hours == 3.0
    with pytest.raises(ValidationError):
        ModelWorktreeReconcilePolicy(claims_max_age_hours=0)


def test_ledger_refusal_names_what_is_wrong() -> None:
    now = datetime(2026, 11, 24, 15, 26, tzinfo=UTC)
    assert "no dated row" in ledger_refusal("", now, 3.0)
    assert "past the 3.0h bound" in ledger_refusal(STALE, now, 3.0)
    fresh = (
        "2026-11-24T15:25:13Z | STATUS | lane=taskset-1540 | ticket=OMN-26639 | a row"
    )
    assert ledger_refusal(fresh, now, 3.0) == ""
