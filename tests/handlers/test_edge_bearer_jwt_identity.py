# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real bearer-token resolver against a real key set served on localhost.

``JwksSigningKeyProvider`` fetches the issuer's published JWKS over HTTP from a
``ThreadingHTTPServer`` on 127.0.0.1 — the only network this module touches — and
``BearerJwtTenantIdentity`` verifies the token's signature, issuer, audience and
expiry against the overlay before trusting its tenant claims.

A token whose signature does not verify resolves to no tenant; an unreachable key
set raises, which the HTTP mount answers 503: the edge never serves a caller it
could not judge.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from omnimarket.handlers.edge_bearer_jwt_identity import (
    BearerJwtTenantIdentity,
    JwksSigningKeyProvider,
)
from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.protocols.protocol_signing_key_provider import (
    SigningKeyUnavailableError,
)

pytestmark = pytest.mark.unit

ISSUER = "https://issuer.invalid/realms/dev"
AUDIENCE = "edge-audience"
TENANT_ID = UUID("00000000-0000-4000-8000-0000000000ab")
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)

JWK = jwt.algorithms.RSAAlgorithm.to_jwk(SIGNER.public_key(), as_dict=True)
JWK.update({"kid": "k1", "use": "sig", "alg": "RS256"})
JWKS = {"keys": [JWK]}


class _JwksHandler(BaseHTTPRequestHandler):
    """Serves the one JWKS document at /certs; anything else is a 404."""

    # stdlib's handler protocol fixes the camel-case name
    def do_GET(self) -> None:
        if self.path != "/certs":
            self.send_error(404)
            return
        body = json.dumps(JWKS).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep the test output quiet


def _token(*, key: Any = SIGNER) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "tenant_id": str(TENANT_ID),
        "tenant_slug": "tenant-alpha",
        "sub": "principal-alpha",
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def _overlay(jwks_url: str) -> ModelEdgeIdentityOverlay:
    return ModelEdgeIdentityOverlay(
        jwks_url=jwks_url,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=["RS256"],
        tenant_id_claim="tenant_id",
        tenant_slug_claim="tenant_slug",
        principal_id_claim="sub",
        jwks_cache_seconds=300,
        leeway_seconds=0,
    )


@pytest.fixture
def jwks_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def test_edge_identity_real_key_set_resolves_a_verified_bearer_token(
    jwks_server: ThreadingHTTPServer,
) -> None:
    overlay = _overlay(f"http://127.0.0.1:{jwks_server.server_address[1]}/certs")
    identity = BearerJwtTenantIdentity(overlay, JwksSigningKeyProvider(overlay))
    resolved = await identity.resolve({"authorization": f"Bearer {_token()}"})
    assert resolved == ModelHttpTenantIdentity(
        tenant_id=TENANT_ID,
        tenant_slug="tenant-alpha",
        principal_id="principal-alpha",
    )


async def test_edge_identity_real_key_set_resolves_no_tenant_for_a_foreign_signature(
    jwks_server: ThreadingHTTPServer,
) -> None:
    overlay = _overlay(f"http://127.0.0.1:{jwks_server.server_address[1]}/certs")
    identity = BearerJwtTenantIdentity(overlay, JwksSigningKeyProvider(overlay))
    forged = _token(key=OTHER_SIGNER)  # names kid k1, signs with another key
    assert await identity.resolve({"authorization": f"Bearer {forged}"}) is None


async def test_edge_identity_real_key_set_down_raises_signing_key_unavailable(
    jwks_server: ThreadingHTTPServer,
) -> None:
    overlay = _overlay(f"http://127.0.0.1:{jwks_server.server_address[1]}/certs")
    jwks_server.shutdown()
    jwks_server.server_close()
    identity = BearerJwtTenantIdentity(overlay, JwksSigningKeyProvider(overlay))
    with pytest.raises(SigningKeyUnavailableError):
        await identity.resolve({"authorization": f"Bearer {_token()}"})
