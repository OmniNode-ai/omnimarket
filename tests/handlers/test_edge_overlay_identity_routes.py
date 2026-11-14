# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime edge binds each mount's caller resolver from its overlay document.

The four mounts are built the way the runtime builds them -- ``create_app(...)``
with NO identity argument -- so the resolver can only come from the overlay
document ``<root>/<node>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.
Tokens are signed with a throwaway RSA key; the key set is a fake provider
patched in with ``monkeypatch.setattr(edge_overlay_identity,
"JwksSigningKeyProvider", lambda _o: fake)`` -- no network, no database.

Failing controls: a runtime whose overlay root holds no document answers 503
(the binding removed); a token that fails any check answers 401; and a
node-local bound resolver (the two mounts with one) serves even when no
document exists.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from omnimarket.handlers import edge_overlay_identity
from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_tenant_delegation_projection import (
    ModelTenantSavingsAggregate,
)
from omnimarket.nodes.node_inference_credentials.handlers.http_app import (
    create_app as create_inference_credentials_app,
)
from omnimarket.nodes.node_inference_credentials.handlers.identity_binding import (
    bind_tenant_identity_resolver as bind_inference_credentials_resolver,
)
from omnimarket.nodes.node_tenant_delegations_read_effect.handlers.http_app import (
    create_app as create_delegations_app,
)
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.http_app import (
    create_app as create_savings_app,
)
from omnimarket.nodes.node_usage_privileges.handlers.http_app import (
    create_app as create_usage_privileges_app,
)
from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    bind_tenant_identity_resolver as bind_usage_privileges_resolver,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from omnimarket.protocols.protocol_signing_key_provider import (
    SigningKeyUnavailableError,
)
from tests.nodes.node_inference_credentials.fakes import (
    REF_A,
    TENANT_A,
    FakeIntake,
    FakeReadModel,
)
from tests.nodes.node_usage_privileges.fakes import FakeUsagePrivilegeRepository

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
UNCONFIGURED_DETAIL = "OIDC authentication is not configured"
API_KEY = "seeded-fixture-key"


def _credential_unauthorized(presented: bool) -> dict[str, Any]:
    return {
        "detail": "Unauthorized",
        "code": "auth.credential.invalid" if presented else "auth.credential.absent",
    }


def _tenant_not_provisioned(_presented: bool) -> dict[str, Any]:
    return {"detail": "tenant_not_provisioned"}


def _assert_privileges_served(body: Any, fake: FakeUsagePrivilegeRepository) -> None:
    assert [p["plan_tier_code"] for p in body] == ["free", "pro"]
    assert fake.calls == 1


def _assert_credentials_served(body: Any, fake: FakeReadModel) -> None:
    assert [item["api_key_ref"] for item in body] == [REF_A]
    assert fake.calls == 1


def _assert_savings_served(body: Any, fake: InMemoryTenantProjectionReader) -> None:
    assert (
        body["total_saved_usd"],
        body["total_spend_usd"],
        body["delegation_runs"],
        body["data_state"],
    ) == (11.0, 1.0, 4, "ok")
    assert fake.calls == [("read_savings", TENANT_A.tenant_id)]


def _assert_delegations_served(body: Any, fake: InMemoryTenantProjectionReader) -> None:
    assert (body["count"], body["data_state"], body["runs"]) == (
        0,
        "insufficient_data",
        [],
    )
    assert fake.calls == [("read_recent_delegations", TENANT_A.tenant_id)]


def _assert_int_counter(fake: Any) -> None:
    assert fake.calls == 0


def _assert_reader_calls(fake: InMemoryTenantProjectionReader) -> None:
    assert fake.calls == []


@dataclass(frozen=True)
class RouteCase:
    """One mount: its overlay id, path, the runtime's build call, and its assertions."""

    name: str
    path: str
    overlay_id: str
    app_factory: Callable[[Any], FastAPI]
    make_fake: Callable[[], Any]
    assert_served: Callable[[Any, Any], None]
    assert_no_read: Callable[[Any], None]
    unauthorized_body: Callable[[bool], dict[str, Any]]
    key_down_detail: str
    bind_local: Callable[[ProtocolHttpTenantIdentity], None] | None


USAGE_PRIVILEGES = RouteCase(
    name="node_usage_privileges",
    path="/v1/usage/privileges",
    overlay_id="node_usage_privileges",
    app_factory=lambda fake: create_usage_privileges_app(repository=fake),
    make_fake=FakeUsagePrivilegeRepository,
    assert_served=_assert_privileges_served,
    assert_no_read=_assert_int_counter,
    unauthorized_body=_credential_unauthorized,
    key_down_detail="identity resolution unavailable",
    bind_local=bind_usage_privileges_resolver,
)
INFERENCE_CREDENTIALS = RouteCase(
    name="node_inference_credentials",
    path="/v1/tenants/me/inference-credentials",
    overlay_id="node_inference_credentials",
    app_factory=lambda fake: create_inference_credentials_app(
        intake=FakeIntake(), read_model=fake
    ),
    make_fake=FakeReadModel,
    assert_served=_assert_credentials_served,
    assert_no_read=_assert_int_counter,
    unauthorized_body=_credential_unauthorized,
    key_down_detail="identity resolution unavailable",
    bind_local=bind_inference_credentials_resolver,
)
TENANT_SAVINGS = RouteCase(
    name="node_tenant_savings_read_effect",
    path="/v1/tenants/me/savings",
    overlay_id="node_tenant_savings_read_effect",
    app_factory=lambda fake: create_savings_app(reader=fake),
    make_fake=lambda: InMemoryTenantProjectionReader(
        savings={
            TENANT_A.tenant_id: ModelTenantSavingsAggregate(
                total_saved_usd=11.0, total_spend_usd=1.0, delegation_runs=4
            )
        }
    ),
    assert_served=_assert_savings_served,
    assert_no_read=_assert_reader_calls,
    unauthorized_body=_tenant_not_provisioned,
    key_down_detail=UNCONFIGURED_DETAIL,
    bind_local=None,
)
TENANT_DELEGATIONS = RouteCase(
    name="node_tenant_delegations_read_effect",
    path="/v1/tenants/me/delegations",
    overlay_id="node_tenant_delegations_read_effect",
    app_factory=lambda fake: create_delegations_app(reader=fake),
    make_fake=lambda: InMemoryTenantProjectionReader(runs={TENANT_A.tenant_id: ()}),
    assert_served=_assert_delegations_served,
    assert_no_read=_assert_reader_calls,
    unauthorized_body=_tenant_not_provisioned,
    key_down_detail=UNCONFIGURED_DETAIL,
    bind_local=None,
)

CASES = [USAGE_PRIVILEGES, INFERENCE_CREDENTIALS, TENANT_SAVINGS, TENANT_DELEGATIONS]
LOCAL_BINDING_CASES = [case for case in CASES if case.bind_local is not None]
CASE_IDS = [case.name for case in CASES]


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


class SeededApiKeyValue:
    """Resolves exactly the seeded ``x-api-key`` to the tenant; everyone else is unknown."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        self.calls += 1
        if headers.get("x-api-key") == API_KEY:
            return ModelHttpTenantIdentity(
                tenant_id=TENANT_A.tenant_id,
                tenant_slug=TENANT_A.tenant_slug,
                principal_id=TENANT_A.principal_id,
            )
        return None


def _token(*, key: Any = SIGNER, drop: str | None = None, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "tenant_id": str(TENANT_A.tenant_id),
        "tenant_slug": TENANT_A.tenant_slug,
        "sub": TENANT_A.principal_id,
    }
    claims.update(overrides)
    if drop:
        claims.pop(drop)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def _clean_binding() -> Iterator[None]:
    edge_overlay_identity.clear_overlay_identity_cache()
    bind_usage_privileges_resolver(None)
    bind_inference_credentials_resolver(None)
    yield
    edge_overlay_identity.clear_overlay_identity_cache()
    bind_usage_privileges_resolver(None)
    bind_inference_credentials_resolver(None)


def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "overlays"
    for case in CASES:
        node_dir = root / case.overlay_id
        node_dir.mkdir(parents=True)
        (node_dir / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return root


def _empty_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "overlays"
    for case in CASES:
        (root / case.overlay_id).mkdir(parents=True)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return root


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each node's overlay directory holds its document, under the roots env."""
    return _overlay_root(tmp_path, monkeypatch)


@pytest.fixture
def empty_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each node's overlay directory exists, but holds no document (yet)."""
    return _empty_root(tmp_path, monkeypatch)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> FakeKeys:
    fake = FakeKeys()
    monkeypatch.setattr(
        edge_overlay_identity, "JwksSigningKeyProvider", lambda _o: fake
    )
    return fake


async def _read(
    case: RouteCase, token: str | None, headers: dict[str, str] | None = None
) -> tuple[httpx.Response, Any]:
    fake = case.make_fake()
    app = case.app_factory(fake)
    request_headers = (
        headers
        if headers is not None
        else ({"authorization": f"Bearer {token}"} if token else {})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as client:
        return await client.get(case.path, headers=request_headers), fake


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_an_overlay_document_with_a_valid_token_serves_and_reads(
    overlay_root: Path, keys: FakeKeys, case: RouteCase
) -> None:
    response, fake = await _read(case, _token())
    assert response.status_code == 200
    case.assert_served(response.json(), fake)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_a_root_with_no_document_refuses_the_same_request(
    empty_root: Path, keys: FakeKeys, case: RouteCase
) -> None:
    """Failing control: the verified request is refused and no read runs."""
    response, fake = await _read(case, _token())
    assert response.status_code == 503
    assert response.json() == {"detail": UNCONFIGURED_DETAIL}
    case.assert_no_read(fake)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_a_document_with_a_symmetric_algorithm_refuses(
    overlay_root: Path, keys: FakeKeys, case: RouteCase
) -> None:
    (overlay_root / case.overlay_id / "overlay.yaml").write_text(
        yaml.safe_dump(dict(OVERLAY, algorithms=["HS256"]))
    )
    response, fake = await _read(case, _token())
    assert response.status_code == 503
    assert response.json() == {"detail": UNCONFIGURED_DETAIL}
    case.assert_no_read(fake)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_a_document_missing_a_setting_refuses(
    overlay_root: Path, keys: FakeKeys, case: RouteCase
) -> None:
    partial = {k: v for k, v in OVERLAY.items() if k != "audience"}
    (overlay_root / case.overlay_id / "overlay.yaml").write_text(
        yaml.safe_dump(partial)
    )
    response, fake = await _read(case, _token())
    assert response.status_code == 503
    assert response.json() == {"detail": UNCONFIGURED_DETAIL}
    case.assert_no_read(fake)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_a_down_key_set_refuses_before_any_read(
    overlay_root: Path, keys: FakeKeys, case: RouteCase
) -> None:
    keys.down = True
    response, fake = await _read(case, _token())
    assert response.status_code == 503
    assert response.json() == {"detail": case.key_down_detail}
    case.assert_no_read(fake)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
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
    ],
    ids=[
        "no-header",
        "malformed",
        "wrong-signer",
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "no-tenant-claim",
    ],
)
async def test_a_token_that_fails_any_check_refuses_before_any_read(
    overlay_root: Path, keys: FakeKeys, case: RouteCase, token: str | None
) -> None:
    response, fake = await _read(case, token)
    assert response.status_code == 401
    assert response.json() == case.unauthorized_body(token is not None)
    case.assert_no_read(fake)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_a_document_that_appears_after_create_app_is_used(
    empty_root: Path, keys: FakeKeys, case: RouteCase
) -> None:
    fake = case.make_fake()
    app = case.app_factory(fake)  # built before the document exists
    headers = {"authorization": f"Bearer {_token()}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as client:
        refused = await client.get(case.path, headers=headers)
        assert refused.status_code == 503
        case.assert_no_read(fake)

        (empty_root / case.overlay_id / "overlay.yaml").write_text(
            yaml.safe_dump(OVERLAY)
        )
        served = await client.get(case.path, headers=headers)
    assert served.status_code == 200
    case.assert_served(served.json(), fake)


@pytest.mark.parametrize(
    "case", LOCAL_BINDING_CASES, ids=[case.name for case in LOCAL_BINDING_CASES]
)
async def test_an_explicitly_bound_node_resolver_wins_without_a_document(
    empty_root: Path, case: RouteCase
) -> None:
    """The two mounts with a node-local binding serve when one is bound there."""
    assert case.bind_local is not None
    case.bind_local(SeededApiKeyValue())
    response, fake = await _read(case, None, headers={"x-api-key": API_KEY})
    assert response.status_code == 200
    case.assert_served(response.json(), fake)
