# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused handler test: dry_run renders path+content but performs ZERO mutation
and never requires a resolvable GITHUB_TOKEN (OMN-14888)."""

from __future__ import annotations

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import occ_observation_record_relpath
from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)


def _record() -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1841,
        head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        policy_version="v1",
        workflow_run_id=100,
        run_attempt=1,
        recorded_at="2026-07-21T00:00:00Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1841,
            occ_pr_number=4500,
            minted_by_node=True,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at="2026-07-21T00:00:00Z",
            reason="",
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_renders_path_and_performs_no_mutation() -> None:
    record = _record()
    request = ModelOccObservationEffectRequest(record=record, mode="dry_run")

    result = await HandlerOccObservationEffect().handle(request)

    assert result.mode == "dry_run"
    assert result.relpath == occ_observation_record_relpath(record)
    assert result.occ_pr_number is None
    assert result.occ_pr_url == ""
    assert "no GitHub mutation" in result.action


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_never_calls_resolve_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> str:
        raise AssertionError("dry_run must never resolve a GitHub token")

    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect._resolve_github_token",
        _boom,
    )
    request = ModelOccObservationEffectRequest(record=_record(), mode="dry_run")

    result = await HandlerOccObservationEffect().handle(request)

    assert result.mode == "dry_run"
