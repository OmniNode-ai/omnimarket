# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every refusal the routes make, and the side effect that must not happen.

Each case names what must stay untouched: no read and no write runs for a caller that
does not resolve, a revoke never reaches the publisher for a ref the caller does not
own, and the customer's key value appears in no response and no log line.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from omnimarket.nodes.node_inference_credentials.handlers.http_app import create_app
from omnimarket.nodes.node_inference_credentials.handlers.identity_binding import (
    bind_tenant_identity_resolver,
)
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    CredentialPlanUndeterminedError,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from tests.nodes.node_inference_credentials.fakes import (
    CREATE_BODY,
    KEY_A,
    KEY_B,
    REF_A,
    REF_B,
    SECRET_VALUE,
    FailingTenantIdentity,
    FakeIntake,
    FakeReadModel,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
PREFIX = "/v1/tenants/me/inference-credentials"


@pytest.fixture(autouse=True)
def _unbound() -> Iterator[None]:
    bind_tenant_identity_resolver(None)
    yield
    bind_tenant_identity_resolver(None)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    )


def _app(
    intake: FakeIntake | None = None,
    read_model: FakeReadModel | None = None,
    identity: ProtocolHttpTenantIdentity | None = None,
) -> tuple[FastAPI, FakeIntake, FakeReadModel]:
    intake = intake or FakeIntake()
    read_model = read_model or FakeReadModel()
    app = create_app(
        intake=intake,
        read_model=read_model,
        identity=identity or SeededTenantIdentity(),
    )
    return app, intake, read_model


CALLS = [
    ("POST", PREFIX),
    ("GET", PREFIX),
    ("DELETE", f"{PREFIX}/{REF_A}"),
]


@pytest.mark.parametrize(("method", "path"), CALLS)
@pytest.mark.parametrize(
    "headers", [{}, {"x-api-key": KEY_A}, {"authorization": "Bearer abc"}]
)
async def test_no_resolver_bound_refuses_503_before_any_read_or_write(
    method: str, path: str, headers: dict[str, str]
) -> None:
    intake, read_model = FakeIntake(), FakeReadModel()
    app = create_app(intake=intake, read_model=read_model)
    async with _client(app) as client:
        response = await client.request(
            method, path, headers=headers, content=CREATE_BODY
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC authentication is not configured"}
    assert (read_model.calls, intake.registered, intake.revoked) == (0, [], [])


@pytest.mark.parametrize(("method", "path"), CALLS)
async def test_anonymous_caller_is_401_absent_with_no_read_or_write(
    method: str, path: str
) -> None:
    app, intake, read_model = _app()
    async with _client(app) as client:
        response = await client.request(method, path, content=CREATE_BODY)
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Unauthorized",
        "code": "auth.credential.absent",
    }
    assert (read_model.calls, intake.registered, intake.revoked) == (0, [], [])


async def test_unknown_credential_is_401_invalid_with_no_read_or_write() -> None:
    app, intake, read_model = _app()
    async with _client(app) as client:
        response = await client.get(PREFIX, headers={"x-api-key": "not-a-key"})
    assert response.status_code == 401
    assert response.json()["code"] == "auth.credential.invalid"
    assert (read_model.calls, intake.registered, intake.revoked) == (0, [], [])


async def test_identity_backend_failure_is_503_with_no_read_or_write() -> None:
    app, intake, read_model = _app(identity=FailingTenantIdentity())
    async with _client(app) as client:
        response = await client.get(PREFIX, headers={"x-api-key": KEY_A})
    assert response.status_code == 503
    assert response.json() == {"detail": "identity resolution unavailable"}
    assert (read_model.calls, intake.registered, intake.revoked) == (0, [], [])


async def test_revoking_another_tenants_ref_is_404_and_never_reaches_the_publisher() -> (
    None
):
    app, intake, _ = _app()
    async with _client(app) as client:
        other = await client.delete(f"{PREFIX}/{REF_B}", headers={"x-api-key": KEY_A})
        missing = await client.delete(
            f"{PREFIX}/cred_does_not_exist", headers={"x-api-key": KEY_A}
        )
    assert other.status_code == missing.status_code == 404
    # Indistinguishable from a ref that does not exist.
    assert (
        other.json() == missing.json() == {"detail": "inference credential not found"}
    )
    assert intake.revoked == []


async def test_list_never_shows_another_tenants_ref() -> None:
    app, _, _ = _app()
    async with _client(app) as client:
        response = await client.get(PREFIX, headers={"x-api-key": KEY_B})
    assert [row["api_key_ref"] for row in response.json()] == [REF_B]


async def test_list_degrades_an_absent_relation_to_an_empty_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _app(read_model=FakeReadModel(mode="absent"))
    with caplog.at_level(logging.ERROR):
        async with _client(app) as client:
            response = await client.get(PREFIX, headers={"x-api-key": KEY_A})
    assert response.status_code == 200
    assert response.json() == []
    assert "relation is absent" in caplog.text


async def test_list_does_not_call_an_unconfigured_database_healthy() -> None:
    app, _, _ = _app(read_model=FakeReadModel(mode="not_configured"))
    async with _client(app) as client:
        response = await client.get(PREFIX, headers={"x-api-key": KEY_A})
    assert response.status_code == 503
    assert response.json() == {"detail": "failed to list inference credentials"}


async def test_list_read_failure_is_500() -> None:
    app, _, _ = _app(read_model=FakeReadModel(mode="broken"))
    async with _client(app) as client:
        response = await client.get(PREFIX, headers={"x-api-key": KEY_A})
    assert response.status_code == 500
    assert response.json() == {"detail": "failed to list inference credentials"}


@pytest.mark.parametrize("mode", ["absent", "not_configured", "broken"])
async def test_revoke_fails_closed_on_every_ownership_read_failure(mode: str) -> None:
    # Read degrades; authorization does not: an absent relation never becomes a revoke.
    app, intake, _ = _app(read_model=FakeReadModel(mode=mode))
    async with _client(app) as client:
        response = await client.delete(
            f"{PREFIX}/{REF_A}", headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 500
    assert response.json() == {"detail": "failed to look up inference credential"}
    assert intake.revoked == []


async def test_revoke_publisher_failure_is_503() -> None:
    app, _, _ = _app(intake=FakeIntake(fail=RuntimeError("store down")))
    async with _client(app) as client:
        response = await client.delete(
            f"{PREFIX}/{REF_A}", headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "failed to revoke inference credential"}


async def test_submit_intake_failure_is_503_and_logs_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _app(intake=FakeIntake(fail=RuntimeError("store down")))
    with caplog.at_level(logging.DEBUG):
        async with _client(app) as client:
            response = await client.post(
                PREFIX, content=CREATE_BODY, headers={"x-api-key": KEY_A}
            )
    assert response.status_code == 503
    assert response.json() == {"detail": "failed to register inference credential"}
    assert SECRET_VALUE not in response.text
    assert SECRET_VALUE not in caplog.text


async def test_submit_refused_key_is_422_with_the_outcome() -> None:
    refusal = CredentialKeyRefusedError(
        "openrouter", "rejected", "the provider refused"
    )
    app, _, _ = _app(intake=FakeIntake(fail=refusal))
    async with _client(app) as client:
        response = await client.post(
            PREFIX, content=CREATE_BODY, headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 422
    assert response.json() == {"detail": "the provider refused", "outcome": "rejected"}


async def test_submit_undetermined_plan_is_422_naming_the_plans() -> None:
    undetermined = CredentialPlanUndeterminedError(
        "anthropic", "inconclusive", ("api", "max")
    )
    app, _, _ = _app(intake=FakeIntake(fail=undetermined))
    async with _client(app) as client:
        response = await client.post(
            PREFIX, content=CREATE_BODY, headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 422
    assert response.json()["plans"] == ["api", "max"]
    assert response.json()["outcome"] == "inconclusive"


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b'{"name": "", "provider": "openrouter", "key_value": "'
        + SECRET_VALUE.encode()
        + b'"}',
        b'{"name": "x", "provider": "bad provider", "key_value": "'
        + SECRET_VALUE.encode()
        + b'"}',
        b'{"name": "x", "provider": "openrouter", "key_value": "'
        + SECRET_VALUE.encode()
        + b'", "surprise": 1}',
    ],
)
async def test_submit_with_an_invalid_body_is_422_and_never_echoes_the_key(
    content: bytes,
) -> None:
    app, intake, _ = _app()
    async with _client(app) as client:
        response = await client.post(
            PREFIX, content=content, headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 422
    assert SECRET_VALUE not in response.text
    assert intake.registered == []


async def test_overlong_revoke_ref_is_422_with_no_read() -> None:
    app, intake, read_model = _app()
    async with _client(app) as client:
        response = await client.delete(
            f"{PREFIX}/{'x' * 301}", headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 422
    assert (read_model.calls, intake.revoked) == (0, [])
