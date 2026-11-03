# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""House model keys resolve from the secret store on a host with no local copy (OMN-22417).

The host here has no ``secret_store`` block, no ``~/.claude-zai/settings.json``
and no ``LLM_GLM_API_KEY`` in its env file: the only place the value exists is
the store, so a test that resolves it fails the moment the store call is removed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.secret_store.model_house_model_key_request import (
    ModelHouseModelKeyRequest,
)
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretStoreRefusedError,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HOUSE_KEY_FOLDER,
    HandlerHouseModelKey,
    house_key_reference,
    require_house_model_key,
)
from omnimarket.nodes.node_secret_store_effect.runtime_house_model_key_cli import main
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PLANTED,
    PROJECT,
    FakeState,
    running_fake_store,
)

pytestmark = pytest.mark.unit

_NAME = "LLM_GLM_API_KEY"


def _host(tmp_path: Path, address: str, *, local_key: bool = False) -> Path:
    """A home whose env file carries the store identity and, only if asked, a local model key."""
    home = tmp_path / "home"
    (home / ".omnibase").mkdir(parents=True)
    lines = [
        f"INFISICAL_ADDR={address}",
        f"INFISICAL_PROJECT_ID={PROJECT}",
        "INFISICAL_ENVIRONMENT_SLUG=dev",
        f"INFISICAL_CLIENT_ID={CLIENT_ID}",
        f"INFISICAL_CLIENT_SECRET={CLIENT_SECRET}",
    ]
    if local_key:
        lines.append(f"{_NAME}=local-copy-must-never-be-used")
    env_file = home / ".omnibase" / ".env"
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    return home


def _seed(state: FakeState) -> None:
    state.secrets[(HOUSE_KEY_FOLDER, _NAME)] = PLANTED


def _handler(home: Path, tmp_path: Path) -> HandlerHouseModelKey:
    return HandlerHouseModelKey(onex_home=tmp_path / "onex_home", home=home)


async def test_a_host_with_no_local_key_reads_it_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, state):
        _seed(state)
        home = _host(tmp_path, address)
        monkeypatch.setenv("HOME", str(home))
        result = await _handler(home, tmp_path).handle(
            ModelHouseModelKeyRequest(name=_NAME)
        )
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert result.value is not None
    assert result.value.get_secret_value() == PLANTED
    assert result.value_length == len(PLANTED)
    assert result.identity_source.value == "host_env_file"
    assert state.logins == 1


async def test_the_serialized_result_never_carries_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, state):
        _seed(state)
        home = _host(tmp_path, address)
        monkeypatch.setenv("HOME", str(home))
        result = await _handler(home, tmp_path).handle(
            ModelHouseModelKeyRequest(name=_NAME)
        )
    assert PLANTED not in result.model_dump_json()
    assert PLANTED not in repr(result)
    assert json.loads(result.model_dump_json())["value_length"] == len(PLANTED)


async def test_a_key_the_store_lacks_is_a_refusal_naming_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        monkeypatch.setenv("HOME", str(home))
        with pytest.raises(SecretStoreRefusedError) as raised:
            await require_house_model_key(_NAME, handler=_handler(home, tmp_path))
    assert house_key_reference(_NAME) in str(raised.value)
    assert raised.value.outcome == EnumSecretStoreOutcome.NOT_FOUND.value


async def test_a_local_copy_of_the_key_is_never_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address, local_key=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv(_NAME, "env-copy-must-never-be-used")
        with pytest.raises(SecretStoreRefusedError):
            await require_house_model_key(_NAME, handler=_handler(home, tmp_path))


async def test_a_host_naming_no_store_is_not_configured_never_a_silent_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "bare_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(SecretStoreRefusedError) as raised:
        await require_house_model_key(_NAME, handler=_handler(home, tmp_path))
    assert raised.value.outcome == EnumSecretStoreOutcome.NOT_CONFIGURED.value
    assert house_key_reference(_NAME) in str(raised.value)


async def test_an_identity_file_open_to_others_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, state):
        _seed(state)
        home = _host(tmp_path, address)
        (home / ".omnibase" / ".env").chmod(0o644)
        monkeypatch.setenv("HOME", str(home))
        result = await _handler(home, tmp_path).handle(
            ModelHouseModelKeyRequest(name=_NAME)
        )
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert result.value is None


@pytest.mark.parametrize(
    "name", ["OPENAI_KEY", "LLM_GLM_API_KEY/../x", "llm_glm_api_key", "LLM__API_KEY"]
)
def test_only_a_house_model_key_name_is_accepted(name: str) -> None:
    with pytest.raises(ValidationError):
        ModelHouseModelKeyRequest(name=name)


def test_the_house_folder_is_not_a_customer_folder() -> None:
    assert HOUSE_KEY_FOLDER == "/shared/env"
    assert "tenant" not in house_key_reference(_NAME)


def test_the_cli_prints_the_length_and_not_the_value_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        _seed(state)
        home = _host(tmp_path, address)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("ONEX_HOME", str(tmp_path / "onex_home"))
        code = main(["--name", _NAME])
    out = capsys.readouterr()
    assert code == 0
    assert PLANTED not in out.out + out.err
    assert json.loads(out.out)["value_length"] == len(PLANTED)


def test_the_cli_value_only_pipe_carries_the_value_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        _seed(state)
        home = _host(tmp_path, address)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("ONEX_HOME", str(tmp_path / "onex_home"))
        code = main(["--name", _NAME, "--value-only"])
    assert code == 0
    assert capsys.readouterr().out == PLANTED


def test_the_cli_refusal_exits_3_with_nothing_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("ONEX_HOME", str(tmp_path / "onex_home"))
        code = main(["--name", _NAME, "--value-only"])
    out = capsys.readouterr()
    assert code == 3
    assert out.out == ""
    assert house_key_reference(_NAME) in out.err
