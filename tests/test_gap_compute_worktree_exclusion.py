"""Worktree-exclusion tests for node_gap_compute (OMN-13638).

The gap sweep walks the canonical repo clones, which contain nested git
worktrees under ``.claude/worktrees/`` and (when scanned from $OMNI_HOME)
sibling per-ticket clones under ``omni_worktrees/``. Those are frozen duplicate
copies of the same source tree; ``contract.yaml`` files inside them re-surface
already-fixed contracts. The 2026-06-26 dev sweep showed 62/128 findings (48%)
sourced from stale worktree clones, including the only CRITICAL
``topic_name_mismatch`` (``routing.feedback``) which existed ONLY in
``omni_worktrees/OMN-13249/`` — the canonical clone was fixed long ago.

``HandlerGapCompute._detect`` must prune ``omni_worktrees`` and
``.claude/worktrees`` path segments from the ``contract.yaml`` walk so only
canonical clones are scanned. Sibling-class fix to OMN-13521
(doc_freshness_sweep).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
    _WORKTREE_EXCLUDED_SEGMENTS,
    HandlerGapCompute,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    ModelGapComputeRequest,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_result import (
    EnumGapSeverity,
    EnumGapStatus,
)


def _write_contract(path: Path, *, topic: str, name: str = "node_sample") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "node_type": "effect",
        "terminal_event": topic,
        "event_bus": {
            "subscribe_topics": [topic],
            "publish_topics": [topic],
        },
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


@pytest.mark.unit
class TestGapDetectExcludesNestedWorktrees:
    """`_detect` must not scan contracts inside nested worktree copies."""

    def test_excludes_claude_worktrees(self, tmp_path: Path) -> None:
        repo = tmp_path / "omniintelligence"
        # Canonical contract: clean, canonical topic.
        _write_contract(
            repo / "src/pkg/nodes/node_clean/contract.yaml",
            topic="onex.evt.omniclaude.routing-feedback.v1",
            name="node_clean",
        )
        # Stale worktree snapshot with a non-canonical (phantom) topic.
        _write_contract(
            repo
            / ".claude/worktrees/OMN-13249/src/pkg/nodes/node_phantom/contract.yaml",
            topic="routing.feedback",
            name="node_phantom",
        )

        result = HandlerGapCompute().handle(
            ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
        )

        # The phantom contract under .claude/worktrees must not be scanned, so
        # no topic_name_mismatch CRITICAL is emitted and the sweep is clean.
        assert result.status == EnumGapStatus.CLEAN
        assert result.findings == []
        assert all(".claude/worktrees" not in f.path for f in result.findings)
        assert all(f.severity != EnumGapSeverity.CRITICAL for f in result.findings)

    def test_excludes_omni_worktrees(self, tmp_path: Path) -> None:
        repo = tmp_path / "omniintelligence"
        _write_contract(
            repo / "src/pkg/nodes/node_clean/contract.yaml",
            topic="onex.evt.omniclaude.routing-feedback.v1",
            name="node_clean",
        )
        # Sibling per-ticket clone nested under an omni_worktrees segment.
        _write_contract(
            repo
            / "omni_worktrees/OMN-13249/omniintelligence/src/pkg/nodes/node_phantom/contract.yaml",
            topic="routing.feedback",
            name="node_phantom",
        )

        result = HandlerGapCompute().handle(
            ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
        )

        assert result.status == EnumGapStatus.CLEAN
        assert result.findings == []
        assert all("omni_worktrees" not in f.path for f in result.findings)

    def test_canonical_contract_still_scanned(self, tmp_path: Path) -> None:
        """Pruning worktrees must not suppress canonical findings.

        A genuinely non-canonical topic in the canonical clone must still emit
        a CRITICAL topic_name_mismatch — the prune is path-scoped, not a
        blanket suppression.
        """
        repo = tmp_path / "omniintelligence"
        _write_contract(
            repo / "src/pkg/nodes/node_bad/contract.yaml",
            topic="routing.feedback",
            name="node_bad",
        )

        result = HandlerGapCompute().handle(
            ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
        )

        assert result.status == EnumGapStatus.FINDINGS
        assert any(
            f.rule_name == "topic_name_mismatch"
            and f.severity == EnumGapSeverity.CRITICAL
            for f in result.findings
        )

    def test_contracts_checked_excludes_worktree_copies(self, tmp_path: Path) -> None:
        """Only the canonical contract is counted; worktree copies are pruned."""
        repo = tmp_path / "omniintelligence"
        _write_contract(
            repo / "src/pkg/nodes/node_clean/contract.yaml",
            topic="onex.evt.omniclaude.routing-feedback.v1",
            name="node_clean",
        )
        _write_contract(
            repo / ".claude/worktrees/OMN-1/src/pkg/nodes/node_dup/contract.yaml",
            topic="onex.evt.omniclaude.routing-feedback.v1",
            name="node_dup",
        )
        _write_contract(
            repo / "omni_worktrees/OMN-2/repo/src/pkg/nodes/node_dup2/contract.yaml",
            topic="onex.evt.omniclaude.routing-feedback.v1",
            name="node_dup2",
        )

        result = HandlerGapCompute().handle(
            ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
        )

        # Exactly one canonical contract scanned; both worktree copies pruned.
        assert result.contracts_checked == 1


@pytest.mark.unit
def test_worktree_excluded_segments_cover_both_kinds() -> None:
    """The exclusion set must prune both worktree directory conventions."""
    assert "omni_worktrees" in _WORKTREE_EXCLUDED_SEGMENTS
    assert "worktrees" in _WORKTREE_EXCLUDED_SEGMENTS
