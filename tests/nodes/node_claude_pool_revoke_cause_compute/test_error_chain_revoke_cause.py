# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: blank markers and unknown fields are refused by validation."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_pool_revoke_cause_compute.handlers.handler_revoke_cause import (
    HandlerRevokeCause,
)
from omnimarket.nodes.node_claude_pool_revoke_cause_compute.models import (
    ModelRevokeCauseRequest,
)

pytestmark = pytest.mark.unit

REQUEST: dict[str, Any] = {
    "label": "alpha",
    "evidence": "engine closed after http 403: attention required by cloudflare",
    "challenge_markers": ["attention required by cloudflare"],
}


def _refused(request: dict[str, Any], *fragments: str) -> None:
    with pytest.raises(ValidationError) as raised:
        ModelRevokeCauseRequest.model_validate(request)
    message = str(raised.value)
    for fragment in fragments:
        assert fragment in message


def test_an_empty_marker_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["challenge_markers"] = [""]
    _refused(request, "challenge_markers", "blank")


def test_a_whitespace_only_marker_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["challenge_markers"] = ["attention required", "   "]
    _refused(request, "challenge_markers", "blank")


def test_extra_unknown_field_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["unexpected"] = 1
    _refused(request, "unexpected")


def test_an_empty_label_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["label"] = ""
    _refused(request, "label")


def test_a_challenge_page_with_a_declared_marker_is_challenged() -> None:
    result = HandlerRevokeCause().handle(
        ModelRevokeCauseRequest.model_validate(REQUEST)
    )
    assert result.cause == "challenged"
    assert result.label == "alpha"
