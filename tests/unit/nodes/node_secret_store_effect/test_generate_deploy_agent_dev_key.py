# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev deploy agent's signing key is born in the runtime, value unseen (OMN-24611).

The value is ``prefix + token_source(bytes)`` of the contract's
``generated_keys`` entry ``deploy_agent_dev_signing_key``: a test injects the
token source, so it holds the value it returned and can prove the store holds
it while the result, the logs and ``str(result)`` never do. This is the
dev-only key of the dev deploy agent; the live agent's key is a different name
and is never read or written here.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_generate_key import (
    HandlerGenerateKey,
)
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PROJECT,
    running_fake_store,
)

pytestmark = pytest.mark.unit

_ID = "deploy_agent_dev_signing_key"
_FOLDER = "/shared/env"
_KEY = "DEPLOY_AGENT_DEV_HMAC_SECRET"
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


def _handler(
    tmp_path: Path,
    address: str,
    *,
    token_source,
    contract_path: Path | None = None,
) -> HandlerGenerateKey:
    home = _host(tmp_path, address)
    return HandlerGenerateKey(
        onex_home=tmp_path / "onex_home",
        home=home,
        contract_path=contract_path,
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


def _contract_without_the_entry(tmp_path: Path) -> Path:
    """A copy of the contract whose ``generated_keys`` lacks the dev signing key."""
    raw: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    del raw["generated_keys"][_ID]
    copy = tmp_path / "contract_without_entry.yaml"
    copy.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return copy


async def test_golden_dev_signing_key_is_created_and_never_printed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        result = await _handler(
            tmp_path, address, token_source=lambda _n: token
        ).handle(ModelGenerateKeyRequest(name=_ID))
    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert result.key_length == len(token)
    assert result.key_length > 32
    assert result.folder == _FOLDER
    assert result.key == _KEY
    assert state.secrets[(_FOLDER, _KEY)] == token
    _assert_value_absent(token, result, caplog.text)


async def test_a_second_call_reports_exists_and_leaves_the_stored_value_untouched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
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


async def test_a_contract_without_the_entry_is_invalid_request_and_stores_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        result = await _handler(
            tmp_path,
            address,
            token_source=lambda _n: token,
            contract_path=_contract_without_the_entry(tmp_path),
        ).handle(ModelGenerateKeyRequest(name=_ID))
    assert result.outcome is EnumSecretStoreOutcome.INVALID_REQUEST
    assert result.key_length is None
    assert state.secrets == {}
    _assert_value_absent(token, result, caplog.text)


@pytest.mark.parametrize("name", ["DEPLOY_AGENT_DEV_HMAC_SECRET", "", "9key", "a b"])
def test_the_request_refuses_an_uppercase_or_empty_name(name: str) -> None:
    with pytest.raises(ValidationError):
        ModelGenerateKeyRequest(name=name)


def test_the_contract_entry_is_32_bytes_with_no_prefix() -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    block = raw["generated_keys"][_ID]
    assert block["folder"] == _FOLDER
    assert block["key"] == _KEY
    assert int(block["bytes"]) >= 32
    assert block["prefix"] == ""
