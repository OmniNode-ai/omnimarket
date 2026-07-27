# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live cloud-completion proof for OMN-13943 (opt-in, never runs in CI).

The ticket's live probe found cloud delegation genuinely completes end-to-end
once the secret-name drift is fixed: Gemini's dotted ``llm.gemini.api_key``
convention maps to ``LLM_GEMINI_API_KEY`` (unset), but the canonical
``~/.omnibase/.env`` defines ``GEMINI_API_KEY`` — the bifrost contract's own
``api_key_env: GEMINI_API_KEY`` field names it correctly but was previously
dead code (secret_ref always won). This test proves the ``api_key_env``
fallback wired in OMN-13943 (``secret_store_resolver.resolve_api_key_async``)
makes that real, through the canonical effect handler — not a raw curl probe.

Opt-in only: set ``OMN_ALLOW_LIVE_CLOUD_DELEGATION=1`` and export the real
``GEMINI_API_KEY`` (already present in the canonical ``~/.omnibase/.env``) to
run. CI never exercises this; the escalation-loop and secret-fallback unit
tests are the deterministic, hermetic proof for that logic. This test is the
"does it actually complete" evidence for the DoD's live re-run requirement.
"""

from __future__ import annotations

import os

import pytest

from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerLlmDelegationCall,
    ModelLlmDelegationCallRequest,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend,
    resolve_timeout_seconds,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMN_ALLOW_LIVE_CLOUD_DELEGATION") != "1",
    reason=(
        "live cloud delegation call; set OMN_ALLOW_LIVE_CLOUD_DELEGATION=1 "
        "(with a real GEMINI_API_KEY exported) to enable"
    ),
)


@pytest.mark.live_model
def test_gemini_flash_completes_a_real_summarization_call() -> None:
    """resolve_delegation_backend + HandlerLlmDelegationCall complete for real.

    Pins the exact reachable backend the OMN-13943 probe proved live
    (``cloud-gemini-flash``, model ``gemini-2.5-flash-lite``) so this test is a
    deterministic re-run of that proof rather than a routing-policy check.
    """
    backend = resolve_delegation_backend(
        "summarization", backend_id="cloud-gemini-flash"
    )
    assert backend.secret_ref == "llm.gemini.api_key"
    assert backend.api_key_env == "GEMINI_API_KEY"

    request = ModelLlmDelegationCallRequest(
        request_id="omn-13943-live-check",
        correlation_id="00000000-0000-0000-0000-000000000001",
        causation_id="00000000-0000-0000-0000-000000000001",
        model_id=backend.model_id,
        endpoint_ref=backend.endpoint_ref,
        prompt="Reply with exactly the single word: pong",
        prompt_hash="",
        task_type="summarization",
        max_tokens=16,
        timeout_seconds=resolve_timeout_seconds(backend_timeout_ms=backend.timeout_ms),
        secret_ref=backend.secret_ref,
        api_key_env=backend.api_key_env,
    )

    result = HandlerLlmDelegationCall()(request)

    assert result.success is True, result.error_message
    assert result.content
    assert result.tokens_out > 0
