# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25506: OpenRouter's 401 "User not found" ends as a typed key refusal.

The gateway wrong-key test (4c) ran a customer's refused OpenRouter key and read
back ``no_higher_tier_available`` with the provider's own sentence
``provider HTTP 401 User not found``. OpenRouter answers a key it does not know
with that short body (no "Invalid API Key" wording), so the refusal must be read
from the status the provider answered, on the OpenRouter route a customer
registers, not from words in the body.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _inference_error_failure_class,
)
from tests.unit.delegation import (
    test_omn25506_wrong_customer_key_is_a_typed_refusal as base,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_BODY = "User not found"


def _openrouter_message() -> str:
    return (
        f"provider HTTP 401 Unauthorized for {OPENROUTER_URL}; "
        f'response_body={{"error":{{"message":"{OPENROUTER_BODY}","code":401}}}}'
    )


def test_openrouter_user_not_found_reads_as_a_rejected_credential() -> None:
    assert (
        _inference_error_failure_class(_openrouter_message())
        is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    )
    assert (
        _inference_error_failure_class("provider HTTP 401 User not found")
        is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    )


def test_a_refused_openrouter_customer_key_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, frontier_unconfigured_bifrost: None
) -> None:
    monkeypatch.setattr(base, "URL", OPENROUTER_URL)
    monkeypatch.setattr(base, "BACKEND_REF", "byok-openrouter")
    monkeypatch.setattr(base, "MODEL", "openai/gpt-4o-mini")
    monkeypatch.setattr(base, "PROVIDER_BODY", OPENROUTER_BODY)
    _, result = base._run_to_the_end(_openrouter_message())

    assert result.terminal_failure_reason == base.KEY_REJECTED
    assert OPENROUTER_BODY not in (result.failure_reason or "")
    assert OPENROUTER_URL not in (result.failure_reason or "")
    assert all(
        OPENROUTER_BODY not in r
        for h in result.escalation_history
        for r in h.get("failure_reasons", ())
    )
