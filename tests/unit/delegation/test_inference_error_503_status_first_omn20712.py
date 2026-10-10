# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A 503 the provider answered is read as a status before any generic marker.

``_inference_error_failure_class`` matched ``"401" in text`` before ``"503"``. A
503 whose URL carried the digits 401 (the loopback port 44011 in CI) was
therefore classified ``provider_auth_failed`` and its terminal read
``inference_failed`` instead of ``provider_unavailable``.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _inference_error_failure_class,
)


@pytest.mark.parametrize(
    "message",
    [
        # The CI failure: the port 44011 carries "401".
        "provider HTTP 503 Service Unavailable for "
        "http://127.0.0.1:44011/v1/chat/completions?onex_cid="
        "9b916544-1cfe-4ff9-8142-4ac5388f9848; "
        'response_body={"error": {"code": 503, "message": "HTTP 503 unavailable"}}',
        # The digits inside the correlation id instead.
        "provider HTTP 503 Service Unavailable for "
        "http://127.0.0.1:8000/v1/chat/completions?onex_cid="
        "0a401b2c-0000-4000-8000-000000000000",
    ],
)
def test_a_503_carrying_401_in_its_url_is_model_unavailable(message: str) -> None:
    assert _inference_error_failure_class(message) is Failure.MODEL_UNAVAILABLE


def test_a_real_401_is_still_an_auth_failure() -> None:
    """The control: an answered 401 keeps its class."""
    message = (
        "provider HTTP 401 Unauthorized for http://127.0.0.1:44011/v1/chat/completions"
    )
    assert _inference_error_failure_class(message) is Failure.PROVIDER_AUTH_FAILED
