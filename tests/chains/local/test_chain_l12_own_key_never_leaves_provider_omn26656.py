# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An own-key session talks only to the customer's chosen provider (OMN-26656).

Goal item 3: "A customer's own-key session talks only to their chosen
provider." The customer registers their key and picks the model. The chosen
provider then fails (throttled, down, unreachable). The session must end on
that failure: the provider call log holds the chosen provider and nothing
else, so no OpenRouter house rung, no Claude rung and no local model is ever
called. Every declared successor rung sits behind one second provider, so a
swap is a single recorded fact.

Fakes only: loopback providers, a synthetic key, no network.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from tests.chains.local.harness import (
    BYOK_BACKEND_ID,
    PROVIDER_SLUG,
    LocalProviderStub,
    house_openrouter_rung,
    local_byok_catalogue,
    no_ambient_provider_credentials,
    run_local_delegation,
    shipped_byok_model_name,
    use_local_store,
)
from tests.chains.local.test_chain_l6_typed_credential_refusals_omn18698 import (
    _initial_tier_without_a_backend_pin,
    _ladder_with_every_successor,
    _passing_named_tests,
    next_rung_provider,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

_AUTOUSE = (_initial_tier_without_a_backend_pin, _passing_named_tests)
_SUCCESSOR_FIXTURE = next_rung_provider

_CUSTOMER_KEY_VALUE = "sk-or-omn26656-customer-supplied-value"
_DEAD_URL = "http://127.0.0.1:1/v1/chat/completions"


@pytest.mark.parametrize("status", [429, 503])
async def test_a_failing_chosen_provider_never_reaches_another_provider(
    status: int,
    provider_stub: LocalProviderStub,
    next_rung_provider: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_stub.completion_status = status
    provider_stub.completion_error_body = {"error": {"message": "unavailable"}}
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        _ladder_with_every_successor(
            monkeypatch,
            first_rung=house_openrouter_rung(monkeypatch),
            next_rung_url=next_rung_provider.completions_url,
        )
        register_local_byok_credential(
            PROVIDER_SLUG,
            _CUSTOMER_KEY_VALUE,
            model=shipped_byok_model_name(),
            db_path=db_path,
        )
        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
            backend_id=None,
        )

    call_log = [("chosen", a) for a in provider_stub.authorizations] + [
        ("other", a) for a in next_rung_provider.authorizations
    ]
    assert call_log, "positive control: the chosen provider was called"
    assert {who for who, _ in call_log} == {"chosen"}, call_log
    assert {a for _, a in call_log} == {f"Bearer {_CUSTOMER_KEY_VALUE}"}
    assert response.status == "failed"
    assert {a.backend_id for a in response.attempts} == {BYOK_BACKEND_ID}


async def test_an_unreachable_chosen_provider_never_reaches_another_provider(
    next_rung_provider: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, _DEAD_URL)
        _ladder_with_every_successor(
            monkeypatch,
            first_rung=house_openrouter_rung(monkeypatch),
            next_rung_url=next_rung_provider.completions_url,
        )
        register_local_byok_credential(
            PROVIDER_SLUG,
            _CUSTOMER_KEY_VALUE,
            model=shipped_byok_model_name(),
            db_path=db_path,
        )
        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
            backend_id=None,
        )

    assert response.status == "failed"
    assert {a.backend_id for a in response.attempts} == {BYOK_BACKEND_ID}
    assert next_rung_provider.authorizations == []
