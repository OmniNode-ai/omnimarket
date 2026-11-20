# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mint signs exactly the two proof tenants' tokens with the lab test key, read by its declared name.

Each token is decoded with the test key's public half: the claims are tenant_id, tenant_slug, audience gateway-attach
and a one-hour expiry. A third tenant name is refused before the key is read. A name that already holds a value is left
as it is.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import jwt
import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_dev_proof_tenant_token_effect.handlers import (
    HandlerDevProofTenantToken,
    ModelDevProofTokenCommand,
    ModelDevProofTokenResult,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenOutcome,
    EnumDevProofTokenStatus,
)

from .fakes import (
    H051,
    H201,
    ID_H051,
    ID_H201,
    SECRET_H051,
    SECRET_H201,
    SIGNING_FOLDER,
    SIGNING_NAME,
    FakeKeyReader,
    FakeSeeder,
    ec_pair,
    overlay,
    rsa_pair,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


def _run(
    command: ModelDevProofTokenCommand,
    *,
    algorithm: str = "RS256",
    pair: tuple[str, str] | None = None,
    issuer: str | None = None,
    seeder: FakeSeeder | None = None,
) -> tuple[ModelDevProofTokenResult, FakeKeyReader, FakeSeeder]:
    private, _ = pair or rsa_pair()
    reader = FakeKeyReader(private)
    seeder = seeder or FakeSeeder()
    handler = HandlerDevProofTenantToken(
        overlay=overlay(algorithm=algorithm, issuer=issuer),
        key_reader=reader,
        seeder=seeder,
        clock=lambda: _NOW,
    )
    return asyncio.run(handler.handle(command)), reader, seeder


def _decode(token: str, public: str, algorithm: str) -> dict[str, object]:
    return jwt.decode(
        token,
        public,
        algorithms=[algorithm],
        audience="gateway-attach",
        options={"verify_exp": False},
    )


def test_dev_proof_token_mint_claims_of_both_tenants() -> None:
    result, reader, seeder = _run(ModelDevProofTokenCommand(lane="dev", write=True))
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    assert reader.calls == [(SIGNING_FOLDER, SIGNING_NAME)]
    assert sorted(seeder.store) == sorted([SECRET_H051, SECRET_H201])
    _, public = rsa_pair()
    h051 = _decode(seeder.store[SECRET_H051], public, "RS256")
    h201 = _decode(seeder.store[SECRET_H201], public, "RS256")
    now = int(_NOW.timestamp())
    assert h051 == {
        "tenant_id": ID_H051,
        "tenant_slug": H051,
        "aud": "gateway-attach",
        "iat": now,
        "exp": now + 3600,
    }
    assert h201 == {
        "tenant_id": ID_H201,
        "tenant_slug": H201,
        "aud": "gateway-attach",
        "iat": now,
        "exp": now + 3600,
    }
    assert h051["tenant_id"] != h201["tenant_id"]


def test_dev_proof_token_mint_claims_signature_is_the_test_keys_only() -> None:
    _, _, seeder = _run(ModelDevProofTokenCommand(lane="dev", write=True))
    token = seeder.store[SECRET_H051]
    other_public = ec_pair()[1]
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(token, other_public, algorithms=["RS256"], audience="gateway-attach")
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(
            token,
            rsa_pair()[1],
            algorithms=["RS256"],
            audience="some-other-audience",
            options={"verify_exp": False},
        )


def test_dev_proof_token_mint_claims_with_es256_and_issuer() -> None:
    result, _, seeder = _run(
        ModelDevProofTokenCommand(lane="dev", write=True),
        algorithm="ES256",
        pair=ec_pair(),
        issuer="test-issuer",
    )
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    claims = jwt.decode(
        seeder.store[SECRET_H201],
        ec_pair()[1],
        algorithms=["ES256"],
        audience="gateway-attach",
        issuer="test-issuer",
        options={"verify_exp": False},
    )
    assert claims["tenant_slug"] == H201
    assert claims["tenant_id"] == ID_H201


@pytest.mark.parametrize("tenant", ["cli-run-z", "cli-run-ax", "", "CLI-RUN-A"])
def test_dev_proof_token_mint_claims_refuses_a_third_tenant(tenant: str) -> None:
    result, reader, seeder = _run(
        ModelDevProofTokenCommand(lane="dev", tenants=(H051, tenant), write=True)
    )
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_TENANT_NOT_ALLOWED
    assert reader.calls == []
    assert seeder.exists_calls == []
    assert seeder.seed_calls == []


def test_dev_proof_token_mint_claims_one_named_tenant_signs_one_token() -> None:
    result, _, seeder = _run(
        ModelDevProofTokenCommand(lane="dev", tenants=(H201,), write=True)
    )
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    assert list(seeder.store) == [SECRET_H201]


def test_dev_proof_token_mint_claims_overlay_with_a_third_tenant_is_refused() -> None:
    private, _ = rsa_pair()
    reader = FakeKeyReader(private)
    seeder = FakeSeeder()
    third = overlay().model_copy(
        update={
            "tenants": [
                *overlay().tenants,
                overlay()
                .tenants[0]
                .model_copy(
                    update={
                        "tenant_slug": "cli-run-z",
                        "tenant_id": "x",
                        "secret_name": "TEST_TOKEN_H999",
                    }
                ),
            ]
        }
    )
    handler = HandlerDevProofTenantToken(
        overlay=third, key_reader=reader, seeder=seeder
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_TENANT_NOT_ALLOWED
    assert reader.calls == []
    assert seeder.seed_calls == []


def test_dev_proof_token_mint_claims_never_overwrites_a_held_name() -> None:
    held = {SECRET_H051: "already-held-value"}
    result, _, seeder = _run(
        ModelDevProofTokenCommand(lane="dev", write=True), seeder=FakeSeeder(held)
    )
    statuses = {t.secret_name: t.status for t in result.tokens}
    assert statuses == {
        SECRET_H051: EnumDevProofTokenStatus.PRESENT,
        SECRET_H201: EnumDevProofTokenStatus.WRITTEN,
    }
    assert seeder.store[SECRET_H051] == "already-held-value"
    assert seeder.seed_calls == [SECRET_H201]


def test_dev_proof_token_mint_claims_read_only_signs_and_writes_nothing() -> None:
    result, reader, seeder = _run(ModelDevProofTokenCommand(lane="dev", write=False))
    assert result.outcome is EnumDevProofTokenOutcome.READY
    assert {t.status for t in result.tokens} == {EnumDevProofTokenStatus.ABSENT}
    assert len(reader.calls) == 1
    assert seeder.seed_calls == []


def test_dev_proof_token_mint_claims_missing_key_is_typed_and_writes_nothing() -> None:
    reader = FakeKeyReader(None)
    seeder = FakeSeeder()
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=reader, seeder=seeder
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.SIGNING_KEY_UNAVAILABLE
    assert result.signing_key_name == SIGNING_NAME
    assert seeder.seed_calls == []


def test_dev_proof_token_mint_claims_a_one_line_escaped_key_still_signs() -> None:
    private, public = rsa_pair()
    escaped = private.strip().replace("\n", "\\n")
    reader = FakeKeyReader(escaped)
    seeder = FakeSeeder()
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=reader, seeder=seeder
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    assert _decode(seeder.store[SECRET_H051], public, "RS256")["tenant_slug"] == H051


def test_dev_proof_token_mint_claims_signer_receives_the_key_as_a_secret() -> None:
    seen: list[object] = []

    class _Signer:
        def sign(self, claims: object, key: SecretStr, algorithm: str) -> SecretStr:
            seen.append(key)
            return SecretStr("signed")

    reader = FakeKeyReader(rsa_pair()[0])
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=reader, seeder=FakeSeeder(), signer=_Signer()
    )
    asyncio.run(handler.handle(ModelDevProofTokenCommand(lane="dev", write=True)))
    assert seen
    assert all(isinstance(k, SecretStr) for k in seen)
