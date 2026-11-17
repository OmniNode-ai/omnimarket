# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime edge binds this mount's caller resolver from its overlay (OMN-24043).

The mount is built the way the runtime builds it — ``create_app(reader=FakeReader(...))``
with NO identity argument — so the resolver can only come from the overlay document
``<root>/node_hook_events_by_correlation_read_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``. Tokens are signed with a throwaway RSA key; the key
set is a fake provider patched in with ``monkeypatch.setattr(http_dependencies,
"JwksSigningKeyProvider", lambda _overlay: fake)`` — no network.

Failing control: the same verified request against a runtime whose overlay root
holds no overlay document answers 503 (the binding removed), and a token that
fails any check answers 401.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers import (
    http_dependencies,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_app import (
    create_app,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_dependencies import (
    IDENTITY_OVERLAY_ID,
)
from omnimarket.protocols.protocol_signing_key_provider import (
    SigningKeyUnavailableError,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import (
    CID,
    ROUTE,
    SLUG_A,
    SLUG_B,
    FakeReader,
    row,
    tenant,
)

pytestmark = pytest.mark.unit

ISSUER = "https://issuer.invalid/realms/dev"
AUDIENCE = "edge-audience"
OVERLAY: dict[str, Any] = {
    "jwks_url": "https://issuer.invalid/realms/dev/certs",
    "issuer": ISSUER,
    "audience": AUDIENCE,
    "algorithms": ["RS256"],
    "tenant_id_claim": "tenant_id",
    "tenant_slug_claim": "tenant_slug",
    "principal_id_claim": "sub",
    "jwks_cache_seconds": 300,
    "leeway_seconds": 0,
}
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SEEDED = [
    row(SLUG_A, event_id="alpha-1", run_id="run-alpha"),
    row(SLUG_B, event_id="bravo-1", run_id="run-bravo"),
]


class FakeKeys:
    """Test fake key set: always the throwaway signer's public half."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.calls = 0

    def signing_key_for(self, token: str) -> Any:
        self.calls += 1
        if self.down:
            raise SigningKeyUnavailableError("PyJWKClientConnectionError")
        return SIGNER.public_key()


def _token(*, key: Any = SIGNER, drop: str | None = None, **overrides: Any) -> str:
    bravo = tenant(SLUG_B)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "tenant_id": str(bravo.tenant_id),
        "tenant_slug": SLUG_B,
        "sub": bravo.principal_id,
    }
    claims.update(overrides)
    if drop:
        claims.pop(drop)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def _clean_binding() -> Iterator[None]:
    http_dependencies.bind_tenant_identity_resolver(None)
    yield
    http_dependencies.bind_tenant_identity_resolver(None)


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node's overlay directory holding the document, under the roots env."""
    root = tmp_path / "overlays"
    node_dir = root / IDENTITY_OVERLAY_ID
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return node_dir


@pytest.fixture
def empty_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node's overlay directory exists, but holds no document (yet)."""
    root = tmp_path / "overlays"
    node_dir = root / IDENTITY_OVERLAY_ID
    node_dir.mkdir(parents=True)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return node_dir


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> FakeKeys:
    fake = FakeKeys()
    monkeypatch.setattr(http_dependencies, "JwksSigningKeyProvider", lambda _o: fake)
    return fake


async def _read(token: str | None, reader: FakeReader) -> httpx.Response:
    app = create_app(reader=reader)  # no identity argument: the runtime's call
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as client:
        return await client.get(ROUTE, params={"correlation_id": CID}, headers=headers)


async def test_edge_identity_overlay_resolves_the_bearer_token_tenant_and_scopes_the_rows(
    overlay_root: Path, keys: FakeKeys
) -> None:
    reader = FakeReader(SEEDED)
    response = await _read(_token(), reader)
    assert response.status_code == 200
    body = response.json()
    assert body["data_state"] == "found"
    assert body["tenant_scope"] == SLUG_B
    assert [r["event_id"] for r in body["rows"]] == ["bravo-1"]
    assert {r["tenant_id"] for r in body["rows"]} == {SLUG_B}
    assert "alpha" not in str(body["rows"])
    assert reader.calls == [
        {"tenant_slug": SLUG_B, "correlation_id": CID, "limit": 10}
    ]  # 10: the contract's default limit


async def test_edge_identity_overlay_absent_document_refuses_the_same_request(
    empty_root: Path, keys: FakeKeys
) -> None:
    """Failing control: no overlay document, the verified request is refused."""
    reader = FakeReader(SEEDED)
    response = await _read(_token(), reader)
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC authentication is not configured"}
    assert reader.calls == []
    assert keys.calls == 0


async def test_edge_identity_overlay_with_a_symmetric_algorithm_refuses(
    overlay_root: Path, keys: FakeKeys
) -> None:
    reader = FakeReader(SEEDED)
    broken = dict(OVERLAY, algorithms=["HS256"])
    (overlay_root / "overlay.yaml").write_text(yaml.safe_dump(broken))
    response = await _read(_token(), reader)
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC authentication is not configured"}
    assert reader.calls == []


async def test_edge_identity_overlay_missing_a_setting_refuses(
    overlay_root: Path, keys: FakeKeys
) -> None:
    reader = FakeReader(SEEDED)
    partial = {k: v for k, v in OVERLAY.items() if k != "audience"}
    (overlay_root / "overlay.yaml").write_text(yaml.safe_dump(partial))
    response = await _read(_token(), reader)
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC authentication is not configured"}
    assert reader.calls == []


async def test_edge_identity_mount_answers_503_when_the_key_set_is_down(
    overlay_root: Path, keys: FakeKeys
) -> None:
    keys.down = True
    reader = FakeReader(SEEDED)
    response = await _read(_token(), reader)
    assert response.status_code == 503
    assert response.json() == {"detail": "Identity provider is unavailable"}
    assert reader.calls == []


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
async def test_edge_identity_mount_answers_401_for_a_token_that_fails_any_check(
    overlay_root: Path, keys: FakeKeys, token: str | None
) -> None:
    reader = FakeReader(SEEDED)
    response = await _read(token, reader)
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Unauthorized",
        "code": (
            "auth.credential.absent" if token is None else "auth.credential.invalid"
        ),
    }
    assert reader.calls == []


async def test_edge_identity_overlay_document_that_appears_after_create_app_is_used(
    empty_root: Path, keys: FakeKeys
) -> None:
    reader = FakeReader(SEEDED)
    app = create_app(reader=reader)  # built before the document exists
    headers = {"authorization": f"Bearer {_token()}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as client:
        refused = await client.get(
            ROUTE, params={"correlation_id": CID}, headers=headers
        )
        assert refused.status_code == 503
        assert reader.calls == []

        (empty_root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))

        served = await client.get(
            ROUTE, params={"correlation_id": CID}, headers=headers
        )
    assert served.status_code == 200
    body = served.json()
    assert body["data_state"] == "found"
    assert body["tenant_scope"] == SLUG_B
    assert [call["tenant_slug"] for call in reader.calls] == [SLUG_B]
