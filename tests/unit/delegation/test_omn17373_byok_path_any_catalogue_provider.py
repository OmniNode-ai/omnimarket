# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17373: a customer key bound to ANY catalogue provider routes, with no house rung.

The keyed OpenAI customer test reached OpenAI zero times in all four work
classes. Three defects on the customer bring-your-own-key path, none specific to
OpenAI:

1. ``--backend-id byok-openai`` was resolved against the bifrost config only, so
   no ``byok-*`` id could be pinned.
2. Unpinned, a customer whose only key was for a provider the platform has no
   rung on (``mirrors_house_rung: false``) was refused as "No local model is
   declared", because the substitution only fired for a house-keyed rung.
3. Fail-closed with no key printed that same message instead of naming the
   missing customer key.

BYOK only: the customer pays their provider, and nothing here routes a customer
to a house or lab model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    resolve_delegation_backend,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    refuse_undeclared_local_model,
)
from omnimarket.routing.local_byok_route import (
    ByokKeyNotRegisteredError,
    substitute_any_registered_byok_route,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

pytestmark = pytest.mark.unit

_FAKE_KEY = "sk-test-" + "k" * 40
_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_TENANT = "3314bdfb-85c8-45fc-a601-e31e6b9d9d93"
_HOUSE_REF = "llm.glm.api_key"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


def _house_rung() -> ModelResolvedDelegationBackend:
    """The first cloud rung the fallback chain lands on: a HOUSE credential."""
    return ModelResolvedDelegationBackend(
        backend_id="cloud-glm",
        model_id="glm-5.3-flash",
        endpoint_ref="https://api.z.ai/api/coding/paas/v4/chat/completions",
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=240000,
        secret_ref=_HOUSE_REF,
    )


def test_a_pinned_byok_openai_id_resolves(local_store_at_tmp: Path) -> None:
    """Defect 1: the catalogue is consulted for a ``byok-*`` pin."""
    register_local_byok_credential("openai", _FAKE_KEY, db_path=local_store_at_tmp)

    routed = resolve_delegation_backend("document", backend_id="byok-openai")

    assert routed.backend_id == "byok-openai"
    assert routed.endpoint_ref == _OPENAI_ENDPOINT
    assert routed.secret_ref is not None
    assert is_tenant_credential_ref(routed.secret_ref)
    assert routed.api_key_env is None


def test_a_pinned_byok_id_with_no_key_names_the_missing_key() -> None:
    with pytest.raises(ByokKeyNotRegisteredError) as excinfo:
        resolve_delegation_backend("document", backend_id="byok-openai")

    message = str(excinfo.value)
    assert "openai" in message
    assert "no key" in message.lower()
    assert "onex secret set llm.openai.api_key" in message


def test_an_unpinned_run_with_only_an_openai_key_routes_to_byok_openai(
    local_store_at_tmp: Path,
) -> None:
    """Defect 2: a provider with no house rung is routable on the customer's key."""
    register_local_byok_credential("openai", _FAKE_KEY, db_path=local_store_at_tmp)

    routed = substitute_any_registered_byok_route(
        _house_rung(), db_path=local_store_at_tmp
    )

    assert routed.backend_id == "byok-openai"
    assert routed.endpoint_ref == _OPENAI_ENDPOINT
    assert routed.secret_ref is not None
    assert is_tenant_credential_ref(routed.secret_ref)
    assert routed.secret_ref != _HOUSE_REF
    assert routed.substituted_from_backend_id == "cloud-glm"


def test_an_unpinned_run_with_no_key_is_returned_unchanged(
    local_store_at_tmp: Path,
) -> None:
    """No key, no route: the caller's refusal (below) names the missing key."""
    rung = _house_rung()

    assert (
        substitute_any_registered_byok_route(rung, db_path=local_store_at_tmp) is rung
    )


def test_a_local_rung_is_never_replaced(local_store_at_tmp: Path) -> None:
    """Cheapest-first holds: a free local rung keeps the work."""
    register_local_byok_credential("openai", _FAKE_KEY, db_path=local_store_at_tmp)
    local = ModelResolvedDelegationBackend(
        backend_id="local-coder",
        model_id="m",
        endpoint_ref="http://127.0.0.1:8000/v1/chat/completions",  # url-authority-ok: test loopback
        tier="local",
        max_tokens=1024,
        timeout_ms=1000,
    )

    assert (
        substitute_any_registered_byok_route(local, db_path=local_store_at_tmp) is local
    )


def test_the_no_key_refusal_names_the_missing_customer_key() -> None:
    """Defect 3: with no model and no key, the refusal says the key is missing."""
    with pytest.raises(ProtocolConfigurationError) as excinfo:
        refuse_undeclared_local_model(
            tenant_id=_TENANT,
            backend=_house_rung(),
            house_refs=frozenset({_HOUSE_REF}),
            backends=[
                {"backend_id": "local-coder", "tier": "local", "endpoint_url": None},
                {"backend_id": "cloud-glm", "tier": "cheap_cloud"},
            ],
        )

    message = str(excinfo.value)
    assert "No local model is declared" in message
    assert "no provider key is registered" in message
    # Survives the consume boundary's sanitizer, which is what a customer sees.
    from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

    assert "no provider key is registered" in sanitize_error_message(excinfo.value)
