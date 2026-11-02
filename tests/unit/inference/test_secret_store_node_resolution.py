# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation keys resolve through node_secret_store_effect (OMN-21921).

A contract-declared provider reference (``llm.<provider>.<name>``) and a minted
tenant credential used to be answered by this machine's own database alone. They
are now one ``resolve`` request to the secret store node, so the machine's
private overlay (``<onex_home>/config.yaml``) decides the backend: a store on a
lab host, the machine's own database on a customer laptop. The tests drive the
resolver exactly as production does: overlay in a config file, identity in a
file, the store over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.inference.secret_store_resolver import (
    LocalSecretNotRegisteredError,
    SecretResolutionError,
    api_key_ref_available,
    resolve_api_key_with_source,
)
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretStoreRefusedError,
)
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PLANTED,
    PROJECT,
    FakeState,
    running_fake_store,
)

pytestmark = pytest.mark.unit

_PROVIDER_REF = "llm.openrouter.api_key"
_UNMAPPED_REF = "llm.glm.api_key"
_TENANT_REF = "cred_localinstall_openrouter_0123456789abcdef0123456789abcdef"
_LOCAL_VALUE = "local-db-value-77c1-do-not-leak"
_MAPPED_PATH = "/lab-provider-keys/OPENROUTER_API_KEY"


@pytest.fixture
def served() -> Iterator[tuple[str, FakeState]]:
    with running_fake_store() as store:
        yield store


@pytest.fixture
def onex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty ONEX home that is also the home the identity file is found under."""
    monkeypatch.setattr(
        "omnibase_infra.doctor.delegation_doctor_support.default_onex_home",
        lambda: tmp_path / ".onex",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".onex").mkdir()
    return tmp_path / ".onex"


def _store_overlay(home: Path, address: str, *, via: str = "identity_file") -> None:
    lines = [
        "secret_store:",
        "  provider: infisical",
        f"  address: {address}",
        f"  project_id: {PROJECT}",
        "  environment: dev",
        "  client_id_ref: STORE_CLIENT_ID",
        "  client_secret_ref: STORE_CLIENT_SECRET",
        "  references:",
        f"    {_PROVIDER_REF}: {_MAPPED_PATH}",
    ]
    identity = {"STORE_CLIENT_ID": CLIENT_ID, "STORE_CLIENT_SECRET": CLIENT_SECRET}
    if via == "identity_file":
        lines.append("  identity_file: lab-identity.env")
        env_file = home.parent / "lab-identity.env"
        env_file.write_text(
            "".join(f"export {name}='{value}'\n" for name, value in identity.items())
        )
        env_file.chmod(0o600)
    else:
        creds = home / "credentials.json"
        creds.write_text(json.dumps(identity))
        creds.chmod(0o600)
    (home / "config.yaml").write_text("\n".join(lines) + "\n")


def _local_overlay(home: Path) -> None:
    (home / "config.yaml").write_text("secret_store:\n  provider: local\n")


def _register_locally(ref: str) -> None:
    asyncio.run(LocalByokCredentialStore().set_secret(ref, _LOCAL_VALUE))


@pytest.mark.parametrize("identity", ["identity_file", "credentials_json"])
def test_secret_store_overlay_resolves_declared_ref_with_local_entry_absent(
    onex_home: Path, served: tuple[str, FakeState], identity: str
) -> None:
    address, state = served
    state.secrets[("/lab-provider-keys", "OPENROUTER_API_KEY")] = PLANTED
    _store_overlay(onex_home, address, via=identity)

    value, source = resolve_api_key_with_source(_PROVIDER_REF)

    assert value is not None
    assert value.get_secret_value() == PLANTED
    assert source is EnumSecretSource.LANE_SECRET_STORE
    assert asyncio.run(LocalByokCredentialStore().get_secret(_PROVIDER_REF)) is None


def test_secret_store_overlay_unmapped_declared_ref_uses_the_contract_folder(
    onex_home: Path, served: tuple[str, FakeState]
) -> None:
    address, state = served
    state.secrets[("/provider-credentials", _UNMAPPED_REF)] = PLANTED
    _store_overlay(onex_home, address)

    value, _ = resolve_api_key_with_source(_UNMAPPED_REF)

    assert value is not None
    assert value.get_secret_value() == PLANTED


def test_secret_store_overlay_resolves_a_tenant_ref_from_the_tenant_folder(
    onex_home: Path, served: tuple[str, FakeState]
) -> None:
    address, state = served
    state.secrets[("/tenant-inference-credentials", _TENANT_REF)] = PLANTED
    _store_overlay(onex_home, address)

    value, source = resolve_api_key_with_source(_TENANT_REF)

    assert value is not None
    assert value.get_secret_value() == PLANTED
    assert source is EnumSecretSource.LANE_SECRET_STORE


def test_secret_store_overlay_naming_the_local_store_resolves_from_it(
    onex_home: Path, served: tuple[str, FakeState]
) -> None:
    _, state = served
    _local_overlay(onex_home)
    _register_locally(_PROVIDER_REF)

    value, source = resolve_api_key_with_source(_PROVIDER_REF)

    assert value is not None
    assert value.get_secret_value() == _LOCAL_VALUE
    assert source is EnumSecretSource.LOCAL_STORE
    assert state.logins == 0


def test_secret_store_without_any_overlay_is_the_local_store(onex_home: Path) -> None:
    assert not (onex_home / "config.yaml").exists()
    _register_locally(_TENANT_REF)

    value, source = resolve_api_key_with_source(_TENANT_REF)

    assert value is not None
    assert value.get_secret_value() == _LOCAL_VALUE
    assert source is EnumSecretSource.LOCAL_STORE


def test_secret_store_overlay_decides_so_a_local_entry_does_not_answer_a_store_machine(
    onex_home: Path, served: tuple[str, FakeState]
) -> None:
    address, _ = served
    _store_overlay(onex_home, address)
    _register_locally(_PROVIDER_REF)

    with pytest.raises(LocalSecretNotRegisteredError, match=r"llm\.openrouter"):
        resolve_api_key_with_source(_PROVIDER_REF)


@pytest.mark.parametrize("overlay", ["store", "local", "none"])
def test_secret_store_refuses_fail_closed_typed_when_no_backend_holds_a_value(
    onex_home: Path,
    served: tuple[str, FakeState],
    monkeypatch: pytest.MonkeyPatch,
    overlay: str,
) -> None:
    address, _ = served
    if overlay == "store":
        _store_overlay(onex_home, address)
    elif overlay == "local":
        _local_overlay(onex_home)
    monkeypatch.setenv("LLM_OPENROUTER_API_KEY", "env-value-must-not-answer")

    with pytest.raises(LocalSecretNotRegisteredError) as refused:
        resolve_api_key_with_source(_PROVIDER_REF)

    assert isinstance(refused.value, SecretResolutionError)
    assert "env-value-must-not-answer" not in str(refused.value)
    assert resolve_api_key_with_source(_PROVIDER_REF, required=False) == (None, None)


def test_secret_store_unreachable_is_a_typed_refusal_not_an_absent_key(
    onex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_overlay(onex_home, "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_OPENROUTER_API_KEY", "env-value-must-not-answer")

    with pytest.raises(SecretStoreRefusedError) as refused:
        resolve_api_key_with_source(_PROVIDER_REF)

    assert refused.value.outcome == "unreachable"
    assert not isinstance(refused.value, LocalSecretNotRegisteredError)
    assert api_key_ref_available(_PROVIDER_REF) is False


def test_secret_store_misconfigured_overlay_is_a_typed_refusal(
    onex_home: Path,
) -> None:
    (onex_home / "config.yaml").write_text(
        "secret_store:\n  provider: infisical\n  address: http://127.0.0.1:9\n"
    )

    with pytest.raises(SecretStoreRefusedError) as refused:
        resolve_api_key_with_source(_PROVIDER_REF)

    assert refused.value.outcome == "misconfigured"


def test_secret_store_value_is_in_no_environment_log_or_serialized_result(
    onex_home: Path,
    served: tuple[str, FakeState],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    address, state = served
    state.secrets[("/lab-provider-keys", "OPENROUTER_API_KEY")] = PLANTED
    _store_overlay(onex_home, address)

    value, _ = resolve_api_key_with_source(_PROVIDER_REF)

    assert value is not None
    assert PLANTED not in repr(value)
    assert PLANTED not in str(value)
    assert all(PLANTED not in v for v in os.environ.values())
    assert all(PLANTED not in r.getMessage() for r in caplog.records)
    assert all(CLIENT_SECRET not in r.getMessage() for r in caplog.records)


def test_secret_store_resolution_path_names_no_product_and_no_path() -> None:
    """The resolver's branch and the key store carry no product name and no store path."""
    root = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
    key_store = (
        root
        / "nodes"
        / "node_secret_store_effect"
        / "handlers"
        / "delegation_key_store.py"
    )
    handler = key_store.with_name("handler_secret_store.py")
    for source in (key_store, handler):
        text = source.read_text(encoding="utf-8").lower()
        for forbidden in (
            "infisical",
            "lab-provider-keys",
            "openrouter_api_key",
            "tenant-inference-credentials",
            "/provider-credentials",
        ):
            assert forbidden not in text, f"{source.name} names {forbidden!r}"
