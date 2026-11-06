# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A verdict that found no contract is not a DoD sample (OMN-22690, parent OMN-22478).

Recorded on the dev lane on 2026-11-06 about 15:20Z: all 4,630 decided DoD
verdicts joined to delegation runs were status ``failed``, outcome ``refused``,
refusal ``checks_failed``, with one check, the contract lookup, failing with the
error text below; none checked the delegated output. The routing DoD read counted
each as a failed sample, so the router logged local/Qwen3.8-27B n=37 pass=0,
harness/glm-5.3-flash n=43 pass=0 and cheap_frontier/cohere n=9 pass=0 for
summarization. The row shapes here are those verdicts, with run ids replaced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.routing.dod_overlay import (
    build_dod_overlay,
    join_delegations_to_verdicts,
)

TENANT_ID = "820272f9-4aaf-5add-a2df-0af942852ab2"
TASK_TYPE = "summarization"
NOW = datetime(2026, 11, 6, 15, 20, tzinfo=UTC)

#: The verifier's text on a recorded dev verdict (dod_verify_runs.error_message).
RECORDED_CONTRACT_MISSING = (
    "EVIDENCE_CHECK_FAILED: contract: CONTRACT_MISSING: no product repository "
    "carries contracts/OMN-17427.yaml in a merged pull request for OMN-17427; "
    "onex_change_control is not consulted."
)

#: A failure on a check that exercised the attempt.
BEHAVIOUR_CHECK_FAILED = (
    "EVIDENCE_CHECK_FAILED: test_passes: uv run pytest tests/unit/x.py -q exited 1"
)


def _pair(
    index: int,
    *,
    tier_name: str,
    model_name: str,
    status: str,
    outcome: str,
    error_message: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = f"00000000-0000-4000-8000-{index:012d}"
    delegation = {
        "task_type": TASK_TYPE,
        "correlation_id": run_id,
        "tier_name": tier_name,
        "model_name": model_name,
        "created_at": NOW,
    }
    verdict = {
        "verdict_correlation_id": f"10000000-0000-4000-8000-{index:012d}",
        "delegation_correlation_id": run_id,
        "verdict_status": status,
        "verdict_outcome": outcome,
        "verdict_completed_at": NOW,
        "verdict_error_message": error_message,
    }
    return delegation, verdict


def _overlay(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> Any:
    rows = join_delegations_to_verdicts([d for d, _ in pairs], [v for _, v in pairs])
    return build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=5,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=None,
        now=NOW,
    )


def _recorded_contract_missing() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    tiers = (
        [("local", "Qwen3.8-27B")] * 37
        + [("harness", "glm-5.3-flash")] * 43
        + [("cheap_frontier", "cohere/north-mini-code:free")] * 9
    )
    return [
        _pair(
            i,
            tier_name=tier,
            model_name=model,
            status="failed",
            outcome="refused",
            error_message=RECORDED_CONTRACT_MISSING,
        )
        for i, (tier, model) in enumerate(tiers)
    ]


@pytest.mark.unit
def test_contract_missing_verdicts_are_not_samples() -> None:
    overlay = _overlay(_recorded_contract_missing())
    assert overlay.model_signals == ()
    assert overlay.roi_overlay.signals == ()
    assert overlay.joined_verdict_count == 89
    assert overlay.undecided_verdict_count == 89
    assert "no decided DoD verdicts" in overlay.describe()


@pytest.mark.unit
def test_contract_missing_verdicts_suppress_no_tier() -> None:
    overlay = _overlay(_recorded_contract_missing())
    assert overlay.roi_overlay.suppressed_tiers == frozenset()


@pytest.mark.unit
def test_a_failed_behaviour_check_still_counts_as_a_failed_sample() -> None:
    pairs = [
        _pair(
            i,
            tier_name="local",
            model_name="Qwen3.8-27B",
            status="failed",
            outcome="refused",
            error_message=BEHAVIOUR_CHECK_FAILED,
        )
        for i in range(6)
    ] + [
        _pair(
            100 + i,
            tier_name="local",
            model_name="Qwen3.8-27B",
            status="failed",
            outcome="refused",
            error_message=RECORDED_CONTRACT_MISSING,
        )
        for i in range(30)
    ]
    overlay = _overlay(pairs)
    (signal,) = overlay.model_signals
    assert (signal.tier_name, signal.sample_count, signal.pass_count) == ("local", 6, 0)
    assert overlay.roi_overlay.suppressed_tiers == frozenset({"local"})
    assert overlay.undecided_verdict_count == 30


@pytest.mark.unit
def test_a_done_verdict_counts_as_a_pass() -> None:
    pairs = [
        _pair(
            i,
            tier_name="local",
            model_name="Qwen3.8-27B",
            status="verified",
            outcome="done",
            error_message="",
        )
        for i in range(5)
    ]
    overlay = _overlay(pairs)
    (signal,) = overlay.model_signals
    assert (signal.sample_count, signal.pass_count) == (5, 5)
