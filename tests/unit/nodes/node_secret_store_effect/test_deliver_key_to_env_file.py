# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delivery writes the generated key into the env file, value unseen (OMN-24611).

The chain is generate then deliver: a test injects the token source, so it
holds the value it returned and can prove the file holds exactly one
``DEPLOY_AGENT_HMAC_SECRET=<value>`` line while the result, the logs and
``str(result)`` never do. Every refusal writes nothing.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.models.secret_store.model_deliver_key_request import (
    ModelDeliverKeyRequest,
)
from omnimarket.models.secret_store.model_deliver_key_result import (
    EnumDeliverKeyOutcome,
)
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
from omnimarket.nodes.node_secret_store_effect.handlers.handler_deliver_key import (
    HandlerDeliverKeyToEnvFile,
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

_ID = "deploy_agent_dev_signing_key"
_STORED_KEY = "DEPLOY_AGENT_DEV_HMAC_SECRET"
_ENV_NAME = "DEPLOY_AGENT_HMAC_SECRET"
_RELATIVE = ".omnibase/deploy-agent-dev-signing-key.env"
_BYTES = 32
_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)


def _host(tmp_path: Path, address: str) -> Path:
    """A home whose env file carries the store identity (copied from test_generate_key)."""
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


def _generator(tmp_path: Path, home: Path, token: str) -> HandlerGenerateKey:
    return HandlerGenerateKey(
        onex_home=tmp_path / "onex_home",
        home=home,
        token_source=lambda _n: token,
    )


def _delivery_handler(
    tmp_path: Path,
    home: Path,
    *,
    store_handler: HandlerSecretStore | None = None,
    contract_path: Path | None = None,
) -> HandlerDeliverKeyToEnvFile:
    return HandlerDeliverKeyToEnvFile(
        store_handler=store_handler,
        onex_home=tmp_path / "onex_home",
        home=home,
        contract_path=contract_path,
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


class _StubStoreHandler(HandlerSecretStore):
    """A store seam that resolves any ask with the value the test holds."""

    def __init__(self, value: str) -> None:
        super().__init__()
        self._value = value

    async def handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        return ModelSecretStoreResult(
            operation=request.operation,
            outcome=EnumSecretStoreOutcome.RESOLVED,
            folder=request.folder,
            key=request.key,
            value=SecretStr(self._value),
            correlation_id=request.correlation_id,
        )


def _contract_copy(tmp_path: Path, mutate) -> Path:
    """A copy of the contract with one entry mutated by ``mutate(raw)``."""
    raw: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    mutate(raw)
    copy = tmp_path / "contract_copy.yaml"
    copy.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return copy


async def test_golden_generate_then_deliver_writes_the_line_and_never_prints_the_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = await _generator(tmp_path, home, token).handle(
            ModelGenerateKeyRequest(name=_ID)
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        result = await _delivery_handler(tmp_path, home).handle(
            ModelDeliverKeyRequest(name=_ID)
        )
    target = home / _RELATIVE
    assert result.outcome is EnumDeliverKeyOutcome.DELIVERED
    assert result.env_name == _ENV_NAME
    assert result.home_relative_path == _RELATIVE
    assert result.key_length == len(token)
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={token}\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert state.secrets[("/shared/env", _STORED_KEY)] == token
    _assert_value_absent(token, result, caplog.text)


async def test_a_second_delivery_is_unchanged_and_does_not_rewrite_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = await _generator(tmp_path, home, token).handle(
            ModelGenerateKeyRequest(name=_ID)
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        handler = _delivery_handler(tmp_path, home)
        first = await handler.handle(ModelDeliverKeyRequest(name=_ID))
        target = home / _RELATIVE
        before = os.stat(target)
        second = await handler.handle(ModelDeliverKeyRequest(name=_ID))
        after = os.stat(target)
    assert first.outcome is EnumDeliverKeyOutcome.DELIVERED
    assert second.outcome is EnumDeliverKeyOutcome.UNCHANGED
    assert second.key_length == len(token)
    assert before.st_ino == after.st_ino
    assert before.st_mtime_ns == after.st_mtime_ns
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={token}\n"
    assert state.secrets[("/shared/env", _STORED_KEY)] == token
    _assert_value_absent(token, second, caplog.text)


async def test_an_unchanged_file_left_at_a_wider_mode_is_corrected_to_the_declared_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = await _generator(tmp_path, home, token).handle(
            ModelGenerateKeyRequest(name=_ID)
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        target = home / _RELATIVE
        target.write_text(f"{_ENV_NAME}={token}\n", encoding="utf-8")
        target.chmod(0o644)
        result = await _delivery_handler(tmp_path, home).handle(
            ModelDeliverKeyRequest(name=_ID)
        )
    assert result.outcome is EnumDeliverKeyOutcome.UNCHANGED
    assert result.detail == "the file mode was corrected to the declared mode"
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={token}\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert state.secrets[("/shared/env", _STORED_KEY)] == token
    _assert_value_absent(token, result, caplog.text)


async def test_an_unchanged_file_at_the_declared_mode_stays_unchanged_with_no_detail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = await _generator(tmp_path, home, token).handle(
            ModelGenerateKeyRequest(name=_ID)
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        target = home / _RELATIVE
        target.write_text(f"{_ENV_NAME}={token}\n", encoding="utf-8")
        target.chmod(0o600)
        result = await _delivery_handler(tmp_path, home).handle(
            ModelDeliverKeyRequest(name=_ID)
        )
    assert result.outcome is EnumDeliverKeyOutcome.UNCHANGED
    assert result.detail is None
    assert target.stat().st_mode & 0o777 == 0o600
    assert state.secrets[("/shared/env", _STORED_KEY)] == token
    _assert_value_absent(token, result, caplog.text)


async def test_a_file_at_a_wider_mode_with_a_stale_value_is_delivered_and_ends_at_the_declared_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = await _generator(tmp_path, home, token).handle(
            ModelGenerateKeyRequest(name=_ID)
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        target = home / _RELATIVE
        target.write_text(f"{_ENV_NAME}=synthetic_stale_value\n", encoding="utf-8")
        target.chmod(0o640)
        result = await _delivery_handler(tmp_path, home).handle(
            ModelDeliverKeyRequest(name=_ID)
        )
    assert result.outcome is EnumDeliverKeyOutcome.DELIVERED
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={token}\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert state.secrets[("/shared/env", _STORED_KEY)] == token
    _assert_value_absent(token, result, caplog.text)


@pytest.mark.parametrize("breaking", ["stat", "chmod"])
async def test_a_stat_or_chmod_failure_on_an_unchanged_file_is_write_failed_naming_the_class_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    breaking: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    value = "synthetic_stored_value_for_the_mode_enforcement_check"
    home = tmp_path / "home"
    target = home / _RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(f"{_ENV_NAME}={value}\n", encoding="utf-8")
    target.chmod(0o644)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("the mode check was refused")

    monkeypatch.setattr(os, breaking, refuse)
    result = await _delivery_handler(
        tmp_path, home, store_handler=_StubStoreHandler(value)
    ).handle(ModelDeliverKeyRequest(name=_ID))
    monkeypatch.undo()
    assert result.outcome is EnumDeliverKeyOutcome.WRITE_FAILED
    assert result.detail == "PermissionError"
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={value}\n"
    assert target.stat().st_mode & 0o777 == 0o644
    _assert_value_absent(value, result, caplog.text)


async def test_a_store_value_replaced_since_is_delivered_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        first = secrets.token_urlsafe(_BYTES)
        second = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generator = _generator(tmp_path, home, first)
        generated = await generator.handle(ModelGenerateKeyRequest(name=_ID))
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        handler = _delivery_handler(tmp_path, home)
        original = await handler.handle(ModelDeliverKeyRequest(name=_ID))
        assert original.outcome is EnumDeliverKeyOutcome.DELIVERED
        # The store value is replaced: the old key is removed and generated anew.
        del state.secrets[("/shared/env", _STORED_KEY)]
        generator._token_source = lambda _n: second
        rotated = await generator.handle(ModelGenerateKeyRequest(name=_ID))
        assert rotated.outcome is EnumSecretStoreOutcome.CREATED
        replaced = await handler.handle(ModelDeliverKeyRequest(name=_ID))
    target = home / _RELATIVE
    assert replaced.outcome is EnumDeliverKeyOutcome.DELIVERED
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={second}\n"
    assert first not in target.read_text(encoding="utf-8")
    _assert_value_absent(first, original, caplog.text)
    _assert_value_absent(second, replaced, caplog.text)


async def test_an_id_the_contract_does_not_declare_is_not_declared_and_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        result = await _delivery_handler(tmp_path, home).handle(
            ModelDeliverKeyRequest(name="not_declared_key")
        )
    assert result.outcome is EnumDeliverKeyOutcome.NOT_DECLARED
    assert result.env_name is None
    assert result.key_length is None
    assert not (home / _RELATIVE).exists()


async def test_a_delivery_whose_key_id_is_not_generated_is_not_declared(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def rename_key_id(raw: dict[str, Any]) -> None:
        raw["env_file_deliveries"][_ID]["key_id"] = "no_such_generated_key"

    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        copy = _contract_copy(tmp_path, rename_key_id)
        result = await _delivery_handler(tmp_path, home, contract_path=copy).handle(
            ModelDeliverKeyRequest(name=_ID)
        )
    assert result.outcome is EnumDeliverKeyOutcome.NOT_DECLARED
    assert "no_such_generated_key" in (result.detail or "")
    assert not (home / _RELATIVE).exists()


async def test_a_store_without_the_key_is_key_unreadable_and_leaves_no_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, _state):  # the store holds nothing
        home = _host(tmp_path, address)
        result = await _delivery_handler(tmp_path, home).handle(
            ModelDeliverKeyRequest(name=_ID)
        )
    assert result.outcome is EnumDeliverKeyOutcome.KEY_UNREADABLE
    assert "not_found" in (result.detail or "")
    assert result.key_length is None
    parent = home / ".omnibase"
    assert not (home / _RELATIVE).exists()
    assert not [entry for entry in parent.iterdir() if entry.name.endswith(".tmp")]


@pytest.mark.parametrize(
    "unsafe",
    [
        "has a space in it",
        'has a "quote" in it',
        "multi\nline",
        "value\n",
        "value\r\n",
    ],
    ids=["space", "quote", "multi-line", "trailing-newline", "trailing-crlf"],
)
async def test_an_unsafe_value_is_value_unsafe_and_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, unsafe: str
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        result = await _delivery_handler(
            tmp_path, home, store_handler=_StubStoreHandler(unsafe)
        ).handle(ModelDeliverKeyRequest(name=_ID))
    assert result.outcome is EnumDeliverKeyOutcome.VALUE_UNSAFE
    assert result.key_length is None
    assert not (home / _RELATIVE).exists()
    parent = home / ".omnibase"
    assert not [entry for entry in parent.iterdir() if entry.name.endswith(".tmp")]
    _assert_value_absent(unsafe, result, caplog.text)


async def test_an_unwritable_parent_is_write_failed_naming_the_exception_class_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, _state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = await _generator(tmp_path, home, token).handle(
            ModelGenerateKeyRequest(name=_ID)
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        omnibase = home / ".omnibase"
        omnibase.chmod(0o500)
        try:
            result = await _delivery_handler(tmp_path, home).handle(
                ModelDeliverKeyRequest(name=_ID)
            )
        finally:
            omnibase.chmod(0o700)
    assert result.outcome is EnumDeliverKeyOutcome.WRITE_FAILED
    assert result.detail is not None
    # The class name alone: no path, no value, no exception text.
    assert result.detail in {"OSError", "PermissionError"}
    assert "/" not in result.detail
    assert token not in result.detail
    assert not (home / _RELATIVE).exists()
    _assert_value_absent(token, result, caplog.text)


@pytest.mark.parametrize("bad", ["../x", "/etc/deploy-agent-dev-signing-key.env"])
async def test_a_path_outside_the_home_is_refused_at_contract_load(
    tmp_path: Path, bad: str
) -> None:
    def move_path(raw: dict[str, Any]) -> None:
        raw["env_file_deliveries"][_ID]["home_relative_path"] = bad

    copy = _contract_copy(tmp_path, move_path)
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        with pytest.raises(ValueError, match="names a path outside the user's home"):
            _delivery_handler(tmp_path, home, contract_path=copy)
    assert not (home / _RELATIVE).exists()


@pytest.mark.parametrize(
    "mode", ["0644", "0660", "0777", "0000", "0200", "0100", "0300", "0500", "0700"]
)
async def test_a_mode_other_than_0600_or_0400_is_refused_at_contract_load(
    tmp_path: Path, mode: str
) -> None:
    def set_mode(raw: dict[str, Any]) -> None:
        raw["env_file_deliveries"][_ID]["mode"] = mode

    copy = _contract_copy(tmp_path, set_mode)
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        with pytest.raises(ValueError, match="mode other than 0600 or 0400"):
            _delivery_handler(tmp_path, home, contract_path=copy)
    assert not (home / _RELATIVE).exists()


async def test_a_mode_of_0400_is_accepted_at_contract_load_and_enforced(
    tmp_path: Path,
) -> None:
    def set_mode(raw: dict[str, Any]) -> None:
        raw["env_file_deliveries"][_ID]["mode"] = "0400"

    copy = _contract_copy(tmp_path, set_mode)
    value = "synthetic_stored_value_for_the_0400_check"
    home = tmp_path / "home"
    result = await _delivery_handler(
        tmp_path,
        home,
        store_handler=_StubStoreHandler(value),
        contract_path=copy,
    ).handle(ModelDeliverKeyRequest(name=_ID))
    assert result.outcome is EnumDeliverKeyOutcome.DELIVERED
    assert (home / _RELATIVE).stat().st_mode & 0o777 == 0o400


async def test_a_chmod_failure_after_the_replace_says_the_file_was_replaced(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    value = "synthetic_stored_value_for_the_chmod_after_replace_check"
    home = tmp_path / "home"
    target = home / _RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(f"{_ENV_NAME}=an_older_synthetic_value\n", encoding="utf-8")
    target.chmod(0o600)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("the chmod was refused")

    monkeypatch.setattr(os, "chmod", refuse)
    result = await _delivery_handler(
        tmp_path, home, store_handler=_StubStoreHandler(value)
    ).handle(ModelDeliverKeyRequest(name=_ID))
    monkeypatch.undo()
    assert result.outcome is EnumDeliverKeyOutcome.WRITE_FAILED
    assert result.detail is not None
    assert result.detail.startswith("PermissionError")
    assert "replaced" in result.detail
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={value}\n"
    assert not [e for e in target.parent.iterdir() if e.name.endswith(".tmp")]
    _assert_value_absent(value, result, caplog.text)
