# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Fable review gate (OMN-24533): operator ruling 2026-11-14T23:50:35Z, every Fable diff review runs only on the
orchestrating Mac. The gate admits a Fable-class review from an orchestrating host and refuses any other origin with a typed
reason; a review with no Fable-class route is not the gate's to decide."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_fable_review_gate_compute import HandlerFableReviewGate
from omnimarket.nodes.node_fable_review_gate_compute import overlay as gate_overlay
from omnimarket.nodes.node_fable_review_gate_compute.models import (
    EnumFableReviewRefusal,
    ModelFableReviewGateOverlay,
    ModelFableReviewGateRequest,
    ModelFableReviewGateVerdict,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 1, 0, 0, tzinfo=UTC)
CID = UUID("11111111-2222-3333-4444-555555555555")
EXAMPLE = Path(gate_overlay.__file__).with_name("overlay.example.yaml")


def _overlay(**over: object) -> ModelFableReviewGateOverlay:
    base: dict[str, object] = {
        "fable_route_keys": ("synthetic-fable",),
        "orchestrating_hosts": ("synthetic-mac",),
        "host_name_env": "SYNTHETIC_HOST_NAME",
    }
    return ModelFableReviewGateOverlay.model_validate({**base, **over})


def _request(
    route_keys: tuple[str, ...], origin_host: str
) -> ModelFableReviewGateRequest:
    return ModelFableReviewGateRequest(
        correlation_id=CID,
        route_keys=route_keys,
        origin_host=origin_host,
        decided_at=NOW,
    )


def _gate() -> HandlerFableReviewGate:
    return HandlerFableReviewGate(overlay=_overlay())


def test_non_fable_route_is_admitted_from_any_origin() -> None:
    for origin in ("synthetic-mac", "synthetic-lab", ""):
        verdict = _gate().handle(_request(("synthetic-other",), origin))
        assert verdict.admitted is True
        assert verdict.applies is False
        assert verdict.refusal is None
        assert verdict.receipt.matched_orchestrating_host is None
        assert verdict.receipt.fable_route_keys == ()


def test_fable_route_from_orchestrating_host_is_admitted_with_receipt() -> None:
    verdict = _gate().handle(_request(("synthetic-fable",), "  Synthetic-MAC "))
    assert verdict.admitted is True
    assert verdict.applies is True
    assert verdict.refusal is None
    receipt = verdict.receipt
    assert receipt.matched_orchestrating_host == "synthetic-mac"
    assert receipt.correlation_id == CID
    assert receipt.origin_host == "  Synthetic-MAC "
    assert receipt.decided_at == NOW
    assert receipt.fable_route_keys == ("synthetic-fable",)


def test_fable_route_from_another_host_is_refused() -> None:
    verdict = _gate().handle(_request(("synthetic-fable",), "synthetic-lab"))
    assert verdict.admitted is False
    assert verdict.applies is True
    assert verdict.refusal is EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC
    assert verdict.receipt.matched_orchestrating_host is None
    assert verdict.receipt.origin_host == "synthetic-lab"


@pytest.mark.parametrize("origin", ["", "   "])
def test_fable_route_with_unreadable_origin_is_refused(origin: str) -> None:
    verdict = _gate().handle(_request(("synthetic-fable",), origin))
    assert verdict.admitted is False
    assert verdict.refusal is EnumFableReviewRefusal.ORIGIN_UNREADABLE
    assert verdict.receipt.matched_orchestrating_host is None


def test_mixed_routes_from_a_lab_host_are_refused_and_name_only_fable_routes() -> None:
    verdict = _gate().handle(
        _request(("synthetic-other", "synthetic-fable"), "synthetic-lab")
    )
    assert verdict.admitted is False
    assert verdict.refusal is EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC
    assert verdict.receipt.fable_route_keys == ("synthetic-fable",)


def test_route_key_match_ignores_case() -> None:
    verdict = _gate().handle(_request(("SYNTHETIC-Fable",), "synthetic-lab"))
    assert verdict.applies is True
    assert verdict.admitted is False
    assert verdict.receipt.fable_route_keys == ("SYNTHETIC-Fable",)


def test_same_request_twice_gives_equal_verdicts() -> None:
    gate = _gate()
    request = _request(("synthetic-fable",), "synthetic-lab")
    assert gate.handle(request) == gate.handle(request)


@pytest.mark.parametrize(
    "bad",
    [
        {"fable_route_keys": ()},
        {"orchestrating_hosts": ()},
        {"fable_route_keys": (" ",)},
        {"orchestrating_hosts": ("a", " A ")},
        {"fable_route_keys": ("k", "K")},
        {"host_name_env": ""},
        {"unknown_key": "x"},
    ],
)
def test_overlay_refuses_a_bad_shape(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _overlay(**bad)


def test_overlay_refuses_an_absent_key() -> None:
    with pytest.raises(ValidationError):
        ModelFableReviewGateOverlay.model_validate(
            {"fable_route_keys": ("k",), "orchestrating_hosts": ("h",)}
        )


def test_packaged_example_overlay_loads() -> None:
    overlay = ModelFableReviewGateOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text())
    )
    assert overlay.host_name_env == "EXAMPLE_HOST_NAME"


def test_read_origin_host_reads_the_named_variable_and_strips_it() -> None:
    overlay = _overlay()
    assert (
        gate_overlay.read_origin_host(
            overlay, {"SYNTHETIC_HOST_NAME": " synthetic-mac "}
        )
        == "synthetic-mac"
    )
    assert gate_overlay.read_origin_host(overlay, {"OTHER": "x"}) == ""


def test_request_refuses_a_naive_time() -> None:
    with pytest.raises(ValidationError):
        ModelFableReviewGateRequest(
            correlation_id=CID,
            route_keys=("k",),
            origin_host="h",
            decided_at=datetime(2026, 11, 15, 1, 0, 0),
        )


def test_verdict_refuses_an_inconsistent_refusal() -> None:
    admitted = _gate().handle(_request(("synthetic-fable",), "synthetic-mac"))
    refused = _gate().handle(_request(("synthetic-fable",), "synthetic-lab"))
    with pytest.raises(ValidationError):
        ModelFableReviewGateVerdict(
            admitted=True,
            applies=True,
            refusal=EnumFableReviewRefusal.ORIGIN_UNREADABLE,
            receipt=admitted.receipt,
        )
    with pytest.raises(ValidationError):
        ModelFableReviewGateVerdict(
            admitted=False, applies=True, refusal=None, receipt=refused.receipt
        )
