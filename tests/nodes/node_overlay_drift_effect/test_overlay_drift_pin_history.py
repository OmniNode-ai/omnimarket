# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pin to a revision the merged branch lacks (a squashed branch commit) is counted on its own.

Both backends: the in-memory pin history, and an omnimarket-like git clone asked with
``git merge-base --is-ancestor``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_overlay_drift_effect.adapters.pin_history import (
    GitPinHistory,
    InMemoryPinHistory,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayDriftCommand,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import (
    PinHistoryError,
    ProtocolPinHistory,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_overlay_drift_effect.overlay_drift_support import (
    MERGED,
    UNMERGED,
    World,
)

pytestmark = pytest.mark.unit

BACKENDS = ("memory", "git")


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin",
            "HOME": str(clone),
        },
    )
    return done.stdout.strip()


def _clone(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone with one commit on dev and one on a branch that never merged."""
    clone = tmp_path / "omnimarket"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "dev")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "merged")
    merged = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "-q", "-b", "feature")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "squashed away")
    unmerged = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "-q", "dev")
    return clone, merged, unmerged


def _history(backend: str, tmp_path: Path) -> tuple[ProtocolPinHistory, str, str]:
    if backend == "memory":
        return InMemoryPinHistory(frozenset({MERGED})), MERGED, UNMERGED
    clone, merged, unmerged = _clone(tmp_path)
    return GitPinHistory(clone, "dev", 30), merged, unmerged


@pytest.mark.parametrize("backend", BACKENDS)
def test_history_names_only_the_unmerged_revision(backend: str, tmp_path: Path) -> None:
    history, merged, unmerged = _history(backend, tmp_path)
    assert history.unmerged((merged, unmerged, merged)) == frozenset({unmerged})


@pytest.mark.parametrize("backend", BACKENDS)
def test_unmerged_pin_is_counted_named_and_does_not_decide_the_verdict(
    backend: str, tmp_path: Path
) -> None:
    history, merged, unmerged = _history(backend, tmp_path)
    world = World("memory", tmp_path)
    world.pin_lines = [line.replace(MERGED, merged) for line in world.pin_lines]
    first = world.pin_lines[0].split()[0]
    world.pin_lines[0] = world.pin_lines[0].replace(merged, unmerged)
    ledger = InMemoryLedger()
    result = world.handler(ledger, history).handle(
        ModelOverlayDriftCommand(correlation_id="c-1", write=True)
    )
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.counts.pin_unmerged == 1
    assert result.unmerged_pins == (first,)
    assert "pin_unmerged-1" in result.reasons
    assert "pin_unmerged=1" in ledger.lines[0]
    assert f"unmerged_pins={first}" in ledger.lines[0]


def test_without_a_pin_history_nothing_is_counted(tmp_path: Path) -> None:
    """The failing control: the same unmerged pin goes unseen when the check is not configured."""
    world = World("memory", tmp_path)
    world.break_pin_an_unmerged_revision()
    result = world.handler(None).handle(
        ModelOverlayDriftCommand(correlation_id="c-1", write=False)
    )
    assert result.counts.pin_unmerged == 0
    assert result.unmerged_pins == ()


def test_unknown_revision_is_unmerged_and_unknown_ref_cannot_answer(
    tmp_path: Path,
) -> None:
    clone, merged, _ = _clone(tmp_path)
    assert GitPinHistory(clone, "dev", 30).unmerged(("f" * 40,)) == frozenset(
        {"f" * 40}
    )
    with pytest.raises(PinHistoryError):
        GitPinHistory(clone, "no-such-ref", 30).unmerged((merged,))
    with pytest.raises(PinHistoryError):
        GitPinHistory(tmp_path / "absent", "dev", 30).unmerged((merged,))


def test_unreadable_history_is_a_reason_not_a_count(tmp_path: Path) -> None:
    world = World("memory", tmp_path)
    world.break_pin_an_unmerged_revision()
    result = world.handler(None, GitPinHistory(tmp_path / "absent", "dev", 30)).handle(
        ModelOverlayDriftCommand(correlation_id="c-1", write=False)
    )
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.counts.pin_unmerged == 0
    assert any(r.startswith("pin-history-unreadable") for r in result.reasons)
