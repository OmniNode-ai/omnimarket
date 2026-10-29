# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sonnet is the default; Opus needs a declared escalation reason (OMN-21396)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lane_model_policy_compute.handlers import (
    handler_lane_model_policy as policy,
)


def test_opus_without_a_reason_is_refused_with_the_policy_text():
    result = policy.resolve_lane_model(lane_kind="build", required_model="opus")
    assert not result.allowed
    assert result.model is None
    assert "escalation_reason" in result.reason
    assert policy.load_policy().ruling in result.reason


def test_opus_with_a_b4_complexity_reason_is_admitted():
    result = policy.resolve_lane_model(
        lane_kind="build", required_model="opus", escalation_reason="complexity:B4"
    )
    assert result.allowed
    assert result.model == "opus"
    assert result.engine == "claude"
    assert result.ceiling == "opus"


def test_opus_with_a_failed_sonnet_reason_is_admitted():
    result = policy.resolve_lane_model(
        lane_kind="build",
        required_model="claude_opus",
        escalation_reason="sonnet-failed:lane-x",
    )
    assert result.allowed
    assert result.model == "opus"


def test_sonnet_is_admitted_without_a_reason():
    result = policy.resolve_lane_model(lane_kind="build", required_model="sonnet")
    assert result.allowed
    assert result.model == "sonnet"
    assert result.ceiling == "sonnet"


@pytest.mark.parametrize(
    "reason", ["complexity:B3", "complexity:B2", "complexity:", "sonnet-failed:", "x"]
)
def test_a_reason_that_does_not_qualify_admits_no_opus(reason):
    result = policy.resolve_lane_model(
        lane_kind="build", required_model="opus", escalation_reason=reason
    )
    assert not result.allowed
    assert "escalation_reason" in result.reason


def test_a_reason_does_not_lift_a_lane_off_sonnet_by_itself():
    result = policy.resolve_lane_model(
        lane_kind="build", requested_engine="claude", escalation_reason="complexity:B5"
    )
    assert result.model == "sonnet"


def test_a_b4_band_lane_without_a_reason_stays_on_sonnet():
    result = policy.resolve_lane_model(
        lane_kind="build", requested_engine="claude", band="B4"
    )
    assert result.model == "sonnet"


def test_haiku_is_still_refused_even_with_a_reason():
    result = policy.resolve_lane_model(
        lane_kind="build", required_model="haiku", escalation_reason="complexity:B5"
    )
    assert not result.allowed


def test_the_exact_model_check_carries_the_reason():
    assert policy.lane_model_refusal("opus") is not None
    assert (
        policy.lane_model_refusal("claude-opus", escalation_reason="complexity:B4")
        is None
    )
    assert policy.lane_model_refusal("sonnet") is None
