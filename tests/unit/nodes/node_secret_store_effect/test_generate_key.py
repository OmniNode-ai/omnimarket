# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generated keys are born in the runtime and stored create-only, value unseen (OMN-23695).

The value is ``prefix + token_source(bytes)`` of the contract's ``generated_keys``
entry: a test injects the token source, so it holds the value it returned and can
prove the store holds it while the result, the logs and ``str(result)`` never do.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_generate_key import (
    HandlerGenerateKey,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PROJECT,
    running_fake_store,
)

pytestmark = pytest.mark.unit

_ID = "r8_probe_tenant_key"
_FOLDER = "/shared/env"
_KEY = "R8_PROBE_TENANT_API_KEY"
_BYTES = 32
_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)


def _host(tmp_path: Path, address: str) -> Path:
    """A home whose env file carries the store identity (copied from test_house_model_key)."""
    home = tmp_path / "home"
    (home / ".omnibase").mkdir(parents=True)
    lines = [
        f"INFISICAL_ADDR={address}",
        f"INFISICAL_PROJECT_ID={PROJECT}",
        "INFISICAL_ENVIRONMENT_SLUG=dev",
        f"INFISICAL_CLIENT_ID={CLIENT_ID}",
        f"INFISICAL_CLIENT_SECRET={CLIENT_SECRET}",
    ]
    env_file = home / ".omnibase" / ".env"
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    return home


def _handler(
    tmp_path: Path,
    address: str,
    *,
    token_source,
    store_handler: HandlerSecretStore | None = None,
) -> HandlerGenerateKey:
    home = _host(tmp_path, address)
    return HandlerGenerateKey(
        store_handler=store_handler,
        onex_home=tmp_path / "onex_home",
        home=home,
        token_source=token_source,
    )


def _no_long_substring(value: str, text: str) -> bool:
    """True when no substring of ``value`` longer than 8 characters occurs in ``text``."""
    return all(value[i : i + 9] not in text for i in range(len(value) - 8))


def _assert_value_absent(value: str, result, caplog_text: str) -> None:
    dumped = result.model_dump_json()
    assert value not in dumped
    assert value not in repr(result)
    assert value not in str(result)
    assert value not in caplog_text
    assert _no_long_substring(value, dumped)
    assert _no_long_substring(value, repr(result))
    assert _no_long_substring(value, str(result))
    assert _no_long_substring(value, caplog_text)


async def test_golden_generated_key_is_created_and_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        result = await _handler(
            tmp_path, address, token_source=lambda _n: token
        ).handle(ModelGenerateKeyRequest(name=_ID))
    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert result.key_length == len(token)
    assert result.folder == _FOLDER
    assert result.key == _KEY
    assert state.secrets[(_FOLDER, _KEY)] == token
    _assert_value_absent(token, result, caplog.text)


async def test_a_second_call_reports_exists_and_leaves_the_stored_value_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        first = secrets.token_urlsafe(_BYTES)
        second = secrets.token_urlsafe(_BYTES)
        handler = _handler(tmp_path, address, token_source=lambda _n: first)
        created = await handler.handle(ModelGenerateKeyRequest(name=_ID))
        handler._token_source = lambda _n: second
        again = await handler.handle(ModelGenerateKeyRequest(name=_ID))
    assert created.outcome is EnumSecretStoreOutcome.CREATED
    assert again.outcome is EnumSecretStoreOutcome.EXISTS
    assert state.secrets[(_FOLDER, _KEY)] == first
    assert second not in state.secrets[(_FOLDER, _KEY)]
    assert again.key_length is None
    _assert_value_absent(first, created, caplog.text)
    _assert_value_absent(second, again, caplog.text)


async def test_a_fixed_marker_value_is_stored_but_never_serialized_or_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    marker = "r8-probe-tenant-key-marker-9f8a-do-not-leak"
    with running_fake_store() as (address, state):
        result = await _handler(
            tmp_path, address, token_source=lambda _n: marker
        ).handle(ModelGenerateKeyRequest(name=_ID))
    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert state.secrets[(_FOLDER, _KEY)] == marker
    _assert_value_absent(marker, result, caplog.text)


async def test_an_id_the_contract_does_not_declare_is_invalid_request_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        marker = "zz-not-declared-marker-77aa"
        result = await _handler(
            tmp_path, address, token_source=lambda _n: marker
        ).handle(ModelGenerateKeyRequest(name="not_declared_key"))
    assert result.outcome is EnumSecretStoreOutcome.INVALID_REQUEST
    assert result.key_length is None
    assert state.secrets == {}
    _assert_value_absent(marker, result, caplog.text)


class _UnreachableStore:
    """A store seam that answers every request ``unreachable``."""

    async def get_secret(self, key: str) -> str | None:
        raise ConnectionError("unreachable")

    async def set_secret(self, key: str, value: str) -> bool:
        raise ConnectionError("unreachable")

    async def delete_secret(self, key: str) -> bool:
        raise ConnectionError("unreachable")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        raise ConnectionError("unreachable")


class _UnreachableHandler(HandlerSecretStore):
    """A store handler whose every request answers the typed ``unreachable`` outcome."""

    def __init__(self) -> None:
        super().__init__(store=_UnreachableStore())

    async def handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        return ModelSecretStoreResult(
            operation=request.operation,
            outcome=EnumSecretStoreOutcome.UNREACHABLE,
            detail="the store could not be reached",
            correlation_id=request.correlation_id,
        )


async def test_a_store_that_cannot_be_reached_is_a_typed_outcome_with_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        result = await _handler(
            tmp_path,
            address,
            token_source=lambda _n: "Zq9fWm3Xp7LkTv2N",
            store_handler=_UnreachableHandler(),
        ).handle(ModelGenerateKeyRequest(name=_ID))
    assert result.outcome is EnumSecretStoreOutcome.UNREACHABLE
    assert result.key_length is None
    assert state.secrets == {}
    _assert_value_absent("Zq9fWm3Xp7LkTv2N", result, caplog.text)


@pytest.mark.parametrize("name", ["R8_PROBE_TENANT_KEY", "", "9key", "r8 probe"])
def test_the_request_refuses_an_uppercase_or_empty_name(name: str) -> None:
    with pytest.raises(ValidationError):
        ModelGenerateKeyRequest(name=name)


def test_the_contract_declares_the_generated_key_and_the_generate_key_operation() -> (
    None
):
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    block = raw["generated_keys"][_ID]
    assert block["folder"] == _FOLDER
    assert block["key"] == _KEY
    assert int(block["bytes"]) >= 24
    operations = [h["operation"] for h in raw["handler_routing"]["handlers"]]
    assert "generate_key" in operations
