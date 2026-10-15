# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``register-tenant-key --store central`` writes through the secret store node (OMN-20994).

Operator rulings 2026-10-11T15:00:01Z and 2026-10-11T16:00:52Z: keys go in
through the script path and live in one central place, so every lab host
resolves them. ``--store central`` hands the value, in process, to the secret
store node's handler (create-only) under the minted reference in the tenant
folder, then publishes credential-registered. The local store is untouched. A
refusal from the node stores nothing, publishes nothing and prints the typed
outcome without the value.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import ModelSecretStoreOverlay
from omnimarket.projection import credential_publisher

pytestmark = pytest.mark.unit
_VALUE = "sk-test-central-secret-never-printed-or-published"
_TENANT = "omninode"
_TENANT_FOLDER = "/tenant-inference-credentials"


class FakeBus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        self.events.append(envelope.model_dump_json())


class FakeSecretStore:
    """A dict-backed ProtocolSecretStore that records every call."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})
        self.calls: list[tuple[str, str]] = []
        self.raise_on_set: BaseException | None = None

    async def get_secret(self, key: str) -> str | None:
        return self.secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        self.calls.append(("set", key))
        if self.raise_on_set is not None:
            raise self.raise_on_set
        if key in self.secrets:
            return False
        self.secrets[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def _overlay() -> ModelSecretStoreOverlay:
    return ModelSecretStoreOverlay(
        provider="fake",
        address="http://127.0.0.1:9",
        project_id="00000000-0000-0000-0000-000000000001",
        environment="dev",
        client_id_ref="id-ref",
        client_secret_ref="secret-ref",
    )


@pytest.fixture
def central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeSecretStore, FakeBus, LocalByokCredentialStore]:
    remote = FakeSecretStore()
    bus = FakeBus()
    local = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    monkeypatch.setattr(
        cli_secret,
        "_central_secret_handler",
        lambda: HandlerSecretStore(store=remote, overlay=_overlay()),
    )
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: local)
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    for name in (
        "ONEX_LAB_RUN_HOSTS",
        "ONEX_LANE_HOST",
        "ONEX_REMOTE_LANE_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    return remote, bus, local


def _register(*extra: str) -> Any:
    return CliRunner().invoke(
        cli_secret.secret_group,
        [
            "register-tenant-key",
            "glm",
            "--tenant",
            _TENANT,
            "--plan",
            "general_api",
            "--expires",
            "2099-01-01",
            "--store",
            "central",
            *extra,
        ],
        input=f"{_VALUE}\n",
    )


def test_register_tenant_key_central_writes_through_the_node_and_publishes(
    central: tuple[FakeSecretStore, FakeBus, LocalByokCredentialStore],
) -> None:
    remote, bus, local = central
    result = _register()
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    ref = shown["api_key_ref"]
    # The node maps the tenant namespace to the tenant folder; the value is
    # held there under the minted reference and nowhere else.
    assert remote.secrets == {f"{_TENANT_FOLDER}/{ref}": _VALUE}
    assert len(bus.events) == 1
    event = json.loads(bus.events[0])["payload"]
    assert event["api_key_ref"] == ref
    assert _VALUE not in bus.events[0]
    assert _VALUE not in result.output
    # AC1: the local SQLite store is untouched: never created, never read.
    assert not local.db_path.exists()


def test_register_tenant_key_central_is_the_only_store_choice_that_skips_local(
    central: tuple[FakeSecretStore, FakeBus, LocalByokCredentialStore],
) -> None:
    remote, _bus, local = central
    result = CliRunner().invoke(
        cli_secret.secret_group,
        [
            "register-tenant-key",
            "glm",
            "--tenant",
            _TENANT,
            "--plan",
            "general_api",
            "--expires",
            "2099-01-01",
        ],
        input=f"{_VALUE}\n",
    )
    assert result.exit_code == 0, result.output
    assert remote.calls == []
    assert local.db_path.exists()


def test_register_tenant_key_central_holds_no_folder_string() -> None:
    # AC3, caller side: the command holds a reference and never a folder.
    assert _TENANT_FOLDER not in inspect.getsource(cli_secret)


def test_register_tenant_key_central_refusal_key_exists(
    central: tuple[FakeSecretStore, FakeBus, LocalByokCredentialStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, bus, local = central
    monkeypatch.setattr(
        credential_publisher, "mint_api_key_ref", lambda *_args: "cred_fixed"
    )
    remote.secrets[f"{_TENANT_FOLDER}/cred_fixed"] = "original"
    result = _register()
    assert result.exit_code != 0
    assert "exists" in result.output
    assert "nothing was stored" in result.output.lower()
    assert remote.secrets == {f"{_TENANT_FOLDER}/cred_fixed": "original"}
    assert bus.events == []
    assert _VALUE not in result.output
    assert not local.db_path.exists()


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (PermissionError("secret store refused the identity"), "refused"),
        (ConnectionError("ConnectError"), "unreachable"),
    ],
)
def test_register_tenant_key_central_refusal_store_error(
    central: tuple[FakeSecretStore, FakeBus, LocalByokCredentialStore],
    error: BaseException,
    outcome: str,
) -> None:
    remote, bus, local = central
    remote.raise_on_set = error
    result = _register()
    assert result.exit_code != 0
    assert outcome in result.output
    assert remote.secrets == {}
    assert bus.events == []
    assert _VALUE not in result.output
    assert not local.db_path.exists()


def test_register_tenant_key_central_refusal_identity_unresolved(
    central: tuple[FakeSecretStore, FakeBus, LocalByokCredentialStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _remote, bus, local = central
    # No injected store and an empty ONEX home: the node has no secret_store
    # block and answers not_configured.
    monkeypatch.setattr(
        cli_secret,
        "_central_secret_handler",
        lambda: HandlerSecretStore(onex_home=tmp_path / "empty-onex-home"),
    )
    result = _register()
    assert result.exit_code != 0
    assert "not_configured" in result.output
    assert bus.events == []
    assert _VALUE not in result.output
    assert not local.db_path.exists()
