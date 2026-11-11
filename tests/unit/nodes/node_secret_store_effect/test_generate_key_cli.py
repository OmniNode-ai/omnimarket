# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The generate-key CLI prints the typed result only, never the value (OMN-23695).

The value is ``prefix + token_source(bytes)`` of the contract's ``generated_keys``
entry: a test injects the token source, so it holds the value it returned and can
prove the store holds it while stdout, stderr and the result never do.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

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
from omnimarket.nodes.node_secret_store_effect.runtime_generate_key_cli import main
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


def _host(tmp_path: Path, address: str) -> Path:
    """A home whose env file carries the store identity."""
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


def test_golden_generated_key_is_created_and_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        token = secrets.token_urlsafe(_BYTES)
        handler = _handler(tmp_path, address, token_source=lambda _n: token)
        code = main(["--name", _ID], handler=handler)
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["outcome"] == "created"
    assert payload["key_length"] == len(token)
    assert payload["folder"] == _FOLDER
    assert payload["key"] == _KEY
    assert state.secrets[(_FOLDER, _KEY)] == token
    assert token not in captured.out
    assert token not in captured.err
    assert _no_long_substring(token, captured.out)
    assert _no_long_substring(token, captured.err)


def test_a_second_call_reports_exists_and_leaves_the_stored_value_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        first = secrets.token_urlsafe(_BYTES)
        second = secrets.token_urlsafe(_BYTES)
        handler = _handler(tmp_path, address, token_source=lambda _n: first)
        code = main(["--name", _ID], handler=handler)
        cap1 = capsys.readouterr()
        handler._token_source = lambda _n: second
        code2 = main(["--name", _ID], handler=handler)
        cap2 = capsys.readouterr()
    assert code == 0
    assert code2 == 0
    payload = json.loads(cap2.out)
    assert payload["outcome"] == "exists"
    assert state.secrets[(_FOLDER, _KEY)] == first
    assert second not in state.secrets[(_FOLDER, _KEY)]
    assert first not in cap1.out
    assert first not in cap1.err
    assert first not in cap2.out
    assert first not in cap2.err
    assert _no_long_substring(first, cap1.out)
    assert _no_long_substring(first, cap1.err)
    assert _no_long_substring(first, cap2.out)
    assert _no_long_substring(first, cap2.err)


def test_an_id_the_contract_does_not_declare_is_invalid_request_and_stores_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        marker = "zz-not-declared-marker-77aa"
        handler = _handler(tmp_path, address, token_source=lambda _n: marker)
        code = main(["--name", "not_declared_key"], handler=handler)
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "invalid_request"
    assert state.secrets == {}
    assert marker not in captured.err
    assert _no_long_substring(marker, captured.err)


class _UnreachableHandler(HandlerSecretStore):
    """A store handler whose every request answers the typed ``unreachable`` outcome."""

    async def handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        return ModelSecretStoreResult(
            operation=request.operation,
            outcome=EnumSecretStoreOutcome.UNREACHABLE,
            detail="the store could not be reached",
            correlation_id=request.correlation_id,
        )


def test_a_store_that_cannot_be_reached_is_a_typed_outcome_with_no_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_fake_store() as (address, state):
        handler = _handler(
            tmp_path,
            address,
            token_source=lambda _n: "Zq9fWm3Xp7LkTv2N",
            store_handler=_UnreachableHandler(),
        )
        code = main(["--name", _ID], handler=handler)
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["outcome"] == "unreachable"
    assert state.secrets == {}
    assert "Zq9fWm3Xp7LkTv2N" not in captured.err
    assert _no_long_substring("Zq9fWm3Xp7LkTv2N", captured.err)
