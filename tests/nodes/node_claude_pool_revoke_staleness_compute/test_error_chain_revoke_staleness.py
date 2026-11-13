# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: invalid inputs are refused by validation; the handler never sees them."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.handlers.handler_revoke_staleness import (
    HandlerRevokeStaleness,
)
from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.models import (
    ModelRevokeStalenessRequest,
    ModelRevokeStalenessResult,
)

pytestmark = pytest.mark.unit

REQUEST: dict[str, Any] = {
    "label": "alpha",
    "fingerprint": "4d2430e0",
    "revoked_at": "2026-11-02T07:25:20Z",
    "observations": [
        {
            "host": "h053",
            "kind": "limited",
            "at": "2026-11-09T14:07:44Z",
            "fingerprint": "4d2430e0",
        }
    ],
    "policy": {"margin_seconds": 60},
}


def _refused(request: dict[str, Any], *fragments: str) -> None:
    """The request never validates, so the handler is never handed it."""
    handler = HandlerRevokeStaleness()
    with pytest.raises(ValidationError) as raised:
        ModelRevokeStalenessRequest.model_validate(request)
    assert callable(handler.handle)
    message = str(raised.value)
    for fragment in fragments:
        assert fragment in message


def test_naive_revoked_at_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["revoked_at"] = "2026-11-02T07:25:20"
    _refused(request, "revoked_at", "timezone")


def test_naive_observation_at_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["observations"][0]["at"] = "2026-11-09T14:07:44"
    _refused(request, "at", "timezone")


def test_negative_margin_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["policy"]["margin_seconds"] = -1
    _refused(request, "margin_seconds")


def test_extra_unknown_field_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["unexpected"] = 1
    _refused(request, "unexpected")


def test_empty_label_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["label"] = ""
    _refused(request, "label")


def test_stale_verdict_without_clear_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRevokeStalenessResult(
            label="alpha",
            verdict="stale",
            reason="x",
            clear=None,
        )


def test_policy_none_is_not_configured_with_clear_none() -> None:
    request = ModelRevokeStalenessRequest(
        label="alpha",
        fingerprint="4d2430e0",
        revoked_at=None,
        observations=(),
        policy=None,
    )
    result = HandlerRevokeStaleness().handle(request)
    assert result.verdict == "not_configured"
    assert result.clear is None
