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

This Phase 2 flip promotes ``contract-topic-graph`` to strict success-only
treatment: the job's result must be EXACTLY ``success``;
``skipped``/``cancelled``/``failure`` must all block the required
``CI Summary`` context.

OMN-14127 fan-out (CI-G2): ``ci-summary`` migrated from a ``needs``-gated shell
loop to a NO-``needs`` fail-closed poller (``scripts/ci/ci_summary_gate.py``) so
the required context can never go absent under fleet saturation. The strict
contract-topic-graph enforcement therefore moved OUT of the YAML strict-block
and INTO the poller's ``STRICT_GATE_JOBS`` anchor (by the job's display name).

These tests assert that relocation preserved the strict enforcement:

1. the ``contract-topic-graph`` job still exists in ``ci.yml``;
2. ``ci-summary`` is a NO-``needs`` poller invoking the gate script (no race:
   the poller waits until the job terminalizes); and
3. ``contract-topic-graph`` is a member of the poller's ``STRICT_GATE_JOBS``
   (must be present + completed + EXACTLY ``success``; skipped/cancelled/
   failure all block), NOT the skippable set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import SKIPPABLE_GATE_JOBS, STRICT_GATE_JOBS

REPO_ROOT = Path(__file__).parent.parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The contract-topic-graph job's Actions display name == its job key.
CONTRACT_TOPIC_GRAPH_DISPLAY_NAME = "contract-topic-graph"


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

    def test_ci_summary_is_no_needs_poller(self) -> None:
        """OMN-14127: ci-summary must be a NO-``needs`` poller (never wedges).

        The poller waits until every anchored job terminalizes before it
        renders a verdict, so it still WAITS for contract-topic-graph — but via
        the job list, not a ``needs`` edge that can go absent under saturation.
        """
        summary = self._parsed().get("jobs", {}).get("ci-summary", {})
        assert "needs" not in summary, (
            "ci-summary must have NO `needs:` (OMN-14127 poller pattern); a "
            "needs-gated required context wedges the PR under fleet saturation."
        )
        assert "scripts/ci/ci_summary_gate.py" in self._content(), (
            "ci-summary must invoke the fail-closed poller gate script"
        )

    def test_contract_topic_graph_is_strict_gate_in_poller(self) -> None:
        """Strict enforcement relocated to STRICT_GATE_JOBS (exact success).

        A strict gate must be present + completed + EXACTLY ``success``;
        skipped/cancelled/failure all block the required CI Summary context —
        the same enforcement the old YAML strict-block provided (OMN-14640),
        now in the poller.
        """
        assert CONTRACT_TOPIC_GRAPH_DISPLAY_NAME in STRICT_GATE_JOBS, (
            f"{CONTRACT_TOPIC_GRAPH_DISPLAY_NAME!r} must be in STRICT_GATE_JOBS "
            "so a skipped/cancelled/failed run blocks the required CI Summary "
            f"(OMN-14640). STRICT_GATE_JOBS={STRICT_GATE_JOBS}"
        )

    def test_contract_topic_graph_not_skippable(self) -> None:
        """It must NOT be in the skippable (success||skipped) set."""
        assert CONTRACT_TOPIC_GRAPH_DISPLAY_NAME not in SKIPPABLE_GATE_JOBS, (
            "contract-topic-graph must be strict, not skippable — a skipped run "
            "must fail closed (OMN-14640), not silently pass."
        )
