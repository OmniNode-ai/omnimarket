# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reading a host command's output into typed findings and a host outcome (OMN-24198), including broken input."""

from __future__ import annotations

import pytest

from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceCloneStatus,
    EnumConvergenceHostOutcome,
    EnumConvergenceMode,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.parse_host_output import (
    host_outcome,
    parse_findings,
)

pytestmark = pytest.mark.unit

ACT_LINE = "CONVERGED    /reg/omniclaude refused 150s: reattached from a detached HEAD at abc123def456; now on main at 0123456789ab"
OBSERVE_LINE = "WOULD_MOVE    /reg/omnimarket: would fast-forward 3 commit(s) on dev"


def test_the_act_tick_line_and_the_observe_line_both_parse() -> None:
    act = parse_findings(ACT_LINE)
    observed = parse_findings(OBSERVE_LINE)

    assert (act[0].clone, act[0].status) == (
        "/reg/omniclaude",
        EnumConvergenceCloneStatus.CONVERGED,
    )
    assert act[0].reason.startswith("reattached from a detached HEAD")
    assert (observed[0].clone, observed[0].status) == (
        "/reg/omnimarket",
        EnumConvergenceCloneStatus.WOULD_MOVE,
    )


def test_the_alarm_suffix_is_not_part_of_the_reason() -> None:
    (finding,) = parse_findings(
        "REFUSED       /reg/omnidash refused 400s: holds local work: modified 1 (a.ts) [alarm: delivered]"
    )

    assert finding.reason == "holds local work: modified 1 (a.ts)"


def test_engine_lines_and_noise_are_not_findings() -> None:
    noise = "\n".join(
        [
            "REFUSED_TOO_LONG /reg/omnidash refused for 400s: tracked changes",
            "UP_TO_DATE /reg/omniclaude origin/main 1->1",
            "",
            "Traceback (most recent call last):",
            "UNKNOWN_STATUS /reg/x: whatever",
        ]
    )

    assert parse_findings(noise) == ()


def test_an_act_run_that_names_no_clone_and_exits_zero_is_clean() -> None:
    outcome, why = host_outcome(EnumConvergenceMode.ACT, 0, ())

    assert outcome is EnumConvergenceHostOutcome.CLEAN
    assert why == ""


def test_an_observe_run_that_names_no_clone_is_failed_not_clean() -> None:
    outcome, why = host_outcome(EnumConvergenceMode.OBSERVE, 0, ())

    assert outcome is EnumConvergenceHostOutcome.FAILED
    assert "named no clone" in why


def test_broken_input_garbage_output_with_exit_one_is_failed() -> None:
    outcome, why = host_outcome(
        EnumConvergenceMode.ACT, 1, parse_findings("\x00\x01 not a clone line at all")
    )

    assert outcome is EnumConvergenceHostOutcome.FAILED
    assert "exited 1" in why


def test_a_refusal_outranks_a_nonzero_exit_so_the_clone_is_named() -> None:
    findings = parse_findings(
        "REFUSED       /reg/omnidash refused 400s: holds local work: staged 1 (a.ts) [alarm: failed: no channel]"
    )

    outcome, _ = host_outcome(EnumConvergenceMode.ACT, 1, findings)

    assert outcome is EnumConvergenceHostOutcome.REFUSED


def test_a_failed_clone_line_fails_the_host() -> None:
    findings = parse_findings(
        "FAILED        /reg/omnimemory: git status: not a git repository"
    )

    outcome, _ = host_outcome(EnumConvergenceMode.OBSERVE, 1, findings)

    assert outcome is EnumConvergenceHostOutcome.FAILED
