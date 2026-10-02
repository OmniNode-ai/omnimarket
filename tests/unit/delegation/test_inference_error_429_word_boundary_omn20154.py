# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A 429 is read as a status, not as three digits anywhere in the error text (OMN-20154).

``_inference_error_failure_class`` fell back to ``"429" in text`` after the
``provider HTTP 429`` read. Any other failure whose text happened to carry those
three digits (a request id, a port, a byte count) was therefore classified
``rate_limited``, charged to the provider as a quota refusal, and the real cause
(an overloaded or unavailable provider) was lost.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _inference_error_failure_class,
)

# A z.ai 503 whose request id carries the digits 429 inside a longer token.
_UNAVAILABLE_WITH_429_IN_AN_ID = (
    "provider HTTP 503 Service Unavailable for "
    "https://api.z.ai/api/coding/paas/v4/chat/completions; "
    'response_body={"error":{"code":"1305","message":"The service may be '
    'temporarily unavailable","request_id":"202609301342429817"}}'
)

# The same failure with the digits inside a hex request id.
_UNAVAILABLE_WITH_429_IN_A_HEX_ID = (
    "connection reset by peer while calling the provider "
    "(request 7f429a1c3b, 4290 bytes sent)"
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_UNAVAILABLE_WITH_429_IN_AN_ID, "model_unavailable"),
        (_UNAVAILABLE_WITH_429_IN_A_HEX_ID, "model_unavailable"),
        ("upstream returned 14290 tokens of garbage", "unknown"),
    ],
)
def test_digits_that_merely_contain_429_are_not_a_rate_limit(
    message: str, expected: str
) -> None:
    assert _inference_error_failure_class(message).value == expected


@pytest.mark.parametrize(
    "message",
    [
        "provider HTTP 429 Too Many Requests for https://openrouter.ai/api/v1; retry_after=17;",
        "Client error '429 Too Many Requests' for url 'https://x.example/v1'",
        "upstream answered 429.",
        'response_body={"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}',
        'response_body={"error":{"code":"429","message":"slow down"}}',
        "rate limit exceeded",
    ],
)
def test_a_real_429_is_still_a_rate_limit(message: str) -> None:
    """The control: the boundary must not lose the shapes that are real."""
    assert _inference_error_failure_class(message) is Failure.RATE_LIMITED
