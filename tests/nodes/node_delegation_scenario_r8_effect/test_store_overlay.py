# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R8 finds its secret store through its own overlay document (OMN-23746).

The host's environment file carries the store identity and a plain-http address that is refused (it is not loopback and
is listed nowhere). The R8 overlay document carries the reachable store block. The golden runs resolve and generate the
tenant key only because the overlay wins; the failing control is the same home and store with no overlay, which cannot
reach the store; the error chain shows an undeclared name and a missing document storing nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_settings import (
    SecretStoreSettingsReader,
    load_store_overlay,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    NODE_NAME,
    r8_settings_store,
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

FOLDER = "/shared/env"
KEY = "R8_PROBE_TENANT_API_KEY"
GENERATED_ID = "r8_probe_tenant_key"
UNREACHABLE = "http://192.0.2.1:8880"
SEEDED = "synthetic-seeded-probe-key-0123456789"
GENERATED = "synthetic-generated-probe-key-abcdefghij"


def _home(tmp_path: Path, address: str) -> Path:
    """A home whose env file carries the store identity and ``address`` as the store address."""
    home = tmp_path / "home"
    (home / ".omnibase").mkdir(parents=True)
    env_file = home / ".omnibase" / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"INFISICAL_ADDR={address}",
                f"INFISICAL_PROJECT_ID={PROJECT}",
                "INFISICAL_ENVIRONMENT_SLUG=dev",
                f"INFISICAL_CLIENT_ID={CLIENT_ID}",
                f"INFISICAL_CLIENT_SECRET={CLIENT_SECRET}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return home


def _overlay_root(tmp_path: Path, document: dict[str, object] | None) -> dict[str, str]:
    """An overlay root holding the R8 document (when given) and the environment that names it."""
    root = tmp_path / "overlays"
    root.mkdir()
    if document is not None:
        (root / NODE_NAME).mkdir()
        (root / NODE_NAME / "overlay.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _store_block(address: str) -> dict[str, object]:
    return {
        "secret_store": {
            "provider": "infisical",
            "address": address,
            "project_id": PROJECT,
            "environment": "dev",
            "identity_file": ".omnibase/.env",
            "client_id_ref": "INFISICAL_CLIENT_ID",
            "client_secret_ref": "INFISICAL_CLIENT_SECRET",
            "root_folder": "/",
        }
    }


def _no_long_substring(value: str, text: str) -> bool:
    return all(value[i : i + 9] not in text for i in range(len(value) - 8))


async def test_reader_resolves_through_the_overlay_store_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, state):
        state.secrets[(FOLDER, KEY)] = SEEDED
        monkeypatch.setenv("HOME", str(_home(tmp_path, UNREACHABLE)))
        environ = _overlay_root(tmp_path, _store_block(address))
        value = await r8_settings_store(environ).read(FOLDER, KEY)
    assert value is not None
    assert value.get_secret_value() == SEEDED


async def test_failing_control_without_the_overlay_the_env_file_address_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (_address, state):
        state.secrets[(FOLDER, KEY)] = SEEDED
        home = _home(tmp_path, UNREACHABLE)
        monkeypatch.setenv("HOME", str(home))
        value = await SecretStoreSettingsReader(
            onex_home=tmp_path / "onex_home", home=home
        ).read(FOLDER, KEY)
    assert value is None


async def test_generate_key_stores_through_the_overlay_and_never_prints_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with running_fake_store() as (address, state):
        home = _home(tmp_path, UNREACHABLE)
        monkeypatch.setenv("HOME", str(home))
        environ = _overlay_root(tmp_path, _store_block(address))
        result = await HandlerGenerateKey(
            overlay=load_store_overlay(NODE_NAME, environ),
            home=home,
            onex_home=tmp_path / "onex_home",
            token_source=lambda _n: GENERATED,
        ).handle(ModelGenerateKeyRequest(name=GENERATED_ID))
        stored = state.secrets.get((FOLDER, KEY))
    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert stored is not None
    assert stored.endswith(GENERATED)
    for text in (result.model_dump_json(), repr(result), str(result), caplog.text):
        assert _no_long_substring(stored, text)


async def test_generate_key_without_the_overlay_is_misconfigured_error_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (_address, state):
        home = _home(tmp_path, UNREACHABLE)
        monkeypatch.setenv("HOME", str(home))
        result = await HandlerGenerateKey(
            home=home,
            onex_home=tmp_path / "onex_home",
            token_source=lambda _n: GENERATED,
        ).handle(ModelGenerateKeyRequest(name=GENERATED_ID))
        held = dict(state.secrets)
    assert result.outcome is not EnumSecretStoreOutcome.CREATED
    assert held == {}


async def test_undeclared_name_with_the_overlay_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, state):
        home = _home(tmp_path, UNREACHABLE)
        monkeypatch.setenv("HOME", str(home))
        environ = _overlay_root(tmp_path, _store_block(address))
        result = await HandlerGenerateKey(
            overlay=load_store_overlay(NODE_NAME, environ),
            home=home,
            onex_home=tmp_path / "onex_home",
        ).handle(ModelGenerateKeyRequest(name="not_declared"))
        held = dict(state.secrets)
    assert result.outcome is EnumSecretStoreOutcome.INVALID_REQUEST
    assert held == {}


async def test_no_overlay_document_keeps_the_env_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_store() as (address, state):
        state.secrets[(FOLDER, KEY)] = SEEDED
        monkeypatch.setenv("HOME", str(_home(tmp_path, address)))
        environ = _overlay_root(tmp_path, None)
        assert load_store_overlay(NODE_NAME, environ) is None
        value = await r8_settings_store(environ).read(FOLDER, KEY)
    assert value is not None
    assert value.get_secret_value() == SEEDED


def test_a_broken_overlay_document_refuses_loudly(tmp_path: Path) -> None:
    broken = _store_block("http://127.0.0.1:1")
    del broken["secret_store"]["provider"]  # type: ignore[attr-defined]
    environ = _overlay_root(tmp_path, broken)
    with pytest.raises(NodeOverlayError) as raised:
        load_store_overlay(NODE_NAME, environ)
    assert "127.0.0.1" not in str(raised.value)
    assert "secret_store.provider" in str(raised.value)
