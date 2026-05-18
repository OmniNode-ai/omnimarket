# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for AdapterLlmClassify DI injection (OMN-10874)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_classify import (
    AdapterLlmClassify,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    ClassifyResult,
    ScoredTicket,
)


def _make_ticket(
    ticket_id: str = "OMN-001",
    title: str = "add tests for handler",
    description: str = "Add unit tests.",
) -> ScoredTicket:
    return ScoredTicket(ticket_id=ticket_id, title=title, description=description)


def _mock_provider(response_json: str) -> MagicMock:
    """Return a mock that satisfies ProtocolLLMProvider.generate_async()."""
    provider = MagicMock()
    response = MagicMock()
    response.generated_text = response_json
    provider.generate_async = AsyncMock(return_value=response)
    provider.close = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_classify_uses_injected_provider() -> None:
    """When provider= is passed, AdapterLlmClassify must use it, not env-driven construction."""
    provider = _mock_provider(
        json.dumps({"buildability": "auto_buildable", "reason": "simple fix"})
    )
    classifier = AdapterLlmClassify(provider=provider)

    result = await classifier.handle(
        correlation_id=uuid4(),
        tickets=(_make_ticket(),),
    )

    assert isinstance(result, ClassifyResult)
    assert len(result.classifications) == 1
    assert result.classifications[0].buildability == "auto_buildable"
    provider.generate_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_injected_provider_no_env_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With provider= injected, LLM_CODER_FAST_URL env var must not be required."""
    monkeypatch.delenv("LLM_CODER_FAST_URL", raising=False)
    provider = _mock_provider(json.dumps({"buildability": "skip", "reason": "stale"}))
    # Should not raise even without the env var
    classifier = AdapterLlmClassify(provider=provider)
    result = await classifier.handle(
        correlation_id=uuid4(),
        tickets=(_make_ticket(title="stale ticket"),),
    )
    assert result.classifications[0].buildability == "skip"


@pytest.mark.asyncio
async def test_classify_missing_env_and_no_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without provider= and without env var, construction must raise RuntimeError."""
    monkeypatch.delenv("LLM_CODER_FAST_URL", raising=False)
    with pytest.raises(RuntimeError, match="LLM_CODER_FAST_URL"):
        AdapterLlmClassify()


@pytest.mark.asyncio
async def test_classify_injected_provider_keyword_fallback() -> None:
    """When injected provider raises, keyword fallback should be used."""
    provider = MagicMock()
    provider.generate_async = AsyncMock(side_effect=Exception("network error"))
    classifier = AdapterLlmClassify(provider=provider)
    ticket = _make_ticket(title="stale wip duplicate", description="blocked waiting")
    result = await classifier.handle(
        correlation_id=uuid4(),
        tickets=(ticket,),
    )
    # Keyword fallback fires: "stale" => skip
    assert result.classifications[0].buildability == "skip"


@pytest.mark.asyncio
async def test_classify_close_delegates_to_provider() -> None:
    """close() must call close() on the injected provider."""
    provider = _mock_provider("{}")
    classifier = AdapterLlmClassify(provider=provider)
    await classifier.close()
    provider.close.assert_awaited_once()
