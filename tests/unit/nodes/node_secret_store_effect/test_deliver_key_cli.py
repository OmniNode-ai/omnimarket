# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deliver-key CLI prints the typed result only, never the value (OMN-24611).

The value is read from the store and written straight into the env file: a test
injects the token source of the generating handler, so it holds the value and
can prove the file holds exactly one ``DEPLOY_AGENT_HMAC_SECRET=<value>`` line
while stdout, stderr and the result never do.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.secret_store.model_deliver_key_result import (
    EnumDeliverKeyOutcome,
    ModelDeliverKeyResult,
)
from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect import (
    runtime_deliver_key_cli as cli_module,
)
from omnimarket.nodes.node_secret_store_effect.handlers import handler_deliver_key
from omnimarket.nodes.node_secret_store_effect.handlers.handler_deliver_key import (
    HandlerDeliverKeyToEnvFile,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_generate_key import (
    HandlerGenerateKey,
)
from omnimarket.nodes.node_secret_store_effect.runtime_deliver_key_cli import main
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PROJECT,
    running_fake_store,
)

pytestmark = pytest.mark.unit

_ID = "deploy_agent_dev_signing_key"
_ENV_NAME = "DEPLOY_AGENT_HMAC_SECRET"
_RELATIVE = ".omnibase/deploy-agent-dev-signing-key.env"
_BYTES = 32


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


def _delivery_handler(tmp_path: Path, home: Path) -> HandlerDeliverKeyToEnvFile:
    return HandlerDeliverKeyToEnvFile(
        onex_home=tmp_path / "onex_home",
        home=home,
    )


def _no_long_substring(value: str, text: str) -> bool:
    """True when no substring of ``value`` longer than 8 characters occurs in ``text``."""
    return all(value[i : i + 9] not in text for i in range(len(value) - 8))


def test_golden_delivery_is_delivered_and_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = asyncio.run(
            _generator(tmp_path, home, token).handle(ModelGenerateKeyRequest(name=_ID))
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        code = main(["--name", _ID], handler=_delivery_handler(tmp_path, home))
    captured = capsys.readouterr()
    target = home / _RELATIVE
    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["outcome"] == "delivered"
    assert payload["env_name"] == _ENV_NAME
    assert payload["home_relative_path"] == _RELATIVE
    assert payload["key_length"] == len(token)
    assert target.read_text(encoding="utf-8") == f"{_ENV_NAME}={token}\n"
    assert state.secrets[("/shared/env", "DEPLOY_AGENT_DEV_HMAC_SECRET")] == token
    assert token not in captured.out
    assert token not in captured.err
    assert _no_long_substring(token, captured.out)
    assert _no_long_substring(token, captured.err)


def test_a_second_call_reports_unchanged_and_never_prints_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, _state):
        token = secrets.token_urlsafe(_BYTES)
        home = _host(tmp_path, address)
        generated = asyncio.run(
            _generator(tmp_path, home, token).handle(ModelGenerateKeyRequest(name=_ID))
        )
        assert generated.outcome is EnumSecretStoreOutcome.CREATED
        handler = _delivery_handler(tmp_path, home)
        code = main(["--name", _ID], handler=handler)
        cap1 = capsys.readouterr()
        code2 = main(["--name", _ID], handler=handler)
        cap2 = capsys.readouterr()
    assert code == 0
    assert code2 == 0
    payload = json.loads(cap2.out)
    assert payload["outcome"] == "unchanged"
    assert payload["key_length"] == len(token)
    assert (home / _RELATIVE).read_text(encoding="utf-8") == f"{_ENV_NAME}={token}\n"
    assert token not in cap1.out
    assert token not in cap1.err
    assert token not in cap2.out
    assert token not in cap2.err
    assert _no_long_substring(token, cap1.out)
    assert _no_long_substring(token, cap1.err)
    assert _no_long_substring(token, cap2.out)
    assert _no_long_substring(token, cap2.err)


def test_an_id_the_contract_does_not_declare_is_not_declared_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, _state):
        home = _host(tmp_path, address)
        code = main(
            ["--name", "not_declared_key"], handler=_delivery_handler(tmp_path, home)
        )
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "not_declared"
    assert not (home / _RELATIVE).exists()


def test_a_store_without_the_key_is_key_unreadable_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, _state):  # the store holds nothing
        home = _host(tmp_path, address)
        code = main(["--name", _ID], handler=_delivery_handler(tmp_path, home))
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "key_unreadable"
    assert not (home / _RELATIVE).exists()


_OVERLAY_ID = "node_deliver_key_cli_probe"


def _overlay_root(tmp_path: Path, document: dict[str, Any] | None) -> Path:
    """An overlay root holding ``<id>/overlay.yaml`` with ``document``, or no file when None."""
    root = tmp_path / "overlay_root"
    root.mkdir()
    if document is not None:
        (root / _OVERLAY_ID).mkdir()
        (root / _OVERLAY_ID / "overlay.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return root


class _NeverBuilt:
    """Stands in for the handler class: building it at all is the failure."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the handler must not be built")


class _RecordingHandler:
    """Stands in for the handler class: records how the CLI built it and delivers."""

    built: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).built.append(kwargs)

    async def handle(self, request: Any) -> ModelDeliverKeyResult:
        return ModelDeliverKeyResult(
            name=request.name,
            outcome=EnumDeliverKeyOutcome.DELIVERED,
            key_length=1,
        )


def test_cli_a_named_overlay_that_loads_as_none_is_refused_not_the_host_env_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _overlay_root(tmp_path, None)  # no overlay file for the id: loads as None
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setattr(cli_module, "HandlerDeliverKeyToEnvFile", _NeverBuilt)
    code = main(["--name", _ID, "--overlay-id", _OVERLAY_ID])
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "key_unreadable"
    assert _OVERLAY_ID in payload["detail"]
    assert "Traceback" not in captured.err


def test_cli_a_named_overlay_that_loads_is_handed_to_the_handler(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _overlay_root(
        tmp_path,
        {"secret_store": {"provider": "infisical", "address": "http://127.0.0.1:1"}},
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    _RecordingHandler.built = []
    monkeypatch.setattr(cli_module, "HandlerDeliverKeyToEnvFile", _RecordingHandler)
    code = main(["--name", _ID, "--overlay-id", _OVERLAY_ID])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["outcome"] == "delivered"
    assert len(_RecordingHandler.built) == 1
    overlay = _RecordingHandler.built[0]["overlay"]
    assert overlay.provider == "infisical"


def test_cli_a_broken_named_overlay_is_exit_3_not_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _overlay_root(tmp_path, {"secret_store": {"provider": ""}})
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setattr(cli_module, "HandlerDeliverKeyToEnvFile", _NeverBuilt)
    code = main(["--name", _ID, "--overlay-id", _OVERLAY_ID])
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    assert json.loads(captured.err)["outcome"] == "key_unreadable"


def test_cli_real_construction_without_an_overlay_id_asks_the_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No injected handler and no overlay id: the real handler is built over the real contract."""
    code = main(["--name", "not_declared_key"])
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    assert json.loads(captured.err)["outcome"] == "not_declared"


@pytest.mark.parametrize("bad", ["Not A Name", "../etc", "UPPER", ""])
def test_cli_a_bad_name_is_exit_3_not_a_traceback(
    capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    code = main(["--name", bad], handler=None)
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "not_declared"


@pytest.mark.parametrize(
    "contract_text",
    [
        "- not\n- a mapping\n",
        "env_file_deliveries:\n  deploy_agent_dev_signing_key: broken\n",
        "env_file_deliveries:\n  deploy_agent_dev_signing_key:\n    key_id: x\n",
        "generated_keys: [unclosed\n",
    ],
    ids=["not-a-mapping", "entry-not-a-mapping", "entry-missing-keys", "invalid-yaml"],
)
def test_cli_a_malformed_contract_is_exit_3_not_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    contract_text: str,
) -> None:
    broken = tmp_path / "contract.yaml"
    broken.write_text(contract_text, encoding="utf-8")
    monkeypatch.setattr(handler_deliver_key, "_CONTRACT_PATH", broken)
    code = main(["--name", _ID])
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "not_declared"
    assert "Traceback" not in captured.err
