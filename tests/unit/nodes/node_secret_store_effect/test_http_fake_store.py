# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node end to end against a local HTTP fake of the store's REST API (OMN-20944).

The fake speaks the universal-auth login and the raw secrets endpoints. These
tests drive the node exactly as a machine would: overlay in config.yaml,
identity in a 0600 credentials.json, the adapter selected by provider.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreRequest,
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


@pytest.fixture
def fake_store() -> Iterator[tuple[str, FakeState]]:
    with running_fake_store() as served:
        yield served


def _onex_home(
    tmp_path: Path,
    address: str,
    *,
    with_identity: bool = True,
    secret: str = CLIENT_SECRET,
) -> Path:
    lines = [
        "secret_store:",
        "  provider: infisical",
        f"  address: {address}",
        f"  project_id: {PROJECT}",
        "  environment: dev",
    ]
    if with_identity:
        lines += [
            "  client_id_ref: lab-reader-id-ref",
            "  client_secret_ref: lab-reader-ref",
        ]
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps({"lab-reader-id-ref": CLIENT_ID, "lab-reader-ref": secret})
    )
    creds.chmod(0o600)
    return tmp_path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_http_fake_resolve_list_and_create(
    tmp_path: Path,
    fake_store: tuple[str, FakeState],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    address, state = fake_store
    state.secrets[("/lab", "TEST_KEY")] = PLANTED
    handler = HandlerSecretStore(onex_home=_onex_home(tmp_path, address))

    resolved = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            reference="secret://onex/lab/TEST_KEY",
        )
    )
    assert resolved.outcome is EnumSecretStoreOutcome.RESOLVED
    assert resolved.value is not None
    assert resolved.value.get_secret_value() == PLANTED

    listed = await handler.handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST, folder="/lab")
    )
    assert listed.outcome is EnumSecretStoreOutcome.LISTED
    assert listed.keys == ("TEST_KEY",)

    created = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder="/lab",
            key="NEW",
            value=SecretStr("new-value"),
        )
    )
    assert created.outcome is EnumSecretStoreOutcome.CREATED
    assert state.secrets[("/lab", "NEW")] == "new-value"
    assert state.logins == 1  # one login, the token reused

    for result in (resolved, listed, created):
        assert PLANTED not in result.model_dump_json()
    assert all(PLANTED not in r.getMessage() for r in caplog.records)
    assert all(CLIENT_SECRET not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exists_status", [400, 409])
async def test_http_fake_create_on_existing_key_is_exists(
    tmp_path: Path, fake_store: tuple[str, FakeState], exists_status: int
) -> None:
    address, state = fake_store
    state.exists_status = exists_status
    state.secrets[("/captured", "KEY")] = "original"
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder="/captured",
            key="KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.EXISTS
    assert state.secrets[("/captured", "KEY")] == "original"


async def test_http_fake_missing_key_is_not_found(
    tmp_path: Path, fake_store: tuple[str, FakeState]
) -> None:
    address, _ = fake_store
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.RESOLVE, key="NOPE")
    )
    assert result.outcome is EnumSecretStoreOutcome.NOT_FOUND


async def test_http_fake_wrong_identity_is_refused(
    tmp_path: Path, fake_store: tuple[str, FakeState]
) -> None:
    address, _ = fake_store
    result = await HandlerSecretStore(
        onex_home=_onex_home(tmp_path, address, secret="wrong")
    ).handle(ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST))
    assert result.outcome is EnumSecretStoreOutcome.REFUSED
    assert result.detail == "PermissionError: login refused with HTTP 401"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (403, EnumSecretStoreOutcome.REFUSED),
        (503, EnumSecretStoreOutcome.UNREACHABLE),
        (422, EnumSecretStoreOutcome.FAILED),
    ],
)
async def test_http_fake_create_status_maps_to_typed_outcome_without_echo(
    tmp_path: Path,
    fake_store: tuple[str, FakeState],
    status: int,
    outcome: EnumSecretStoreOutcome,
) -> None:
    address, state = fake_store
    state.forced[("POST", "/api/v3/secrets/raw/KEY")] = status
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            key="KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is outcome
    assert PLANTED not in result.model_dump_json()


async def test_http_fake_unreachable_store(tmp_path: Path) -> None:
    address = f"http://127.0.0.1:{_free_port()}"
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.RESOLVE, key="A")
    )
    assert result.outcome is EnumSecretStoreOutcome.UNREACHABLE


async def test_http_fake_overlay_without_identity_fails_closed(
    tmp_path: Path, fake_store: tuple[str, FakeState]
) -> None:
    address, state = fake_store
    result = await HandlerSecretStore(
        onex_home=_onex_home(tmp_path, address, with_identity=False)
    ).handle(ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST))
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert state.logins == 0
    assert state.seen_bodies == []


async def test_http_fake_world_readable_credentials_fail_closed(
    tmp_path: Path, fake_store: tuple[str, FakeState]
) -> None:
    address, state = fake_store
    home = _onex_home(tmp_path, address)
    (home / "credentials.json").chmod(0o644)
    result = await HandlerSecretStore(onex_home=home).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST)
    )
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert state.seen_bodies == []
