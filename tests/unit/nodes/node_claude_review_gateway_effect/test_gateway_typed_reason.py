# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review gateway's 503 names why it refused, in a field a reader can check.

When every key behind the gateway is billed out the reply carries
``reason: credit_exhausted``; a non-key upstream failure carries
``reason: gateway_upstream_failed``. No test reaches a provider: the key is a
provider-free double reported billed out.

The rendered voter error is the shape the voter's transport gives a 503 reply:
``[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error (503) from <target>`` followed by
`` :: response_body=<reply body>``. The anchored pattern below is spelled here, in this
repository, because the classifier lives in another repository's package and a test
that needs that package installed would be skipped where it is absent; the pattern is
the first entry of ``_UNAVAILABLE_ERRORS`` in that package's substitute handler.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    EnumReviewGatewayReason,
)

from .test_claude_review_gateway import (
    _PAYLOAD,
    _fail,
    _gateway,
    _ok,
    _row,
)

pytestmark = pytest.mark.unit

_TARGET = "claude-review-gateway"
_SUBSTITUTE_UNAVAILABLE = re.compile(
    r"\[ONEX_CORE_205_SERVICE_UNAVAILABLE\] Server error \((?:502|503|504)\) from "
)


def _rendered_voter_error(status: int, body: dict[str, Any]) -> str:
    """The error text the voter's transport builds from a gateway reply."""
    snippet = json.dumps(body, separators=(",", ":"))
    return (
        f"[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error ({status}) from {_TARGET}"
        f" :: response_body={snippet}"
    )


def _substitutable_failure(error: str) -> str | None:
    """The classification the substitute handler gives a failed voter's error."""
    return "was unavailable" if _SUBSTITUTE_UNAVAILABLE.match(error) else None


def _exhausted() -> tuple[int, dict[str, Any]]:
    gateway, _values, _call = _gateway(
        [_row("key-a")],
        [_fail(EnumDelegationFailureClass.PROVIDER_BILLING, "PROVIDER_BILLING")],
    )
    return asyncio.run(gateway.complete(_PAYLOAD))


def test_reason_values_are_exactly_the_two_declared_strings() -> None:
    assert EnumReviewGatewayReason.CREDIT_EXHAUSTED.value == "credit_exhausted"
    assert (
        EnumReviewGatewayReason.GATEWAY_UPSTREAM_FAILED.value
        == "gateway_upstream_failed"
    )


def test_every_key_billed_out_answers_503_with_reason_credit_exhausted() -> None:
    status, reply = _exhausted()
    assert status == 503
    assert reply["error"]["type"] == "gateway_keys_set_aside"
    assert reply["error"]["reason"] == "credit_exhausted"


def test_a_key_the_gateway_set_aside_earlier_still_reads_credit_exhausted() -> None:
    gateway, _values, _call = _gateway(
        [_row("key-a")],
        [_fail(EnumDelegationFailureClass.PROVIDER_BILLING, "PROVIDER_BILLING")],
    )
    asyncio.run(gateway.complete(_PAYLOAD))
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 503
    assert reply["error"]["reason"] == "credit_exhausted"


def test_a_non_key_upstream_failure_keeps_reason_gateway_upstream_failed() -> None:
    gateway, _values, _call = _gateway(
        [_row("key-a")], [_fail(EnumDelegationFailureClass.TIMEOUT)]
    )
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 503
    assert reply["error"]["type"] == "gateway_upstream_failed"
    assert reply["error"]["reason"] == "gateway_upstream_failed"


def test_no_other_refusal_claims_exhausted_credit() -> None:
    gateway, _values, _call = _gateway([], [_ok()])
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 503
    assert reply["error"]["type"] == "gateway_no_key"
    assert reply["error"].get("reason") != "credit_exhausted"


def test_the_rendered_error_of_the_new_body_is_classified_unavailable() -> None:
    status, reply = _exhausted()
    error = _rendered_voter_error(status, reply)
    assert _substitutable_failure(error) == "was unavailable"
    assert '"reason":"credit_exhausted"' in error


def test_reason_leads_the_error_object_so_a_truncated_snippet_keeps_it() -> None:
    _status, reply = _exhausted()
    assert list(reply["error"])[:2] == ["type", "reason"]
