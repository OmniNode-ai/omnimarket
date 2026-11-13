# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_effect: the minter reads the App id and key through the secret store
block the settings file declares, and never from the process environment when it does (OMN-24311).

Golden chain: the declared store and folder are read, in order, and the minted token is the one sent.
Error chain: an unreachable store, a missing key or a refused identity reads UNKNOWN with the mint's
own reason and makes no GitHub call; the message names the secret and the folder, never a value.
Failing control: a decoy in the environment under the secret's own name is never used. The shape
follows test_token_mint.py (same local HTTP fixture, overlay installer and minter helpers)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.github_app_auth import GitHubAppCredentialMissingError
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.runner_group_exposure import EnumExposureReadFailure
from omnimarket.nodes.node_runner_group_exposure_effect.adapters import app_token_mint
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.app_token_mint import (
    AppInstallationTokenMint,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers import (
    handler_exposure_probe,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    ModelMintSecretStore,
    ModelTokenMint,
)
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_runner_group_exposure_effect.test_exposure_probe import (
    NOW,
    SETTINGS,
    TOKEN_ENV,
    TOKEN_VALUE,
    Scenario,
    _Handler,
    _Server,
)
from tests.nodes.node_runner_group_exposure_effect.test_token_mint import (
    APP_ID_REF,
    KEY_REF,
    MINT,
    MINTED,
)

pytestmark = pytest.mark.unit

FOLDER = "/example"
STORE_BLOCK: dict[str, Any] = {
    "folder": FOLDER,
    "secret_store": {
        "provider": "infisical",
        "address": "http://192.0.2.10:8880",
        "project_id": "00000000-0000-4000-8000-000000000000",
        "environment": "dev",
        "plain_http_hosts": ["192.0.2.10"],
        "root_folder": "/",
        "identity_file": ".omnibase/.env",
        "client_id_ref": "EXAMPLE_CLIENT_ID",
        "client_secret_ref": "EXAMPLE_CLIENT_SECRET",
    },
}
APP_ID_VALUE = "424242"
KEY_VALUE = "example-pem-body"


class _Reader:
    """A store seam: the values it holds, the reads asked of it, or the error it raises."""

    def __init__(
        self,
        values: dict[tuple[str, str], str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.values = (
            {(FOLDER, APP_ID_REF): APP_ID_VALUE, (FOLDER, KEY_REF): KEY_VALUE}
            if values is None
            else values
        )
        self.error = error
        self.asked: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.asked.append((folder, name))
        if self.error is not None:
            raise self.error
        value = self.values.get((folder, name))
        return None if value is None else SecretStr(value)


@pytest.fixture
def server() -> Iterator[_Server]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.scenario = Scenario()
    httpd.seen_auth = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _request() -> ModelExposureProbeRequest:
    return ModelExposureProbeRequest(correlation_id="store-mint-1", now=NOW)


def _stub_exchange(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_mint(**kwargs: object) -> str:
        seen.update(kwargs)
        return MINTED

    monkeypatch.setattr(app_token_mint, "mint_installation_token", fake_mint)
    return seen


def _no_environment_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the environment resolver was asked while a store is declared"
        )

    monkeypatch.setattr(app_token_mint, "resolve_api_key", refuse)


def _handler(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: _Reader,
    recorded: list[dict[str, Any]],
) -> HandlerExposureProbe:
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    # the decoys: the same names in the environment, which a declared store must never read
    monkeypatch.setenv(APP_ID_REF, "decoy-app-id")
    monkeypatch.setenv(KEY_REF, "decoy-key")
    server.scenario = Scenario()

    def fake_reader(*, overlay: Any) -> _Reader:
        recorded.append(overlay.model_dump(mode="json"))
        return reader

    monkeypatch.setattr(
        handler_exposure_probe, "SecretStoreSettingsReader", fake_reader
    )
    source = {
        "backend": "github",
        "api_base": f"http://127.0.0.1:{server.server_address[1]}",
        "org": "example-org",
        "group_id": 7,
        "group_labels": ["example-private-group"],
        "token_env": TOKEN_ENV,
        "token_mint": {**MINT, "store": STORE_BLOCK},
        "org_read_permission": "example_org_permission_read",
        "max_calls": 60,
        "timeout_seconds": 5,
    }
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, {"settings": SETTINGS, "source": source}
    )
    return HandlerExposureProbe()


# -- golden chain ------------------------------------------------------------------------------


def test_golden_chain_the_minter_reads_the_declared_folder_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_exchange(monkeypatch)
    _no_environment_resolution(monkeypatch)
    reader = _Reader()
    token = AppInstallationTokenMint(reader=reader, folder=FOLDER).mint(
        org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
    )
    assert token == MINTED
    assert reader.asked == [(FOLDER, APP_ID_REF), (FOLDER, KEY_REF)]
    assert seen["app_id"] == APP_ID_VALUE
    assert seen["private_key_pem"] == KEY_VALUE
    assert seen["org"] == "example-org"


def test_golden_chain_the_handler_builds_the_store_minter_from_the_declared_block(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_exchange(monkeypatch)
    _no_environment_resolution(monkeypatch)
    reader = _Reader()
    recorded: list[dict[str, Any]] = []
    result = _handler(server, tmp_path, monkeypatch, reader, recorded).handle(
        _request()
    )
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert set(server.seen_auth) == {f"Bearer {MINTED}"}
    # the store block the minter got is the one the settings file declares
    assert [block["address"] for block in recorded] == [
        STORE_BLOCK["secret_store"]["address"]
    ]
    assert recorded[0]["identity_file"] == ".omnibase/.env"
    assert reader.asked == [(FOLDER, APP_ID_REF), (FOLDER, KEY_REF)]
    assert MINTED not in result.model_dump_json()
    assert KEY_VALUE not in result.model_dump_json()


def test_golden_chain_the_read_works_from_inside_a_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_exchange(monkeypatch)

    async def inside_loop() -> str:
        return AppInstallationTokenMint(reader=_Reader(), folder=FOLDER).mint(
            org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
        )

    assert asyncio.run(inside_loop()) == MINTED


# -- failing control ---------------------------------------------------------------------------


def test_failing_control_a_decoy_in_the_environment_is_never_used(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _stub_exchange(monkeypatch)
    recorded: list[dict[str, Any]] = []
    # the store holds the real values; the environment holds decoys under the same names
    _handler(server, tmp_path, monkeypatch, _Reader(), recorded).handle(_request())
    assert seen["app_id"] == APP_ID_VALUE
    assert seen["private_key_pem"] == KEY_VALUE
    assert "decoy" not in str(seen)


def test_failing_control_with_no_store_the_environment_is_still_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_exchange(monkeypatch)
    monkeypatch.setenv(APP_ID_REF, "env-app-id")
    monkeypatch.setenv(KEY_REF, "env-key")
    AppInstallationTokenMint().mint(
        org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
    )
    assert seen["app_id"] == "env-app-id"
    assert seen["private_key_pem"] == "env-key"


# -- error chain -------------------------------------------------------------------------------


def test_error_chain_a_key_the_store_does_not_hold_is_named_without_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_exchange(monkeypatch)
    reader = _Reader({(FOLDER, APP_ID_REF): APP_ID_VALUE})
    with pytest.raises(GitHubAppCredentialMissingError) as raised:
        AppInstallationTokenMint(reader=reader, folder=FOLDER).mint(
            org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
        )
    message = str(raised.value)
    assert KEY_REF in message
    assert FOLDER in message
    assert APP_ID_VALUE not in message


def test_error_chain_an_empty_value_reads_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_exchange(monkeypatch)
    reader = _Reader({(FOLDER, APP_ID_REF): "", (FOLDER, KEY_REF): KEY_VALUE})
    with pytest.raises(GitHubAppCredentialMissingError, match=APP_ID_REF):
        AppInstallationTokenMint(reader=reader, folder=FOLDER).mint(
            org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
        )


def test_error_chain_an_unreachable_store_names_the_error_type_and_never_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_exchange(monkeypatch)
    reader = _Reader(error=RuntimeError("tailnet down with a secret-looking text"))
    with pytest.raises(GitHubAppCredentialMissingError) as raised:
        AppInstallationTokenMint(reader=reader, folder=FOLDER).mint(
            org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
        )
    message = str(raised.value)
    assert "RuntimeError" in message
    assert APP_ID_REF in message
    assert "secret-looking text" not in message


@pytest.mark.parametrize(
    "reader",
    [
        _Reader({}),
        _Reader(error=RuntimeError("store unreachable")),
    ],
)
def test_error_chain_through_the_handler_reads_unknown_and_calls_nothing(
    reader: _Reader,
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_exchange(monkeypatch)
    result = _handler(server, tmp_path, monkeypatch, reader, []).handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumExposureReadFailure.TOKEN_MINT_FAILED
    assert result.calls == 0
    assert server.seen_auth == []
    _, payload = HandlerExposureProbe.event_for(result)
    assert payload["reading"]["read_failure"] == "token-mint-failed"
    assert "decoy" not in result.model_dump_json()


def test_error_chain_a_reader_and_a_folder_come_together() -> None:
    with pytest.raises(ValueError, match="together"):
        AppInstallationTokenMint(reader=_Reader())
    with pytest.raises(ValueError, match="together"):
        AppInstallationTokenMint(folder=FOLDER)


# -- the settings file -------------------------------------------------------------------------


def test_the_settings_model_refuses_a_folder_that_is_not_absolute() -> None:
    with pytest.raises(ValidationError):
        ModelMintSecretStore.model_validate({**STORE_BLOCK, "folder": "example"})


def test_the_settings_model_refuses_a_store_block_without_a_folder() -> None:
    with pytest.raises(ValidationError):
        ModelMintSecretStore.model_validate(
            {"secret_store": STORE_BLOCK["secret_store"]}
        )


def test_the_settings_model_refuses_a_mint_that_leaves_the_store_key_out() -> None:
    without = {key: value for key, value in MINT.items() if key != "store"}
    with pytest.raises(ValidationError, match="store"):
        ModelTokenMint.model_validate(without)
