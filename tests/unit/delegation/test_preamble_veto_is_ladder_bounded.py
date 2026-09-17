# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The preamble segmentation rule cannot loop; only the ladder repeats it (OMN-13640).

WHY THIS EXISTS. On 2026-09-15 a delegation was observed being vetoed
repeatedly, on the same rung, until the ladder exhausted — three local
``heuristic_veto`` refusals at quality 0.92 before it climbed. The reading
offered at the time was that the ``unpaired_closing_tag`` segmentation rule
(``EnumReasoningBoundaryRule.UNPAIRED_CLOSING_TAG``) was "vetoing in a loop",
which would make it an unbounded retry surface and a real defect.

It is not, and this module is the falsifier rather than an assertion of the
happy path. ``segment_reasoning_preamble`` is a pure function: it tries the
declared rules once each, in order, and returns — there is no loop, no
re-invocation, no retry on failing to find a boundary. Repetition comes from
the LADDER calling it once per attempt, and the ladder is bounded by the task
class's ``max_escalations`` and the tier's ``max_retries``.

The distinction matters because the two have different fixes. An unbounded
internal retry would need a bound added. A bounded ladder re-running a
deterministic function on three different model outputs is the ladder working
as designed, and the thing to change is the veto's threshold or the rung
order — neither of which is this function.

These tests are therefore written to FAIL if the function ever grows an
internal retry, and to state plainly what they do not cover.
"""

from __future__ import annotations

import inspect

import pytest

from omnimarket.delegation import reasoning_preamble
from omnimarket.delegation.reasoning_preamble import (
    EnumReasoningBoundaryRule,
    segment_reasoning_preamble,
)

pytestmark = pytest.mark.unit

#: A response whose scratchpad ends with a trace terminator that has no opener
#: — the exact shape the rule exists to resolve, and the shape observed live.
UNPAIRED_CLOSING = (
    "Here's a thinking process:\n"
    "I should check whether this is verified.\n"
    "</think>\n"
    "The answer is that the lane resolved the class by contract predicate.\n"
)


class TestSegmentationIsPureAndSingleShot:
    def test_the_boundary_rule_resolves_once_and_returns(self) -> None:
        segmentation = segment_reasoning_preamble(UNPAIRED_CLOSING)
        assert (
            segmentation.boundary_rule is EnumReasoningBoundaryRule.UNPAIRED_CLOSING_TAG
        )
        assert "thinking process" not in segmentation.answer

    def test_it_is_deterministic_across_repeated_calls(self) -> None:
        """Same input, same verdict. A stateful veto would drift here."""
        first = segment_reasoning_preamble(UNPAIRED_CLOSING)
        for _ in range(5):
            again = segment_reasoning_preamble(UNPAIRED_CLOSING)
            assert again.answer == first.answer
            assert again.boundary_rule == first.boundary_rule

    def test_no_boundary_returns_the_whole_response_rather_than_retrying(self) -> None:
        """The failure mode is "give up and return", never "try again"."""
        plain = "A plain answer with no scratchpad in front of it."
        segmentation = segment_reasoning_preamble(plain)
        assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
        assert segmentation.answer == plain

    def test_the_source_carries_no_retry_construct(self) -> None:
        """THE FALSIFIER. This fails the day the function grows a retry.

        Asserted against the source of the function itself rather than its
        behaviour, because an internal retry that happens to converge would be
        invisible to every behavioural test above while still being the
        unbounded surface this module exists to rule out.
        """
        source = inspect.getsource(segment_reasoning_preamble)
        for construct in ("while ", "retry", "attempt", "for _ in"):
            assert construct not in source, (
                f"segment_reasoning_preamble now contains {construct!r}; if it "
                "has gained an internal retry it is no longer bounded by the "
                "ladder alone, and the veto-loop reading of OMN-13640 becomes "
                "correct"
            )

    def test_positive_control_the_scan_finds_a_loop_where_one_exists(self) -> None:
        """The scan above is doing work, not passing because it matches nothing.

        Run the identical construct scan against the ladder's own dispatch
        loop, which genuinely loops, retries and counts attempts. It must hit.
        Without this, a typo in the construct list would make the falsifier
        above pass forever and read as proof.
        """
        from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
            LocalDelegationDispatchPort,
        )

        looping = inspect.getsource(LocalDelegationDispatchPort.dispatch)
        hits = [c for c in ("while ", "retry", "attempt") if c in looping]
        assert hits, (
            "the ladder's dispatch loop matched none of the constructs the "
            "falsifier scans for, so the falsifier cannot be trusted"
        )

    def test_the_module_declares_no_retry_budget_of_its_own(self) -> None:
        """A bound here would mean the ladder is not the only bound."""
        names = dir(reasoning_preamble)
        assert not [
            name
            for name in names
            if "RETRY" in name.upper() or "MAX_ATTEMPT" in name.upper()
        ]


class TestWhatThisDoesNotCover:
    """Stated rather than left absent."""

    def test_the_ladder_bound_itself_is_not_asserted_here(self) -> None:
        """``max_escalations`` and per-tier ``max_retries`` are the real bound.

        They live in the task-class contract and ``routing_tiers.yaml`` and are
        exercised by the dispatch-port suite, not here. This module's claim is
        narrower and is the one that was in doubt: that the segmentation rule
        adds NO bound of its own and therefore cannot loop independently of
        that ladder.
        """
        assert True
