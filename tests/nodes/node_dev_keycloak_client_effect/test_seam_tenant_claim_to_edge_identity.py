# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: this node's mapper claims resolve through the by-correlation edge identity.

The mappers declared in overlay.example.yaml are turned into token claims the
way Keycloak does (claim.name <- the probe user's user.attribute value), signed
with an in-test RSA key, and resolved through
``omnimarket.handlers.edge_bearer_jwt_identity.BearerJwtTenantIdentity``. The
resolved tenant must equal the expected tenant; with the mappers emptied the
resolver returns None; and the set of claim names equals the overlay's tenant
claim names.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

from omnimarket.handlers.edge_bearer_jwt_identity import BearerJwtTenantIdentity
from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from tests.nodes.node_hook_events_by_correlation_read_effect.test_edge_identity_overlay import (
    OVERLAY as BY_CORRELATION_OVERLAY,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_keycloak_client_effect/overlay.example.yaml"
)

# The by-correlation read's own edge identity overlay values: a change to its claim names there breaks this seam.
EDGE_OVERLAY: dict[str, Any] = dict(BY_CORRELATION_OVERLAY)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_SLUG = "tenant-alpha"
PRINCIPAL_ID = "principal-alpha"
PROBE_ATTRIBUTES = {"tenant_id": str(TENANT_ID), "tenant_slug": TENANT_SLUG}
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _InTestSigningKeys:
    """A tiny in-test provider that always returns the throwaway signer's public half."""

    def signing_key_for(self, token: str) -> Any:
        return SIGNER.public_key()


def _overlay() -> ModelEdgeIdentityOverlay:
    return ModelEdgeIdentityOverlay(
        jwks_url=EDGE_OVERLAY["jwks_url"],
        issuer=EDGE_OVERLAY["issuer"],
        audience=EDGE_OVERLAY["audience"],
        algorithms=list(EDGE_OVERLAY["algorithms"]),
        tenant_id_claim=EDGE_OVERLAY["tenant_id_claim"],
        tenant_slug_claim=EDGE_OVERLAY["tenant_slug_claim"],
        principal_id_claim=EDGE_OVERLAY["principal_id_claim"],
        jwks_cache_seconds=EDGE_OVERLAY["jwks_cache_seconds"],
        leeway_seconds=EDGE_OVERLAY["leeway_seconds"],
    )


def _declared_mappers() -> list[dict[str, Any]]:
    overlay = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return list(overlay["sign_in_client"]["mappers"])


def _claims(mappers: list[dict[str, Any]]) -> dict[str, Any]:
    """Build token claims the way Keycloak does for each declared mapper."""
    claims: dict[str, Any] = {
        "iss": EDGE_OVERLAY["issuer"],
        "aud": EDGE_OVERLAY["audience"],
        "exp": int(time.time()) + 300,
        "sub": PRINCIPAL_ID,
    }
    for mapper in mappers:
        config = mapper["config"]
        claims[config["claim.name"]] = PROBE_ATTRIBUTES[config["user.attribute"]]
    return claims


def _token(mappers: list[dict[str, Any]]) -> str:
    return jwt.encode(
        _claims(mappers), SIGNER, algorithm="RS256", headers={"kid": "k1"}
    )


def _identity() -> BearerJwtTenantIdentity:
    return BearerJwtTenantIdentity(_overlay(), _InTestSigningKeys())


def test_seam_resolves_the_mapper_claims_to_the_expected_tenant() -> None:
    resolved = asyncio.run(
        _identity().resolve({"authorization": f"Bearer {_token(_declared_mappers())}"})
    )
    assert resolved == ModelHttpTenantIdentity(
        tenant_id=TENANT_ID,
        tenant_slug=TENANT_SLUG,
        principal_id=PRINCIPAL_ID,
    )


def test_seam_with_no_mappers_resolves_no_tenant() -> None:
    """Failing control: with the mappers emptied the resolver returns None."""
    resolved = asyncio.run(
        _identity().resolve({"authorization": f"Bearer {_token([])}"})
    )
    assert resolved is None


def test_seam_claim_names_equal_the_overlay_tenant_claims() -> None:
    mappers = _declared_mappers()
    claim_names = {mapper["config"]["claim.name"] for mapper in mappers}
    assert claim_names == {
        EDGE_OVERLAY["tenant_id_claim"],
        EDGE_OVERLAY["tenant_slug_claim"],
    }
