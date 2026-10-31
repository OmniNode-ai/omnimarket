"""Catch Click silently retaining only the last repeated --model."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret


def test_repeated_models_reach_the_hosted_intake_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    async def register(request: object, **kwargs: object) -> object:
        seen.append(request)
        return SimpleNamespace(
            api_key_ref="cred_fake_test",
            provider="openrouter",
            plan=None,
            model=getattr(request, "model", None),
            models=getattr(request, "models", None),
            expires_at=None,
            expiry_warning=None,
            bound_host=None,
            workspace_id=None,
        )

    async def bus_call(callback: object, **kwargs: object) -> object:
        return await callback(AsyncMock())

    monkeypatch.setattr(cli_secret, "register_inference_credential", register)
    monkeypatch.setattr(cli_secret, "_register_on_declared_bus", bus_call)
    monkeypatch.setattr(cli_secret, "_tenant_key_store", AsyncMock)
    monkeypatch.setattr(cli_secret, "_fold_registered_tenant_key", lambda *_args: None)
    monkeypatch.setattr(cli_secret, "_resolve_bound_host", lambda _host: None)
    result = CliRunner().invoke(
        cli_secret.secret_group,
        [
            "register-tenant-key",
            "openrouter",
            "--tenant",
            "tenant-a",
            "--model",
            "vendor/b",
            "--model",
            "vendor/a",
        ],
        input="synthetic-test-value\n",
    )
    assert result.exit_code == 0, result.output
    assert getattr(seen[0], "models", None) == ["vendor/b", "vendor/a"]
    assert "synthetic-test-value" not in result.output
