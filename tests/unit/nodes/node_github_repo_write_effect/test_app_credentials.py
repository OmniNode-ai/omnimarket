# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ordered contract credentials at the live boundary, independent of the bus."""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.nodes.node_github_repo_write_effect.handlers import (
    handler_github_repo_write as module,
)

pytestmark = pytest.mark.unit


def _resolver(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> list[str]:
    seen: list[str] = []

    async def resolve(ref: str, *, env_var_fallback: str, required: bool = True):
        assert env_var_fallback == ref
        seen.append(ref)
        value = values.get(ref)
        if value is None:
            assert not required
            return None
        return SecretStr(value)

    monkeypatch.setattr(module, "resolve_api_key_async", resolve)
    return seen


async def test_resolved_github_token_never_takes_app_path(monkeypatch):
    monkeypatch.delenv("ONEX_AUTOMATION_ACTOR_ID", raising=False)
    seen = _resolver(monkeypatch, {"GITHUB_TOKEN": "ghs_test_primary"})
    mint = Mock(side_effect=AssertionError("App path taken while token resolves"))
    monkeypatch.setattr(module, "mint_installation_token", mint, raising=False)
    transport = await module.HandlerGithubRepoWriteEffect()._live_transport()
    assert transport._token == SecretStr("ghs_test_primary")
    assert seen == ["GITHUB_TOKEN"]
    mint.assert_not_called()


async def test_missing_token_mints_from_declared_store_refs(monkeypatch):
    seen = _resolver(
        monkeypatch,
        {
            "ONEXBOT_OCC_APP_ID": "test-app-id",
            "ONEXBOT_OCC_PRIVATE_KEY": "test-private-key",
            "ONEXBOT_OCC_INSTALLATION_ID": "123",
        },
    )
    mint = Mock(return_value="ghs_test_minted")
    monkeypatch.setattr(module, "mint_installation_token", mint, raising=False)
    transport = await module.HandlerGithubRepoWriteEffect()._live_transport(
        "OmniNode-ai/omnimarket"
    )
    assert transport._token == SecretStr("ghs_test_minted")
    assert seen == [
        "GITHUB_TOKEN",
        "ONEXBOT_OCC_APP_ID",
        "ONEXBOT_OCC_PRIVATE_KEY",
        "ONEXBOT_OCC_INSTALLATION_ID",
    ]
    mint.assert_called_once_with(
        app_id="test-app-id",
        private_key_pem="test-private-key",
        private_key_ref="ONEXBOT_OCC_PRIVATE_KEY",
        installation_id=123,
        org="OmniNode-ai",
        repositories=["omnimarket"],
    )


async def test_missing_installation_ref_uses_existing_org_discovery(monkeypatch):
    _resolver(
        monkeypatch, {"ONEXBOT_OCC_APP_ID": "id", "ONEXBOT_OCC_PRIVATE_KEY": "key"}
    )
    mint = Mock(return_value="ghs_test_minted")
    monkeypatch.setattr(module, "mint_installation_token", mint, raising=False)
    await module.HandlerGithubRepoWriteEffect()._live_transport(
        "OmniNode-ai/omnimarket"
    )
    assert mint.call_args.kwargs["installation_id"] is None


async def test_unreadable_key_refuses_without_minting(monkeypatch):
    _resolver(monkeypatch, {"ONEXBOT_OCC_APP_ID": "id"})
    mint = Mock()
    monkeypatch.setattr(module, "mint_installation_token", mint, raising=False)
    with pytest.raises(RuntimeError, match="ONEXBOT_OCC_PRIVATE_KEY"):
        await module.HandlerGithubRepoWriteEffect()._live_transport()
    mint.assert_not_called()


async def test_secret_values_never_reach_logs_or_mint_failure(monkeypatch, caplog):
    values = {
        "ONEXBOT_OCC_APP_ID": "sensitive-app-id",
        "ONEXBOT_OCC_PRIVATE_KEY": "sensitive-private-key",
    }
    _resolver(monkeypatch, values)
    mint = Mock(
        side_effect=RuntimeError("sensitive-private-key sensitive-minted-token")
    )
    monkeypatch.setattr(module, "mint_installation_token", mint, raising=False)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeError) as caught:
        await module.HandlerGithubRepoWriteEffect()._live_transport()
    for value in [*values.values(), "sensitive-minted-token"]:
        assert value not in caplog.text
        assert value not in str(caught.value)
    assert caught.value.__suppress_context__


def test_contract_declares_ordered_optional_credentials():
    contract = yaml.safe_load(module._CONTRACT_PATH.read_text())
    auth = contract["credential_choice"]
    assert auth["token_secret"] == "GITHUB_TOKEN"
    # The primary token stays the required declaration (OMN-21423); the
    # handler still resolves it with required=False so absence selects the App.
    assert contract["secrets"][auth["token_secret"]]["required"] is True
    for name in auth["fallback_app"].values():
        assert contract["secrets"][name]["required"] is False


async def test_missing_app_credential_returns_a_failure_terminal(monkeypatch):
    from uuid import uuid4

    from omnimarket.nodes.node_github_repo_write_effect.models import (
        ModelRepoWriteFailed,
        ModelRepoWriteRequest,
    )

    _resolver(monkeypatch, {})
    request = ModelRepoWriteRequest(
        correlation_id=uuid4(),
        operation="pr_create",
        repo="OmniNode-ai/omnimarket",
        requested_by_lane="app-auth-test",
        idempotency_key="missing-app",
        title="probe",
        head="probe",
        base="dev",
    )
    result = await module.HandlerGithubRepoWriteEffect().handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason.value == "credential_unavailable"
    assert "ONEXBOT_OCC_APP_ID" in result.detail
    assert result.requests == ()


async def test_resolver_error_does_not_take_app_path_or_expose_value(
    monkeypatch, caplog
):
    async def resolve(*args, **kwargs):
        raise RuntimeError("sensitive-store-exception")

    monkeypatch.setattr(module, "resolve_api_key_async", resolve)
    mint = Mock()
    monkeypatch.setattr(module, "mint_installation_token", mint)
    with pytest.raises(RuntimeError) as caught:
        await module.HandlerGithubRepoWriteEffect()._live_transport()
    assert "GITHUB_TOKEN" in str(caught.value)
    assert "sensitive-store-exception" not in str(caught.value)
    assert "sensitive-store-exception" not in caplog.text
    assert caught.value.__suppress_context__
    mint.assert_not_called()


def test_inmemory_cli_uses_the_same_store_resolver_for_app_credentials(
    monkeypatch, caplog
):
    import json

    from click.testing import CliRunner

    from omnimarket.github_landing.model_github_http_exchange import (
        ModelGithubHttpResponse,
    )
    from omnimarket.handlers.scm.scm_cli import scm_group

    seen = _resolver(
        monkeypatch,
        {
            "ONEXBOT_OCC_APP_ID": "synthetic-app",
            "ONEXBOT_OCC_PRIVATE_KEY": "synthetic-private-key",
        },
    )
    mint = Mock(return_value="ghs_synthetic-minted-token")
    monkeypatch.setattr(module, "mint_installation_token", mint)
    sent = []

    class Transport:
        async def send(self, request):
            sent.append(request)
            return ModelGithubHttpResponse(
                status=200 if request.method == "GET" else 201,
                headers={
                    "x-ratelimit-limit": "5000",
                    "x-ratelimit-remaining": "4900",
                    "x-ratelimit-used": "100",
                    "x-ratelimit-reset": "9999999999",
                    "x-ratelimit-resource": "core",
                },
                body={"value": []} if request.method == "GET" else {"number": 23},
            )

    def transport(secret):
        assert secret == SecretStr("ghs_synthetic-minted-token")
        return Transport()

    monkeypatch.setattr(module, "UrllibGithubLandingTransport", transport)
    answer = CliRunner().invoke(
        scm_group,
        [
            "pr-create",
            "OmniNode-ai/omnibase_spi",
            "--head",
            "probe",
            "--base",
            "dev",
            "--title",
            "test",
            "--body",
            "test",
            "--lane",
            "app-auth-test",
            "--bus",
            "inmemory",
            "--timeout-s",
            "2",
        ],
        catch_exceptions=False,
    )
    assert answer.exit_code == 0, answer.output
    payload = json.loads(answer.output)
    assert payload["result"]["pr_number"] == 23
    assert payload["bus"]["terminal_topic"].endswith("github-repo-write-completed.v1")
    assert [call.method for call in sent] == ["GET", "POST"]
    assert seen == [
        "GITHUB_TOKEN",
        "ONEXBOT_OCC_APP_ID",
        "ONEXBOT_OCC_PRIVATE_KEY",
        "ONEXBOT_OCC_INSTALLATION_ID",
    ]
    assert mint.call_args.kwargs["repositories"] == ["omnibase_spi"]
    for value in (
        "synthetic-app",
        "synthetic-private-key",
        "ghs_synthetic-minted-token",
    ):
        assert value not in answer.output
        assert value not in caplog.text
