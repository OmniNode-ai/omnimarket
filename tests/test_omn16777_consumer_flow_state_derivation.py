# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16777 — the verdict table, including the half that must NOT fire.

Four failures on 2026-08-23 were invisible because every signal in the platform
was binary and every one of the four consumers was up. The derivation under test
is the smallest thing that would have caught them, and it has to be right in
BOTH directions: a rule that calls everything STALLED is worth exactly as much
as one that calls nothing STALLED.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    derive_flow_state,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
    EnumUpstreamEvidence,
)


@pytest.mark.unit
def test_consumed_everything_produced_nothing_is_stalled() -> None:
    """AC2, the canonical acceptance case.

    ``node_gateway_link_health_projection_compute`` on the .201 dev lane:
    Stable, members > 0, LAG 0, current-offset 15,750 — it had read every
    heartbeat ever produced — while its declared output topic sat at
    LOG-END-OFFSET 0. Every check was green. The verdict must be STALLED.
    """
    state, _evidence = derive_flow_state(
        messages_in=15750, messages_out=0, upstream_produced=None
    )
    assert state is EnumConsumerFlowState.STALLED


@pytest.mark.unit
def test_stalled_verdict_survives_a_silent_upstream() -> None:
    """A stalled consumer must not be rescued into green by a quiet upstream.

    Once messages_in > 0, upstream evidence is irrelevant: the messages were
    taken and nothing came out. Consulting upstream here would let the exact
    OMN-16755 consumer report IDLE on a window where its own source happened to
    go quiet.
    """
    for upstream in (None, 0, 5):
        state, _ = derive_flow_state(
            messages_in=15750, messages_out=0, upstream_produced=upstream
        )
        assert state is EnumConsumerFlowState.STALLED, (
            f"upstream_produced={upstream!r} changed a STALLED verdict"
        )


@pytest.mark.unit
def test_in_and_out_is_flowing() -> None:
    state, _ = derive_flow_state(
        messages_in=5575, messages_out=5575, upstream_produced=5575
    )
    assert state is EnumConsumerFlowState.FLOWING


@pytest.mark.unit
def test_zero_in_while_upstream_produced_is_starved() -> None:
    """Messages existed and this consumer took none of them."""
    state, evidence = derive_flow_state(
        messages_in=0, messages_out=0, upstream_produced=42
    )
    assert state is EnumConsumerFlowState.STARVED
    assert evidence is EnumUpstreamEvidence.PRODUCED


@pytest.mark.unit
def test_quiet_consumer_on_a_provably_silent_topic_is_idle() -> None:
    """AC4 — the false-positive half, and it is not optional.

    Nothing was produced upstream and nothing was consumed. That is a correctly
    quiet consumer. Reporting it as STALLED would light up every quiet topic in
    the platform, and an alert that fires on everything gets muted within a day
    — which is how OMN-14440 ran for three months.
    """
    state, evidence = derive_flow_state(
        messages_in=0, messages_out=0, upstream_produced=0
    )
    assert state is EnumConsumerFlowState.IDLE
    assert evidence is EnumUpstreamEvidence.SILENT


@pytest.mark.unit
def test_no_upstream_evidence_reports_idle_and_says_so() -> None:
    """An externally-fed topic is invisible on this rail, and the row admits it.

    Nothing in this runtime publishes to an MSK ingress leg, so there is no
    honest basis to call a quiet consumer on it STARVED. The verdict is IDLE and
    the evidence column records NONE, so an operator can see the difference
    between "provably silent" and "we cannot see this topic's producers"
    instead of being handed a confident guess.
    """
    state, evidence = derive_flow_state(
        messages_in=0, messages_out=0, upstream_produced=None
    )
    assert state is EnumConsumerFlowState.IDLE
    assert evidence is EnumUpstreamEvidence.NONE


@pytest.mark.unit
def test_derivation_is_pure_and_total_over_the_verdict_space() -> None:
    """Every (in, out, upstream) combination lands on exactly one verdict, and
    the same inputs always land on the same one (AC6's determinism half)."""
    seen: dict[tuple[int, int, int | None], object] = {}
    for messages_in in (0, 1, 15750):
        for messages_out in (0, 1, 5575):
            for upstream in (None, 0, 7):
                key = (messages_in, messages_out, upstream)
                state, evidence = derive_flow_state(
                    messages_in=messages_in,
                    messages_out=messages_out,
                    upstream_produced=upstream,
                )
                assert isinstance(state, EnumConsumerFlowState)
                assert isinstance(evidence, EnumUpstreamEvidence)
                # UNKNOWN is never a DERIVED verdict — it is the absence of an
                # observation, materialized only for a missing window.
                assert state is not EnumConsumerFlowState.UNKNOWN
                seen[key] = (state, evidence)
                assert (
                    derive_flow_state(
                        messages_in=messages_in,
                        messages_out=messages_out,
                        upstream_produced=upstream,
                    )
                    == seen[key]
                )
    assert len(seen) == 27
