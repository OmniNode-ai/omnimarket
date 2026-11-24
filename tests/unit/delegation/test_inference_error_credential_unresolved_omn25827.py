# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local no-credential refusal is not a provider auth failure (OMN-25827, F3).

``CredentialUnresolvedError`` refuses the call BEFORE the outbound request, so no
provider ever saw a credential. Its message explains the refusal with the words
"Authorization header" and "unauthenticated", and the bare ``"auth"`` marker in
``_inference_error_failure_class`` read that sentence as
``provider_auth_failed``: the vendor was reported for a condition the platform
refused locally. The code leads the message, so it is matched before every
generic marker, and the bare ``"auth"`` substring marker is gone.
"""

from __future__ import annotations

import pytest
from omnibase_core.models.delegation.wire import EnumCredentialSource

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _inference_error_failure_class,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    CredentialUnresolvedError,
)


@pytest.mark.parametrize(
    "api_key_ref",
    ["secret://anthropic/house-key", None],
)
def test_the_local_no_credential_refusal_reads_credential_missing(
    api_key_ref: str | None,
) -> None:
    refusal = str(
        CredentialUnresolvedError(
            expected=EnumCredentialSource.HOUSE,
            api_key_ref=api_key_ref,
            tenant_id="tenant-a",
        )
    )
    # The sentence that read as an auth rejection, verbatim from the effect.
    assert "Authorization header" in refusal
    assert "unauthenticated" in refusal

    assert (
        _inference_error_failure_class(refusal) is Failure.PROVIDER_CREDENTIAL_MISSING
    )


def test_a_bare_auth_substring_is_not_an_auth_rejection() -> None:
    """The word "auth" inside prose names no status the provider answered."""
    message = "the author of the prompt asked for a summary and the call failed"
    assert _inference_error_failure_class(message) is Failure.UNKNOWN


@pytest.mark.parametrize(
    "message",
    [
        "provider HTTP 401 Unauthorized for http://127.0.0.1:8000/v1/chat/completions",
        "provider HTTP 403 Forbidden for http://127.0.0.1:8000/v1/chat/completions",
        "401 invalid x-api-key",
        "Unauthorized",
    ],
)
def test_a_rejected_credential_keeps_its_class(message: str) -> None:
    """The control: a provider that answered 401/403 still reads auth failed."""
    assert _inference_error_failure_class(message) is Failure.PROVIDER_AUTH_FAILED
