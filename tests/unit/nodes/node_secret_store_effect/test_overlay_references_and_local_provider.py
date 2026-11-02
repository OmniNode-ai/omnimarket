# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's overlay reads: per-machine reference paths, a local provider, an identity file (OMN-21921)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreOverlay,
    ModelSecretStoreRequest,
)
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PLANTED,
    PROJECT,
    running_fake_store,
)

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_PROVIDER_REFERENCE = f"secret://onex/provider/{_REF}"  # onex-topic-allow: a secret reference, not a topic


class _Store:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def get_secret(self, key: str) -> str | None:
        self.reads.append(key)
        return PLANTED

    async def set_secret(self, key: str, value: str) -> bool:
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def _overlay(**fields: object) -> ModelSecretStoreOverlay:
    return ModelSecretStoreOverlay.model_validate({"provider": "fake", **fields})


async def _resolve(
    handler: HandlerSecretStore,
) -> tuple[EnumSecretStoreOutcome, str | None]:
    result = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE, reference=_PROVIDER_REFERENCE
        )
    )
    return result.outcome, result.key


async def test_the_contract_provider_folder_holds_a_declared_name_by_default() -> None:
    store = _Store()
    handler = HandlerSecretStore(store=store, overlay=_overlay())
    assert await _resolve(handler) == (EnumSecretStoreOutcome.RESOLVED, _REF)
    assert store.reads == [f"/provider-credentials/{_REF}"]


async def test_an_overlay_reference_points_a_declared_name_at_the_stores_own_path() -> (
    None
):
    store = _Store()
    handler = HandlerSecretStore(
        store=store,
        overlay=_overlay(
            root_folder="/dev",
            references={_REF: "/lab-provider-keys/OPENROUTER_API_KEY"},
        ),
    )
    assert await _resolve(handler) == (
        EnumSecretStoreOutcome.RESOLVED,
        "OPENROUTER_API_KEY",
    )
    assert store.reads == ["/dev/lab-provider-keys/OPENROUTER_API_KEY"]


async def test_an_overlay_reference_cannot_remap_a_namespace_the_contract_does_not_open() -> (
    None
):
    store = _Store()
    handler = HandlerSecretStore(
        store=store, overlay=_overlay(references={"cred_x": "/elsewhere/KEY"})
    )
    await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            reference="secret://onex/tenant/cred_x",  # onex-topic-allow: a secret reference, not a topic
        )
    )
    assert store.reads == ["/tenant-inference-credentials/cred_x"]


async def test_a_malformed_overlay_reference_is_misconfigured_and_reads_nothing() -> (
    None
):
    store = _Store()
    handler = HandlerSecretStore(
        store=store, overlay=_overlay(references={_REF: "/a/../KEY/"})
    )
    outcome, _ = await _resolve(handler)
    assert outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert store.reads == []


async def test_no_overlay_block_is_not_configured_unless_the_caller_names_one(
    tmp_path: Path,
) -> None:
    plain = HandlerSecretStore(onex_home=tmp_path)
    assert plain.configured_provider() is None
    assert (await _resolve(plain))[0] is EnumSecretStoreOutcome.NOT_CONFIGURED

    named = HandlerSecretStore(onex_home=tmp_path, unconfigured_overlay=_overlay())
    assert named.configured_provider() == "fake"


async def test_a_local_provider_overlay_needs_no_identity_and_reads_the_local_database() -> (
    None
):
    await LocalByokCredentialStore().set_secret(_REF, PLANTED)
    handler = HandlerSecretStore(overlay=ModelSecretStoreOverlay(provider="local"))
    result = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE, reference=_PROVIDER_REFERENCE
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert result.value is not None
    assert result.value.get_secret_value() == PLANTED
    assert PLANTED not in result.model_dump_json()


async def test_a_local_provider_create_is_create_only() -> None:
    from pydantic import SecretStr

    handler = HandlerSecretStore(overlay=ModelSecretStoreOverlay(provider="local"))
    create = ModelSecretStoreRequest(
        operation=EnumSecretStoreOperation.CREATE,
        reference=_PROVIDER_REFERENCE,
        value=SecretStr("first"),
    )
    assert (await handler.handle(create)).outcome is EnumSecretStoreOutcome.CREATED
    assert (await handler.handle(create)).outcome is EnumSecretStoreOutcome.EXISTS
    assert await LocalByokCredentialStore().get_secret(_REF) == "first"


async def test_an_infisical_overlay_missing_its_addressing_is_misconfigured(
    tmp_path: Path,
) -> None:
    handler = HandlerSecretStore(
        overlay=ModelSecretStoreOverlay(
            provider="infisical", address="http://127.0.0.1:9"
        ),
        onex_home=tmp_path,
    )
    result = await handler.handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST)
    )
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert "project_id" in (result.detail or "")


def _config(tmp_path: Path, address: str, identity_file: str) -> Path:
    home = tmp_path / ".onex"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "secret_store": {
                    "provider": "infisical",
                    "address": address,
                    "project_id": PROJECT,
                    "environment": "dev",
                    "client_id_ref": "ID",
                    "client_secret_ref": "SECRET",
                    "identity_file": identity_file,
                }
            }
        )
    )
    return home


@pytest.mark.parametrize(
    ("mode", "identity_file", "outcome"),
    [
        (0o600, "identity.env", EnumSecretStoreOutcome.LISTED),
        (0o644, "identity.env", EnumSecretStoreOutcome.MISCONFIGURED),
        (0o600, "absent.env", EnumSecretStoreOutcome.MISCONFIGURED),
        (0o600, "../identity.env", EnumSecretStoreOutcome.MISCONFIGURED),
        (0o600, "/etc/identity.env", EnumSecretStoreOutcome.MISCONFIGURED),
    ],
)
async def test_an_identity_file_is_read_only_when_owner_only_and_under_the_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    identity_file: str,
    outcome: EnumSecretStoreOutcome,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with running_fake_store() as (address, state):
        env_file = tmp_path / "identity.env"
        env_file.write_text(f"ID={CLIENT_ID}\n# SECRET=nope\nSECRET={CLIENT_SECRET}\n")
        env_file.chmod(mode)
        home = _config(tmp_path, address, identity_file)
        result = await HandlerSecretStore(onex_home=home).handle(
            ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST)
        )
        assert result.outcome is outcome
        assert CLIENT_SECRET not in (result.detail or "")
        if outcome is EnumSecretStoreOutcome.MISCONFIGURED:
            assert state.logins == 0
