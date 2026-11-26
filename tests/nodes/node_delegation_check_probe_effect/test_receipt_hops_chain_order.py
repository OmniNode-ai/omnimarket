# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation check asserts the chain order from the hops the receipt records, never from a pinned model (OMN-26043)."""

from __future__ import annotations

from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_hop_chain_order,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    ModelDelegateObservation,
    ModelDelegationHop,
)


def _hop(tier: str, **over: str | None) -> ModelDelegationHop:
    fields: dict[str, str | None] = {
        "tier": tier,
        "backend": f"{tier}-backend",
        "model": f"{tier}-model",
        "outcome": "reject",
    }
    return ModelDelegationHop(**{**fields, **over})  # type: ignore[arg-type]


def _obs(*hops: ModelDelegationHop) -> ModelDelegateObservation:
    return ModelDelegateObservation(rc=0, attempt_hops=hops)


def test_local_then_openrouter_then_claude_reads_green() -> None:
    clause = grade_hop_chain_order(
        _obs(_hop("local"), _hop("openrouter"), _hop("claude_api", outcome="accept"))
    )
    assert clause.ok, clause.detail


def test_any_model_passes_when_the_order_holds() -> None:
    assert grade_hop_chain_order(_obs(_hop("local", model="another-model"))).ok


def test_claude_before_openrouter_reads_red() -> None:
    assert not grade_hop_chain_order(_obs(_hop("claude_api"), _hop("openrouter"))).ok


def test_a_local_hop_after_a_cloud_hop_reads_red() -> None:
    assert not grade_hop_chain_order(_obs(_hop("openrouter"), _hop("local"))).ok


def test_a_receipt_with_no_hop_reads_red() -> None:
    assert not grade_hop_chain_order(_obs()).ok


def test_a_hop_missing_backend_model_or_outcome_reads_red() -> None:
    for missing in ("backend", "model", "outcome"):
        assert not grade_hop_chain_order(_obs(_hop("local", **{missing: None}))).ok


def test_a_tier_outside_the_chain_reads_red() -> None:
    assert not grade_hop_chain_order(_obs(_hop("cheap_cloud"))).ok
