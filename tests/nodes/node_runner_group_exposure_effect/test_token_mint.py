# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_effect: the GitHub source mints a fresh App installation token per read
instead of reading a stored one that expires after an hour (OMN-23583).

Golden chain: the minted token is the one sent, once per read, and the stored token is never read.
Error chain: a refused mint reads UNKNOWN with its own reason and makes no GitHub call. The shape
follows test_exposure_probe.py (same local HTTP fixture and overlay installer)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from omnimarket.github_app_auth import (
    GitHubAppCredentialMissingError,
    GitHubAppIdentityError,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.runner_group_exposure import EnumExposureReadFailure
from omnimarket.nodes.node_runner_group_exposure_effect.adapters import app_token_mint
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.app_token_mint import (
    AppInstallationTokenMint,
)
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.github_group_source import (
    GithubGroupSource,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
    load_topics,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    ModelGithubSource,
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

pytestmark = pytest.mark.unit


@pytest.fixture
def server() -> Iterator[_Server]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.scenario = Scenario()
    httpd.seen_auth = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


MINTED = "ghs_minted_example_0123456789"
APP_ID_REF = "EXAMPLE_APP_ID"
KEY_REF = "EXAMPLE_APP_PRIVATE_KEY"
MINT = {
    "org": "example-org",
    "app_id_secret": APP_ID_REF,
    "private_key_secret": KEY_REF,
}


class _Minter:
    def __init__(self, token: str | None = MINTED, error: Exception | None = None):
        self.token = token
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def mint(self, *, org: str, app_id_secret: str, private_key_secret: str) -> str:
        self.calls.append((org, app_id_secret, private_key_secret))
        if self.error is not None:
            raise self.error
        assert self.token is not None
        return self.token


def _handler(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minter: _Minter | None,
) -> HandlerExposureProbe:
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    # the stored token, as the unit has it: expired, so GitHub would refuse it
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    server.scenario = Scenario()
    source = {
        "backend": "github",
        "api_base": f"http://127.0.0.1:{server.server_address[1]}",
        "org": "example-org",
        "group_id": 7,
        "group_labels": ["example-private-group"],
        "token_env": TOKEN_ENV,
        "token_mint": MINT,
        "max_calls": 60,
        "timeout_seconds": 5,
    }
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, {"settings": SETTINGS, "source": source}
    )
    return HandlerExposureProbe(token_mint=minter)


def _request() -> ModelExposureProbeRequest:
    return ModelExposureProbeRequest(correlation_id="mint-1", now=NOW)


# -- golden chain ------------------------------------------------------------------------------


def test_golden_chain_reads_with_the_minted_token_and_never_the_stored_one(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minter = _Minter()
    result = _handler(server, tmp_path, monkeypatch, minter).handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    topic, _ = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_completed"]
    assert result.calls > 1
    assert set(server.seen_auth) == {f"Bearer {MINTED}"}
    # one mint per read, from the names the overlay declares
    assert minter.calls == [("example-org", APP_ID_REF, KEY_REF)]
    assert MINTED not in result.model_dump_json()


def test_golden_chain_a_second_read_mints_again(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minter = _Minter()
    handler = _handler(server, tmp_path, monkeypatch, minter)
    handler.handle(_request())
    handler.handle(_request())
    assert len(minter.calls) == 2


# -- error chain -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        GitHubAppCredentialMissingError("the secret did not resolve"),
        GitHubAppIdentityError("the App refused the exchange"),
        RuntimeError("anything else the mint raises"),
    ],
)
def test_error_chain_a_refused_mint_reads_unknown_with_its_own_reason_and_calls_nothing(
    error: Exception,
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _handler(server, tmp_path, monkeypatch, _Minter(error=error)).handle(
        _request()
    )
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumExposureReadFailure.TOKEN_MINT_FAILED
    assert result.calls == 0
    assert server.seen_auth == []
    # the stored token is not a fallback: a refused mint is never read as the old denial
    assert TOKEN_VALUE not in result.model_dump_json()
    topic, payload = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_failed"]
    assert payload["reading"]["read_failure"] == "token-mint-failed"


def test_error_chain_a_declared_mint_with_no_minter_is_refused_at_construction() -> (
    None
):
    source = ModelGithubSource(
        backend="github",
        api_base="http://127.0.0.1:1",
        org="example-org",
        group_id=7,
        group_labels=("example-private-group",),
        token_env=TOKEN_ENV,
        token_mint=ModelTokenMint(**MINT),
        max_calls=1,
        timeout_seconds=1,
    )
    with pytest.raises(ValueError, match="token_mint"):
        GithubGroupSource(
            source,
            http_get=cast(Any, _Minter()),
            environ={},
        )


# -- the real minter (secrets and exchange stubbed at their own seams) ----------------------------


def test_the_real_minter_resolves_both_named_secrets_and_exchanges_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []
    values = {APP_ID_REF: "12345", KEY_REF: "pem-body"}

    def fake_resolve(ref: str | None, **kwargs: object) -> SecretStr | None:
        assert ref is not None
        resolved.append(ref)
        return SecretStr(values[ref])

    seen: dict[str, object] = {}

    def fake_mint(**kwargs: object) -> str:
        seen.update(kwargs)
        return MINTED

    monkeypatch.setattr(app_token_mint, "resolve_api_key", fake_resolve)
    monkeypatch.setattr(app_token_mint, "mint_installation_token", fake_mint)
    token = AppInstallationTokenMint().mint(
        org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
    )
    assert token == MINTED
    assert resolved == [APP_ID_REF, KEY_REF]
    assert seen["app_id"] == "12345"
    assert seen["private_key_pem"] == "pem-body"
    assert seen["org"] == "example-org"


def test_error_chain_the_real_minter_names_a_missing_secret_and_never_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolve(ref: str | None, **kwargs: object) -> SecretStr | None:
        return SecretStr("12345") if ref == APP_ID_REF else None

    monkeypatch.setattr(app_token_mint, "resolve_api_key", fake_resolve)
    with pytest.raises(GitHubAppCredentialMissingError) as raised:
        AppInstallationTokenMint().mint(
            org="example-org", app_id_secret=APP_ID_REF, private_key_secret=KEY_REF
        )
    assert KEY_REF in str(raised.value)
    assert "12345" not in str(raised.value)
