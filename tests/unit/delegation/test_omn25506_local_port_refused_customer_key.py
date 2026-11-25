# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25506: the local port states a refused customer key as the typed reason too."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.inference.secret_store_resolver import (
    resolve_api_key_with_source_loop_safe,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from tests.chains.local.harness import (
    house_openrouter_rung,
    run_local_delegation,
    use_local_store,
)
from tests.chains.local.test_chain_l6_typed_credential_refusals_omn18698 import (
    _ladder_with_every_successor,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    KEY,
    OLD,
    SUCCESSOR_URL,
    FakeModels,
    _bind_effect,
    _row,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("catalogue_picks_models")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_customer_key_ends_with_the_typed_reason_and_no_provider_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    row = _row("openrouter")
    provider, _ = _bind_effect(monkeypatch, [status], models=FakeModels({"data": []}))
    db_path = use_local_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        resolve_api_key_with_source_loop_safe,
    )
    credentials.register_local_byok_credential(
        "openrouter", KEY, model=OLD, model_chosen_by="catalogue", db_path=db_path
    )
    _ladder_with_every_successor(
        monkeypatch,
        first_rung=house_openrouter_rung(monkeypatch),
        next_rung_url=SUCCESSOR_URL,
    )
    response = await run_local_delegation(
        prompt="write a function that parses a semver string",
        db_path=db_path,
        correlation_id=uuid4(),
    )
    assert response.status == "failed"
    assert all(url == row.endpoint_url for url, _ in provider.calls)
    assert response.error_message is not None
    assert "provider_key_rejected" in response.error_message
    assert "no_higher_tier_available" not in response.error_message
    assert row.endpoint_url not in response.error_message
    assert "Incorrect API key" not in response.error_message
    for attempt in response.attempts:
        assert row.endpoint_url not in str(attempt.error_message or "")
