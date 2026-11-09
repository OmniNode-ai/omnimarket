# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime edge binds a tenant identity resolver from its overlay (OMN-23305).

The mount is built the way the runtime builds it (``create_app()`` with no identity),
so the resolver can only come from the node's overlay under ``ONEX_SKILL_OVERLAY_ROOTS``.
Tokens are signed with a throwaway key; the key set is a fake provider, no network.

Failing control: the same authenticated request against a runtime whose overlay root
holds no overlay answers 503 (the binding removed), and a token that fails any check
answers 401.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

from omnimarket.nodes.node_inference_credentials.handlers import identity_binding
from omnimarket.nodes.node_inference_credentials.handlers.http_app import create_app
from omnimarket.nodes.node_inference_credentials.handlers.identity_binding import (
    OVERLAY_ID,
    bind_tenant_identity_resolver,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_signing_key_provider import (
    SigningKeyUnavailableError,
)
from tests.nodes.node_inference_credentials.fakes import (
    REF_A,
    TENANT_A,
    FakeIntake,
    FakeReadModel,
)

pytestmark = pytest.mark.unit
PREFIX = "/v1/tenants/me/inference-credentials"
ISSUER = "https://issuer.invalid/realms/dev"
AUDIENCE = "edge-audience"
OVERLAY = {
    "jwks_url": "https://issuer.invalid/realms/dev/certs",
    "issuer": ISSUER,
    "audience": AUDIENCE,
    "algorithms": ["RS256"],
    "tenant_id_claim": "tenant_id",
    "tenant_slug_claim": "tenant_slug",
    "principal_id_claim": "principal_id",
    "jwks_cache_seconds": 300,
    "leeway_seconds": 0,
}
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKeys:
    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.calls = 0

    def signing_key_for(self, token: str) -> Any:
        self.calls += 1
        if self.down:
            raise SigningKeyUnavailableError("PyJWKClientConnectionError")
        return SIGNER.public_key()


def _token(*, key: Any = SIGNER, drop: str | None = None, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "tenant_id": str(TENANT_A.tenant_id),
        "tenant_slug": TENANT_A.tenant_slug,
        "principal_id": TENANT_A.principal_id,
    }
    claims.update(overrides)
    if drop:
        claims.pop(drop)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def _clean_binding() -> Iterator[None]:
    bind_tenant_identity_resolver(None)
    yield
    bind_tenant_identity_resolver(None)


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "overlays"
    (root / OVERLAY_ID).mkdir(parents=True)
    (root / OVERLAY_ID / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return root


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> FakeKeys:
    fake = FakeKeys()
    monkeypatch.setattr(identity_binding, "JwksSigningKeyProvider", lambda _o: fake)
    return fake


def _client() -> httpx.AsyncClient:
    app = create_app(intake=FakeIntake(), read_model=FakeReadModel())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    )


async def _list(token: str | None) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with _client() as client:
        return await client.get(PREFIX, headers=headers)


async def test_inference_credentials_mount_answers_200_for_a_verified_token(
    overlay_root: Path, keys: FakeKeys
) -> None:
    response = await _list(_token())
    assert response.status_code == 200
    assert [row["api_key_ref"] for row in response.json()] == [REF_A]


async def test_inference_credentials_mount_resolves_the_tenant_from_the_overlay_claims(
    overlay_root: Path, keys: FakeKeys
) -> None:
    other = UUID("00000000-0000-4000-8000-0000000000ee")
    response = await _list(_token(tenant_id=str(other)))
    assert response.status_code == 200
    assert response.json() == []  # a tenant with no rows sees none of tenant A's


async def test_inference_credentials_mount_answers_503_with_no_overlay_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys: FakeKeys
) -> None:
    """Failing control: the binding removed, the same request is refused."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    response = await _list(_token())
    assert response.status_code == 503
    assert keys.calls == 0


async def test_inference_credentials_mount_answers_503_for_an_invalid_overlay(
    overlay_root: Path, keys: FakeKeys
) -> None:
    broken = dict(OVERLAY, algorithms=["HS256"])
    (overlay_root / OVERLAY_ID / "overlay.yaml").write_text(yaml.safe_dump(broken))
    assert (await _list(_token())).status_code == 503


async def test_inference_credentials_mount_answers_503_when_the_key_set_is_down(
    overlay_root: Path, keys: FakeKeys
) -> None:
    keys.down = True
    assert (await _list(_token())).status_code == 503


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        _token(key=OTHER_SIGNER),
        _token(exp=int(time.time()) - 60),
        _token(aud="someone-else"),
        _token(iss="https://other.invalid"),
        _token(drop="tenant_id"),
        _token(tenant_id="not-a-uuid"),
        _token(drop="exp"),
    ],
    ids=[
        "no-header",
        "malformed",
        "wrong-signer",
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "no-tenant-claim",
        "bad-tenant-claim",
        "no-expiry",
    ],
)
async def test_inference_credentials_mount_answers_401_for_a_token_that_fails(
    overlay_root: Path, keys: FakeKeys, token: str | None
) -> None:
    assert (await _list(token)).status_code == 401


async def test_inference_credentials_mount_overlay_must_name_every_setting(
    overlay_root: Path, keys: FakeKeys
) -> None:
    partial = {k: v for k, v in OVERLAY.items() if k != "audience"}
    (overlay_root / OVERLAY_ID / "overlay.yaml").write_text(yaml.safe_dump(partial))
    assert (await _list(_token())).status_code == 503
