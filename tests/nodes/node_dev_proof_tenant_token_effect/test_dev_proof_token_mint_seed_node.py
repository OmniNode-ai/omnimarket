# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each token is written only through node_secret_seed_effect's handler, and never over a held name.

The real seed handler runs over an in-memory store. A name the store lists is not written (the seed node upserts, so the
adapter asks its dry run first); an absent name is written once; the value goes by the handler's source reader, so no
source file appears on disk; the whole chain from key read to stored token verifies with the test key's public half.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jwt
import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_dev_proof_tenant_token_effect.adapters import (
    SecretSeedNodeSeeder,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.handlers import (
    HandlerDevProofTenantToken,
    ModelDevProofTokenCommand,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenOutcome,
    EnumDevProofTokenStatus,
)

from .fakes import (
    PROJECT_ID,
    SECRET_H051,
    SECRET_H201,
    FakeKeyReader,
    FakeSeedStore,
    overlay,
    rsa_pair,
)

pytestmark = pytest.mark.unit


def _seeder(
    store: FakeSeedStore, missing_auth: list[str] | None = None
) -> SecretSeedNodeSeeder:
    return SecretSeedNodeSeeder(
        host="https://secrets.example.invalid",
        project_id=PROJECT_ID,
        environment="dev",
        folder="/test/tokens",
        store_factory=lambda _request: store,
        auth_probe=lambda: missing_auth or [],
    )


def test_dev_proof_token_mint_seed_node_writes_an_absent_name_once() -> None:
    store = FakeSeedStore()
    status = asyncio.run(_seeder(store).seed("TEST_NAME", SecretStr("a-value-1234")))
    assert status is EnumDevProofTokenStatus.WRITTEN
    assert store.set_calls == ["TEST_NAME"]
    assert store.values == {"TEST_NAME": "a-value-1234"}


def test_dev_proof_token_mint_seed_node_never_overwrites_a_held_name() -> None:
    store = FakeSeedStore({"TEST_NAME": "held-value-1234"})
    status = asyncio.run(
        _seeder(store).seed("TEST_NAME", SecretStr("a-new-value-5678"))
    )
    assert status is EnumDevProofTokenStatus.PRESENT
    assert store.set_calls == []
    assert store.values == {"TEST_NAME": "held-value-1234"}


def test_dev_proof_token_mint_seed_node_exists_writes_nothing() -> None:
    store = FakeSeedStore({"TEST_NAME": "held-value-1234"})
    seeder = _seeder(store)
    assert asyncio.run(seeder.exists("TEST_NAME")) is True
    assert asyncio.run(seeder.exists("OTHER_NAME")) is False
    assert store.set_calls == []


def test_dev_proof_token_mint_seed_node_without_auth_raises_a_named_verdict() -> None:
    store = FakeSeedStore()
    with pytest.raises(RuntimeError, match="auth_unavailable"):
        asyncio.run(
            _seeder(store, ["INFISICAL_CLIENT_ID"]).seed("N", SecretStr("v-1234"))
        )
    assert store.set_calls == []


def test_dev_proof_token_mint_seed_node_leaves_no_source_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = FakeSeedStore()
    asyncio.run(_seeder(store).seed("TEST_NAME", SecretStr("a-value-1234")))
    assert list(tmp_path.iterdir()) == []


def test_dev_proof_token_mint_seed_node_end_to_end_verifies_with_the_public_half() -> (
    None
):
    store = FakeSeedStore()
    private, public = rsa_pair()
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=FakeKeyReader(private), seeder=_seeder(store)
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    assert store.set_calls == [SECRET_H051, SECRET_H201]
    claims = jwt.decode(
        store.values[SECRET_H051],
        public,
        algorithms=["RS256"],
        audience="gateway-attach",
    )
    assert claims["tenant_slug"] == "cli-run-a"
    again = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert {t.status for t in again.tokens} == {EnumDevProofTokenStatus.PRESENT}
    assert store.set_calls == [SECRET_H051, SECRET_H201]
