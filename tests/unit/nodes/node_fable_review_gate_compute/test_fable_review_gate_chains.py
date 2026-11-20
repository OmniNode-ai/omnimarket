# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the Fable review gate (OMN-24533), with the failing controls.

The chain is the one a deployment runs: the overlay names the host-identity variable, the variable names this machine, the
request carries that host, the gate decides. The golden chain reads the orchestrating Mac and is admitted with a receipt that
names it; the error chain reads a lab host and is refused with the typed reason. The controls are the same checks run against
a gate with an injected break: each reads red there and green on the real gate, so the red is the break's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.events.fable_review_gate import (
    EnumFableReviewRefusal,
    ModelFableReviewGateOverlay,
    ModelFableReviewGateRequest,
    ModelFableReviewGateVerdict,
)
from omnimarket.nodes.node_fable_review_gate_compute import HandlerFableReviewGate
from omnimarket.nodes.node_fable_review_gate_compute.overlay import read_origin_host

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 1, 0, 0, tzinfo=UTC)
CID = UUID("11111111-2222-3333-4444-555555555555")
OVERLAY = ModelFableReviewGateOverlay(
    fable_route_keys=("synthetic-fable",),
    orchestrating_hosts=("synthetic-mac",),
    host_name_env="SYNTHETIC_HOST_NAME",
)


def _chain(
    gate: HandlerFableReviewGate, environ: dict[str, str]
) -> ModelFableReviewGateVerdict:
    return gate.handle(
        ModelFableReviewGateRequest(
            correlation_id=CID,
            route_keys=("synthetic-fable",),
            origin_host=read_origin_host(OVERLAY, environ),
            decided_at=NOW,
        )
    )


class _AdmitEverywhereGate(HandlerFableReviewGate):
    """Injected break: the origin check is gone, every request is admitted."""

    def handle(
        self, request: ModelFableReviewGateRequest
    ) -> ModelFableReviewGateVerdict:
        real = super().handle(request)
        return real.model_copy(update={"admitted": True, "refusal": None})


class _IgnoreEmptyOriginGate(HandlerFableReviewGate):
    """Injected break: an unreadable origin is read as the orchestrating host."""

    def handle(
        self, request: ModelFableReviewGateRequest
    ) -> ModelFableReviewGateVerdict:
        if not request.origin_host.strip():
            request = request.model_copy(update={"origin_host": "synthetic-mac"})
        return super().handle(request)


def _refuses_lab_origin(gate: HandlerFableReviewGate) -> None:
    verdict = _chain(gate, {"SYNTHETIC_HOST_NAME": "synthetic-lab"})
    assert verdict.admitted is False
    assert verdict.refusal is EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC


def _refuses_unreadable_origin(gate: HandlerFableReviewGate) -> None:
    verdict = _chain(gate, {})
    assert verdict.admitted is False
    assert verdict.refusal is EnumFableReviewRefusal.ORIGIN_UNREADABLE


def test_golden_chain_orchestrating_mac_is_admitted_with_its_receipt() -> None:
    verdict = _chain(
        HandlerFableReviewGate(overlay=OVERLAY),
        {"SYNTHETIC_HOST_NAME": "synthetic-mac"},
    )
    assert verdict.admitted is True
    assert verdict.applies is True
    assert verdict.refusal is None
    assert verdict.receipt.origin_host == "synthetic-mac"
    assert verdict.receipt.matched_orchestrating_host == "synthetic-mac"
    assert verdict.receipt.decided_at == NOW


def test_error_chain_lab_host_is_refused_with_the_typed_reason() -> None:
    verdict = _chain(
        HandlerFableReviewGate(overlay=OVERLAY),
        {"SYNTHETIC_HOST_NAME": "synthetic-lab"},
    )
    assert verdict.admitted is False
    assert verdict.refusal is EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC
    assert verdict.receipt.origin_host == "synthetic-lab"
    assert verdict.receipt.matched_orchestrating_host is None


def test_error_chain_unreadable_host_is_refused_with_the_typed_reason() -> None:
    _refuses_unreadable_origin(HandlerFableReviewGate(overlay=OVERLAY))


def test_control_admit_everywhere_gate_reads_broken_on_a_lab_origin() -> None:
    _refuses_lab_origin(HandlerFableReviewGate(overlay=OVERLAY))
    with pytest.raises(AssertionError):
        _refuses_lab_origin(_AdmitEverywhereGate(overlay=OVERLAY))


def test_control_ignore_empty_origin_gate_reads_broken_on_an_unreadable_origin() -> (
    None
):
    _refuses_unreadable_origin(HandlerFableReviewGate(overlay=OVERLAY))
    with pytest.raises(AssertionError):
        _refuses_unreadable_origin(_IgnoreEmptyOriginGate(overlay=OVERLAY))
