# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Baseline-ratchet tests for the compliance_sweep org-jam fix (OMN-14541).

First activation of the compliance-sweep CI gate found 81 pre-existing
violations across 40 handler files this PR never touched. Since ``CI
Summary`` is the sole required status check on omnimarket ``dev``, hard-
failing on that debt would have permanently blocked every future dev PR
(the OMN-14505 "fail closed with no grandfather jams the org" lesson).

This module proves the fix in four parts, mirroring the contract-topic-graph
ratchet (OMN-14527) where the scope allows:

1. A genuinely NEW violation (not in any baseline) still fails the gate —
   the grandfather does not become a blanket exemption.
2. A violation whose key IS in the trusted baseline does not fail the gate.
3. The ratchet cannot be defeated by adding a violation and its baseline
   entry in the same commit — ``evaluate_ratchet`` keys off
   ``trusted_accepted`` (the merge-base baseline), never the PR-local one.
4. A stale baseline entry (accepted key with no matching current violation)
   is surfaced for visibility but does NOT fail the gate here — unlike
   contract-topic-graph's fixed whole-corpus scope, this handler is
   routinely called against an arbitrary partial scan scope, where a
   missing key is not reliable evidence the violation was fixed rather
   than simply out of scope for this particular call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    ComplianceSweepRequest,
    ModelComplianceViolation,
    NodeComplianceSweep,
    evaluate_ratchet,
    merge_base_accepted_keys,
)

pytestmark = pytest.mark.integration

_TOPIC_HANDLER = (
    'TOPIC = "onex.evt.omnimarket.new-thing-happened.v1"\n\n\n'
    "def handle(envelope):\n    return envelope\n"
)


def _write_handler(root: Path, node: str, source: str) -> str:
    handler_dir = root / "myrepo" / "src" / "nodes" / node / "handlers"
    handler_dir.mkdir(parents=True)
    (handler_dir / f"handler_{node}.py").write_text(source)
    return str(root / "myrepo")


def _write_baseline(path: Path, accepted: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "accepted:\n" + "".join(f"  - {k!r}\n" for k in accepted)
        if accepted
        else "accepted: []\n"
    )


class TestNewViolationFailsTheGate:
    """A genuinely NEW violation must still fail — the whole point of the
    fix is to grandfather EXISTING debt, not to disable the check."""

    def test_new_hardcoded_topic_not_in_baseline_fails_gate(
        self, tmp_path: Path
    ) -> None:
        target = _write_handler(tmp_path, "node_new_thing_effect", _TOPIC_HANDLER)
        empty_baseline = tmp_path / "empty_baseline.yaml"
        _write_baseline(empty_baseline, [])

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(
                target_dirs=[target],
                checks=["hardcoded-topics"],
                baseline_path=str(empty_baseline),
            )
        )

        assert result.status == "violations_found", (
            "a NEW hardcoded-topic violation with no baseline entry must "
            f"fail the gate, got status={result.status!r}"
        )
        assert len(result.new_violations) == 1
        assert result.new_violations[0].violation_type == "HARDCODED_TOPIC"
        assert result.baselined_violations == []

    def test_same_violation_baselined_does_not_fail_gate(self, tmp_path: Path) -> None:
        """The identical violation, once its key is in the (local, since
        there is no real git history here) baseline, is grandfathered."""
        target = _write_handler(tmp_path, "node_new_thing_effect", _TOPIC_HANDLER)

        # Discover the real key the same way the gate does, then baseline it.
        probe = NodeComplianceSweep().handle(
            ComplianceSweepRequest(
                target_dirs=[target],
                checks=["hardcoded-topics"],
                baseline_path=str(tmp_path / "unused.yaml"),
            )
        )
        key = probe.violations[0].key()

        baseline = tmp_path / "baseline_with_entry.yaml"
        _write_baseline(baseline, [key])

        result = NodeComplianceSweep().handle(
            ComplianceSweepRequest(
                target_dirs=[target],
                checks=["hardcoded-topics"],
                baseline_path=str(baseline),
            )
        )

        assert result.status == "compliant", (
            "a violation whose key is in the baseline must not fail the "
            f"gate, got status={result.status!r}"
        )
        assert result.new_violations == []
        assert len(result.baselined_violations) == 1


class TestRatchetRejectsSmuggling:
    """The ratchet compares against the TRUSTED (merge-base) baseline, not
    the PR's own copy — a PR cannot add a violation and its baseline entry
    in the same commit and have the check miss it."""

    def test_violation_smuggled_via_its_own_baseline_entry_still_new(self) -> None:
        violation = ModelComplianceViolation(
            repo="",
            handler_path="src/nodes/node_x/handlers/handler_x.py",
            node_name="node_x",
            violation_type="HARDCODED_TOPIC",
            message='Hardcoded topic string: TOPIC = "onex.evt.x.y.v1"',
            severity="ERROR",
            line=1,
        )
        key = violation.key()

        # The PR's own baseline already "accepts" the key — as if the
        # implementer ran --write-baseline in the same commit that
        # introduced the violation.
        local_accepted = {key}
        # The merge-base baseline (what actually predates this PR) does not.
        trusted_accepted: set[str] = set()

        new_violations, baselined, fixed = evaluate_ratchet(
            [violation], local_accepted, trusted_accepted
        )

        assert [v.key() for v in new_violations] == [key]
        assert baselined == []
        assert fixed == []

    def test_violation_genuinely_predating_pr_is_trusted(self) -> None:
        """The same key is silent when the MERGE-BASE baseline already has
        it — genuine pre-existing debt, not a same-commit addition."""
        violation = ModelComplianceViolation(
            repo="",
            handler_path="src/nodes/node_x/handlers/handler_x.py",
            node_name="node_x",
            violation_type="HARDCODED_TOPIC",
            message='Hardcoded topic string: TOPIC = "onex.evt.x.y.v1"',
            severity="ERROR",
            line=1,
        )
        key = violation.key()
        local_accepted = {key}
        trusted_accepted = {key}  # genuinely pre-existing per merge-base

        new_violations, baselined, fixed = evaluate_ratchet(
            [violation], local_accepted, trusted_accepted
        )

        assert new_violations == []
        assert [v.key() for v in baselined] == [key]
        assert fixed == []


class TestFixedKeysAreReportedNotEnforced:
    """A baselined key with no matching current violation is surfaced in
    ``fixed_keys`` for visibility, but does NOT fail the gate here — unlike
    the contract-topic-graph ratchet's fixed whole-corpus scope, this
    handler is routinely called against an arbitrary PARTIAL scope (a
    single repo, one target_dir, a synthetic test fixture), where a missing
    key is not reliable evidence the violation was actually fixed rather
    than simply out of scope for this particular scan."""

    def test_stale_baseline_key_is_returned_as_fixed_not_as_new_or_baselined(
        self,
    ) -> None:
        # No current violations at all -- the baseline still claims one key.
        local_accepted = {"HARDCODED_TOPIC::node_gone::path.py::message"}
        trusted_accepted = {"HARDCODED_TOPIC::node_gone::path.py::message"}

        new_violations, baselined, fixed = evaluate_ratchet(
            [], local_accepted, trusted_accepted
        )

        assert new_violations == []
        assert baselined == []
        assert fixed == ["HARDCODED_TOPIC::node_gone::path.py::message"]


class TestMergeBaseAcceptedKeys:
    def test_returns_none_outside_a_git_repo(self, tmp_path: Path) -> None:
        """No ``.git`` reachable from the baseline path resolves to unknown
        (fall back to trusting the local baseline), not an empty accept set
        (which would treat all pre-existing debt as brand new)."""
        baseline_path = tmp_path / "not_a_repo" / "baseline.yaml"
        baseline_path.parent.mkdir(parents=True)
        _write_baseline(baseline_path, [])

        assert merge_base_accepted_keys(baseline_path) is None
