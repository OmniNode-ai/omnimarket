# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: the decision rule, its failing controls and order independence."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.handlers.handler_revoke_staleness import (
    HandlerRevokeStaleness,
)
from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.models import (
    ModelRevokeObservation,
    ModelRevokeStalenessPolicy,
    ModelRevokeStalenessRequest,
)

pytestmark = pytest.mark.unit

FINGERPRINT = "4d2430e0"
OTHER_FINGERPRINT = "9f000000"


def _obs(
    host: str, kind: str, at: str, fingerprint: str = FINGERPRINT
) -> ModelRevokeObservation:
    return ModelRevokeObservation(host=host, kind=kind, at=at, fingerprint=fingerprint)


def _request(
    revoked_at: str | None,
    observations: tuple[ModelRevokeObservation, ...],
    margin_seconds: int = 60,
) -> ModelRevokeStalenessRequest:
    return ModelRevokeStalenessRequest(
        label="alpha",
        fingerprint=FINGERPRINT,
        revoked_at=revoked_at,
        observations=observations,
        policy=ModelRevokeStalenessPolicy(margin_seconds=margin_seconds),
    )


def test_observation_predating_the_revoke_is_real() -> None:
    result = HandlerRevokeStaleness().handle(
        _request(
            "2026-11-02T07:25:20Z",
            (
                _obs(
                    "h053",
                    "limited",
                    "2026-11-01T00:00:00Z",
                ),
            ),
        )
    )
    assert result.verdict == "real"
    assert result.clear is None


def test_observation_inside_the_margin_is_real() -> None:
    # at == revoked_at + margin (07:25:20 + 60s = 07:26:20) is not strictly after.
    result = HandlerRevokeStaleness().handle(
        _request(
            "2026-11-02T07:25:20Z",
            (
                _obs(
                    "h053",
                    "limited",
                    "2026-11-02T07:26:20Z",
                ),
            ),
        )
    )
    assert result.verdict == "real"
    assert result.clear is None


def test_observation_with_a_different_fingerprint_is_real() -> None:
    result = HandlerRevokeStaleness().handle(
        _request(
            "2026-11-02T07:25:20Z",
            (_obs("h053", "limited", "2026-11-09T14:07:44Z", OTHER_FINGERPRINT),),
        )
    )
    assert result.verdict == "real"
    assert result.clear is None


def test_no_observations_is_real() -> None:
    result = HandlerRevokeStaleness().handle(_request("2026-11-02T07:25:20Z", ()))
    assert result.verdict == "real"
    assert result.clear is None


def test_missing_stamp_is_unknown() -> None:
    result = HandlerRevokeStaleness().handle(
        _request(
            None,
            (
                _obs(
                    "h053",
                    "limited",
                    "2026-11-09T14:07:44Z",
                ),
            ),
        )
    )
    assert result.verdict == "unknown"
    assert result.clear is None


def test_order_independence_of_observations() -> None:
    obs_a = _obs("h053", "limited", "2026-11-09T14:07:44Z")
    obs_b = _obs("h054", "ok", "2026-11-09T14:07:45Z")
    forward = HandlerRevokeStaleness().handle(
        _request("2026-11-02T07:25:20Z", (obs_a, obs_b))
    )
    reversed_ = HandlerRevokeStaleness().handle(
        _request("2026-11-02T07:25:20Z", (obs_b, obs_a))
    )
    assert forward == reversed_
    assert forward.verdict == "stale"
    assert forward.clear is not None
    assert forward.clear.evidence_host == "h053"
