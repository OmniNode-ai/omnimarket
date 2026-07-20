# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14640 / OMN-14582 Phase 2: contract-topic-graph must be STRICT, not loose.

Per Operating Rule 5 (enforcement, not detection), a static seam-measurement
gate that CI Summary tolerates as skipped/cancelled is decoration, not
enforcement. Phase 1 (omnimarket#1754, merged to dev) relocated
``contract-topic-graph`` into ``ci.yml`` and wired it into ``ci-summary``'s
``needs:`` — but only into the LOOSE failure loop, where BOTH ``success`` and
``skipped`` pass. A skipped/cancelled contract-topic-graph run therefore still
reports the required ``CI Summary`` check GREEN — the seam-measurement tool
gates nothing.

This Phase 2 flip promotes ``contract-topic-graph`` to the same strict
success-only treatment as ``no-noncanonical-lifecycle-classes``
(``ci.yml`` OMN-14350 block): the job's result must be EXACTLY ``success``;
``skipped``/``cancelled``/``failure`` must all set ``FAILED=true``.

These tests assert the static wiring that makes the flip real:

1. ``contract-topic-graph`` stays a member of the ``ci-summary`` ``needs:``
   list (so the rollup WAITS for it — no race).
2. ``contract-topic-graph`` is REMOVED from the loose ``for check in ...``
   loop (where ``skipped`` silently passes).
3. A dedicated strict fail-closed block checks
   ``needs.contract-topic-graph.result`` and requires it to equal exactly
   ``success``; any other result (including ``skipped``/``cancelled``) must
   set ``FAILED=true``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.mark.unit
class TestContractTopicGraphStrictGateWiring:
    """Static proof that contract-topic-graph is strict, not loose (OMN-14640)."""

    def _parsed(self) -> dict:
        return yaml.safe_load(CI_WORKFLOW.read_text())

    def _content(self) -> str:
        return CI_WORKFLOW.read_text()

    def test_ci_workflow_exists(self) -> None:
        assert CI_WORKFLOW.exists(), f"CI workflow not found: {CI_WORKFLOW}"

    def test_contract_topic_graph_job_present(self) -> None:
        jobs = self._parsed().get("jobs", {})
        assert "contract-topic-graph" in jobs, (
            f"contract-topic-graph job missing from ci.yml. Jobs: {sorted(jobs)}"
        )

    def test_contract_topic_graph_in_ci_summary_needs(self) -> None:
        """The rollup must still WAIT for this job (no needs:/no race)."""
        jobs = self._parsed().get("jobs", {})
        summary = jobs.get("ci-summary", {})
        needs = summary.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "contract-topic-graph" in needs, (
            f"contract-topic-graph must stay in ci-summary needs. needs={needs}"
        )

    def test_contract_topic_graph_not_in_loose_failure_loop(self) -> None:
        """The LOOSE loop (success||skipped both pass) must NOT check this job.

        This is the RED assertion pre-flip: Phase 1 put contract-topic-graph
        into this exact loose-loop line. Phase 2 must remove it so a
        skipped/cancelled run cannot silently pass CI Summary.
        """
        content = self._content()
        assert (
            'contract-topic-graph=${{ needs.contract-topic-graph.result }}"; do'
            not in content
        ), (
            "contract-topic-graph must NOT be a member of the loose "
            "success||skipped loop — that is the un-enforced Phase 1 state "
            "this ticket exists to close (OMN-14640)."
        )

    def test_contract_topic_graph_has_dedicated_strict_result_variable(self) -> None:
        """A dedicated result variable must read the job's exact GHA result."""
        content = self._content()
        assert re.search(
            r'CONTRACT_[A-Z_]*RESULT="\$\{\{\s*needs\.contract-topic-graph\.result\s*\}\}"',
            content,
        ), (
            "Expected a dedicated strict result variable reading "
            "needs.contract-topic-graph.result (mirroring RATCHET_RESULT / "
            "COVERAGE_RESULT / REASON_CODE_RESULT strict blocks)."
        )

    def test_contract_topic_graph_strict_block_requires_exact_success(self) -> None:
        """The strict block must require RESULT != "success" -> FAILED=true.

        Anything other than the literal string 'success' (skipped, cancelled,
        failure) must flip FAILED=true, mirroring the no-noncanonical-lifecycle
        -classes / coverage-sweep-gate / merge-reason-code-gate strict blocks.
        """
        content = self._content()
        match = re.search(
            r'CONTRACT_[A-Z_]*RESULT="\$\{\{\s*needs\.contract-topic-graph\.result\s*\}\}"'
            r"(.*?)fi",
            content,
            re.DOTALL,
        )
        assert match, "Could not locate strict contract-topic-graph result block"
        block = match.group(1)
        assert '!= "success"' in block, (
            'Strict block must gate on RESULT != "success" (exact match, '
            "not the loose success||skipped check)"
        )
        assert "FAILED=true" in block, (
            "Strict block must set FAILED=true when the result is not exactly success"
        )
