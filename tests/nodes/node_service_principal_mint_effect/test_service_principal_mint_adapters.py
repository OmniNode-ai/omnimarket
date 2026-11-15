# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for the service principal mint adapters, with injected fakes."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.driver_broker_acl import (
    RemoteRunner,
)
from omnimarket.nodes.node_service_principal_mint_effect.handlers.adapter_rpk_broker_login_admin import (
    RpkSshBrokerLoginAdmin,
)
from omnimarket.nodes.node_service_principal_mint_effect.handlers.adapter_secret_store_vault import (
    SecretStoreVault,
)
from omnimarket.nodes.node_service_principal_mint_effect.handlers.adapter_ssh_credential_delivery import (
    SshCredentialDelivery,
)
from omnimarket.nodes.node_service_principal_mint_effect.models.model_service_principal_mint import (
    ModelServicePrincipalBroker,
    ModelServicePrincipalDelivery,
)
from omnimarket.nodes.node_service_principal_mint_effect.protocols.protocol_service_principal_mint import (
    BrokerLoginAdminError,
    PrincipalVaultError,
)

pytestmark = pytest.mark.unit

SENTINEL = SecretStr("SENTINEL-PASSWORD-VALUE")


def _broker() -> ModelServicePrincipalBroker:
    return ModelServicePrincipalBroker(
        lane="lane-a",
        host="broker.test",
        compose_project="proj",
        broker_container="broker",
        superuser_source_container="superuser",
        ssh_target="broker.test",
        mechanism="SCRAM-SHA-256",
    )


def _fake_runner(stdout: str) -> tuple[RemoteRunner, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def runner(target: str, script: str) -> str:
        calls.append((target, script))
        return stdout

    return runner, calls


def test_list_logins_parses_stdout() -> None:
    stdout = "USERNAME  MECHANISM\nalice  SCRAM-SHA-256\nbob  SCRAM-SHA-256\n"
    runner, calls = _fake_runner(stdout)
    admin = RpkSshBrokerLoginAdmin(_broker(), runner=runner)

    import asyncio

    result = asyncio.run(admin.list_logins())

    assert result == ("alice", "bob")
    assert len(calls) == 1
    target, script = calls[0]
    assert target == _broker().ssh_target
    assert "security user list" in script
    assert _broker().broker_container in script
    assert _broker().superuser_source_container in script


def test_create_login_script_contains_mechanism() -> None:
    runner, calls = _fake_runner("")
    admin = RpkSshBrokerLoginAdmin(_broker(), runner=runner)

    import asyncio

    asyncio.run(admin.create_login("svc", SENTINEL))

    assert len(calls) == 1
    target, script = calls[0]
    assert target == _broker().ssh_target
    assert "security user create" in script
    assert "svc" in script
    assert "--mechanism" in script
    assert _broker().mechanism in script


def test_create_login_error_never_leaks_password() -> None:
    def raising_runner(target: str, script: str) -> str:
        raise RuntimeError(f"boom {SENTINEL.get_secret_value()}")

    admin = RpkSshBrokerLoginAdmin(_broker(), runner=raising_runner)

    import asyncio

    with pytest.raises(BrokerLoginAdminError) as excinfo:
        asyncio.run(admin.create_login("svc", SENTINEL))

    assert SENTINEL.get_secret_value() not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


class _FakeVaultHandler:
    def __init__(self, result: ModelSecretStoreResult) -> None:
        self._result = result
        self.requests: list[Any] = []

    async def handle(self, request: Any) -> ModelSecretStoreResult:
        self.requests.append(request)
        return self._result


def _vault_result(
    outcome: EnumSecretStoreOutcome,
    *,
    value: SecretStr | None = None,
) -> ModelSecretStoreResult:
    return ModelSecretStoreResult(
        operation=EnumSecretStoreOperation.RESOLVE,
        outcome=outcome,
        folder="/f",
        key="k",
        value=value,
    )


def test_vault_read_resolved_returns_secret() -> None:
    handler = _FakeVaultHandler(
        _vault_result(EnumSecretStoreOutcome.RESOLVED, value=SENTINEL)
    )
    vault = SecretStoreVault(handler=handler)

    import asyncio

    result = asyncio.run(vault.read("/f", "k"))

    assert result == SENTINEL
    assert handler.requests[0].operation is EnumSecretStoreOperation.RESOLVE


def test_vault_read_not_found_returns_none() -> None:
    handler = _FakeVaultHandler(_vault_result(EnumSecretStoreOutcome.NOT_FOUND))
    vault = SecretStoreVault(handler=handler)

    import asyncio

    result = asyncio.run(vault.read("/f", "k"))

    assert result is None


def test_vault_read_refused_raises() -> None:
    handler = _FakeVaultHandler(_vault_result(EnumSecretStoreOutcome.REFUSED))
    vault = SecretStoreVault(handler=handler)

    import asyncio

    with pytest.raises(PrincipalVaultError) as excinfo:
        asyncio.run(vault.read("/f", "k"))

    assert "refused" in str(excinfo.value)


def test_vault_create_created() -> None:
    handler = _FakeVaultHandler(_vault_result(EnumSecretStoreOutcome.CREATED))
    vault = SecretStoreVault(handler=handler)

    import asyncio

    result = asyncio.run(vault.create("/f", "k", SENTINEL))

    assert result == "created"
    req = handler.requests[0]
    assert req.operation is EnumSecretStoreOperation.CREATE
    assert req.folder == "/f"
    assert req.key == "k"
    assert req.value == SENTINEL


def test_vault_create_exists() -> None:
    handler = _FakeVaultHandler(_vault_result(EnumSecretStoreOutcome.EXISTS))
    vault = SecretStoreVault(handler=handler)

    import asyncio

    result = asyncio.run(vault.create("/f", "k", SENTINEL))

    assert result == "exists"


def test_vault_create_unreachable_raises() -> None:
    handler = _FakeVaultHandler(_vault_result(EnumSecretStoreOutcome.UNREACHABLE))
    vault = SecretStoreVault(handler=handler)

    import asyncio

    with pytest.raises(PrincipalVaultError):
        asyncio.run(vault.create("/f", "k", SENTINEL))


def _delivery_target(folder: str, owner: str) -> ModelServicePrincipalDelivery:
    return ModelServicePrincipalDelivery(
        host="local-test",
        ssh_target="unused",
        folder=folder,
        filename="x.env",
        owner=owner,
        mode="0400",
        privilege_argv=(),
    )


def test_ssh_credential_delivery_full_sequence(tmp_path: Path) -> None:
    folder = str(tmp_path / "creds")
    owner = f"{os.getuid()}:{os.getgid()}"
    target = _delivery_target(folder, owner)
    delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))
    content = SecretStr("USER=alice\nPASSWORD=SENTINEL-PASSWORD-VALUE\n")

    import asyncio

    # deliver(apply=False) on an empty folder -> would-write, folder does not exist
    reading = asyncio.run(delivery.deliver(target, content, apply=False))
    assert reading.state == "would-write"
    assert not Path(folder).exists()

    # deliver(apply=True) -> written, file exists, mode 0o400, content exact
    reading = asyncio.run(delivery.deliver(target, content, apply=True))
    assert reading.state == "written"
    dest = Path(folder) / "x.env"
    assert dest.exists()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o400
    assert dest.read_text() == content.get_secret_value()

    # deliver(apply=True) again -> unchanged
    reading = asyncio.run(delivery.deliver(target, content, apply=True))
    assert reading.state == "unchanged"

    # deliver with changed content apply=False -> would-write, file content unchanged
    changed = SecretStr("USER=alice\nPASSWORD=OTHER-VALUE\n")
    target2 = _delivery_target(folder, owner)
    reading = asyncio.run(delivery.deliver(target2, changed, apply=False))
    assert reading.state == "would-write"
    assert dest.read_text() == content.get_secret_value()

    # deliver with changed content apply=True -> written, content changed
    reading = asyncio.run(delivery.deliver(target2, changed, apply=True))
    assert reading.state == "written"
    assert dest.read_text() == changed.get_secret_value()


def test_ssh_credential_delivery_folder_under_regular_file(tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("data")
    folder = str(tmp_path / "afile" / "sub")
    owner = f"{os.getuid()}:{os.getgid()}"
    target = _delivery_target(folder, owner)
    delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))
    content = SecretStr("USER=alice\nPASSWORD=SENTINEL-PASSWORD-VALUE\n")

    import asyncio

    reading = asyncio.run(delivery.deliver(target, content, apply=True))
    assert reading.state == "failed"
    assert content.get_secret_value() not in reading.detail
    assert "SENTINEL-PASSWORD-VALUE" not in reading.detail


def test_ssh_credential_delivery_relative_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    owner = f"{os.getuid()}:{os.getgid()}"
    target = _delivery_target(".creds", owner)
    delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))
    content = SecretStr("USER=alice\nPASSWORD=SENTINEL-PASSWORD-VALUE\n")

    import asyncio

    reading = asyncio.run(delivery.deliver(target, content, apply=True))
    assert reading.state == "written"
    dest = tmp_path / ".creds" / "x.env"
    assert dest.exists()
    assert dest.read_text() == content.get_secret_value()


def test_ssh_credential_delivery_wrong_owner(tmp_path: Path) -> None:
    folder = str(tmp_path / "creds2")
    if os.getuid() != 0:
        owner = "0:0"
        target = _delivery_target(folder, owner)
        delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))
        content = SecretStr("USER=alice\nPASSWORD=SENTINEL-PASSWORD-VALUE\n")

        import asyncio

        reading = asyncio.run(delivery.deliver(target, content, apply=True))
        assert reading.state == "failed"
    else:
        owner = "12345:12345"
        target = _delivery_target(folder, owner)
        delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))
        content = SecretStr("USER=alice\nPASSWORD=SENTINEL-PASSWORD-VALUE\n")

        import asyncio

        reading = asyncio.run(delivery.deliver(target, content, apply=True))
        assert reading.owner == "12345:12345"
