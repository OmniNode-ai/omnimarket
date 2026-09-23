# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""C10 safety proof: an overlay-ADDED backend cannot put a house key on a customer route.

OMN-17099 (operator ruling 2026-09-22, "remove the block") lets a site overlay
ADD a fully specified backend the committed contract does not declare. That is
new freedom on a surface that decides which credential a delegation runs on, so
the operator required a proof that it cannot be used to bind OmniNode's own
credential to a customer's route.

The proof runs the backends through the SAME resolution the routing reducer
uses — ``_load_bifrost_endpoints`` over the committed contract plus an overlay,
bound by ``BIFROST_CONTRACT_PATH`` / ``BIFROST_OVERLAY_PATH`` — then derives the
house credential set the way every terminus enforcement point does
(``house_credential_refs`` / ``shipped_house_credential_refs``), and asserts:

1. the ``secret_ref`` the overlay-added backend declares is IN the house set —
   a platform backend is a platform backend no matter which file declared it;
2. ``enforce_customer_key_terminus`` refuses a customer-attributed tenant on
   that route with ``HOUSE_CREDENTIAL_ON_CUSTOMER_PATH``;
3. house and untenanted work on the same route is unaffected;
4. an overlay-added backend with an explicit null ``secret_ref``, for a
   customer on the CLOUD surface, is refused ``NO_PROVIDER_KEY_REGISTERED`` —
   an overlay-added platform backend is not a customer-declared backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.customer_key_terminus import (
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    enforce_customer_key_terminus,
    house_credential_refs,
)

pytestmark = pytest.mark.unit

_COMMITTED_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

_CUSTOMER = "acme-corp"
_ADDED_ID = "site-added-cloud"
_ADDED_KEYLESS_ID = "site-added-keyless"
#: A ref the committed contract does NOT declare, so its presence in the house
#: set can only have come from the overlay-added backend.
_ADDED_REF = "llm.site_added.api_key"


def _added_backend(backend_id: str, secret_ref: str | None) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "provider": "site-inference",
        "endpoint_url": f"https://{backend_id}.example/v1/chat/completions",
        "model_name": "site-model-1",
        "tier": "cheap_cloud",
        "timeout_ms": 60000,
        "max_tokens": 8192,
        "secret_ref": secret_ref,
        "capabilities": ["code_generation"],
    }


@pytest.fixture
def overlay_adds_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    overlay_path.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    _added_backend(_ADDED_ID, _ADDED_REF),
                    _added_backend(_ADDED_KEYLESS_ID, None),
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_COMMITTED_CONTRACT))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _enforce(tenant_id: str | None, backend_id: str) -> None:
    endpoints = routing._load_bifrost_endpoints()
    backend = endpoints[backend_id]
    enforce_customer_key_terminus(
        tenant_id=tenant_id,
        task_type="code_generation",
        correlation_id=uuid4(),
        surface=EnumDelegationSurface.CLOUD,
        api_key_ref=backend.api_key_ref,
        api_key_env=backend.api_key_env,
        backend_ref=backend.endpoint_url,
        house_refs=house_credential_refs(endpoints),
    )


@pytest.mark.usefixtures("overlay_adds_backends")
def test_overlay_added_secret_ref_is_a_house_credential() -> None:
    endpoints = routing._load_bifrost_endpoints()
    assert _ADDED_ID in endpoints, "the overlay-added backend must resolve"
    assert endpoints[_ADDED_ID].api_key_ref == _ADDED_REF
    assert _ADDED_REF in house_credential_refs(endpoints)
    # The shipped accessor every enforcement point reads agrees.
    assert _ADDED_REF in routing.shipped_house_credential_refs()


@pytest.mark.usefixtures("overlay_adds_backends")
def test_customer_on_an_overlay_added_house_route_is_refused() -> None:
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        _enforce(_CUSTOMER, _ADDED_ID)
    refusal = excinfo.value.refusal
    assert (
        refusal.reason is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
    )
    assert refusal.attempted_api_key_ref == _ADDED_REF


@pytest.mark.usefixtures("overlay_adds_backends")
@pytest.mark.parametrize("tenant_id", [None, "", HOUSE_TENANT_SLUG])
def test_house_and_untenanted_work_on_the_added_route_is_unaffected(
    tenant_id: str | None,
) -> None:
    _enforce(tenant_id, _ADDED_ID)
    _enforce(tenant_id, _ADDED_KEYLESS_ID)


@pytest.mark.usefixtures("overlay_adds_backends")
def test_customer_on_an_overlay_added_keyless_backend_is_refused_on_cloud() -> None:
    endpoints = routing._load_bifrost_endpoints()
    assert endpoints[_ADDED_KEYLESS_ID].api_key_ref is None
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        _enforce(_CUSTOMER, _ADDED_KEYLESS_ID)
    assert (
        excinfo.value.refusal.reason
        is EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED
    )
