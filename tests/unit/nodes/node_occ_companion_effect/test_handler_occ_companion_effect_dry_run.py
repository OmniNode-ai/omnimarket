# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused handler test: dry_run does the full read -> compute but ZERO mutation.

Injects a stub RSD-2 read-EFFECT so the handler runs offline (no GitHub). Proves
the orchestration wiring (read -> compute -> plan summary) without any git/gh side
effect, and that ``mode="dry_run"`` (the default) never reaches the write path.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)


class _StubStateHandler(HandlerOccStateEffect):
    """RSD-2 read stub — returns a canned pass-1 request, performs no I/O."""

    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._request


def _canned_request() -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1760,
        pr_head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        pr_title="feat(OMN-14608): thing",
        pr_body="Closes OMN-14608",
        run_timestamp="2026-07-14T12:00:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1760",
            stdout='{"number":1760,"state":"OPEN"}',
            exit_code=0,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_reads_and_computes_but_does_not_mutate() -> None:
    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    result = await handler.handle(
        uuid4(),
        ModelOccCompanionEffectRequest(repo="OmniNode-ai/omnimarket", pr_number=1760),
    )

    assert result.mode == "dry_run"
    assert not result.no_op
    assert not result.fast_path
    assert result.tickets == ("OMN-14608",)
    # Pass 1 (OCC PR unknown in dry_run): contract + downstream receipt, no PR opened.
    assert result.occ_pr_number is None
    assert not result.product_body_stamped
    assert result.deterministic_digest
    assert any(p.startswith("contracts/") for p in result.companion_paths)
    assert any("dod_receipts" in p for p in result.companion_paths)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_on_already_bound_pr_is_no_op() -> None:
    bound = _canned_request().model_copy(
        update={"pr_body": "Closes OMN-14608\nEvidence-Source: OCC#4242"}
    )
    handler = HandlerOccCompanionEffect(state_handler=_StubStateHandler(bound))
    result = await handler.handle(
        uuid4(),
        ModelOccCompanionEffectRequest(repo="OmniNode-ai/omnimarket", pr_number=1760),
    )
    assert result.no_op
    assert "already bound" in result.no_op_reason
