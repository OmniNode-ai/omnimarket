# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prune configuration uses overlays/stored credentials, never value env vars."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr, ValidationError

from omnimarket.handlers import handler_prune_binding as binding_module
from omnimarket.handlers.handler_prune_binding import (
    PruneConfigurationError,
    load_prune_binding,
    prune_database_url,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.model_prune_binding import ModelPruneBinding

pytestmark = pytest.mark.unit


def test_database_secret_ref_uses_injected_store() -> None:
    store = AsyncMock(spec=ProtocolSecretStore)
    store.get_secret.return_value = "postgresql://fixture/store-binding"
    binding = ModelPruneBinding(database_secret_ref="database.retention.url")
    assert (
        prune_database_url(binding, "consumer_flow", store=store)
        == "postgresql://fixture/store-binding"
    )
    store.get_secret.assert_awaited_once_with("database.retention.url")


async def test_store_resolution_inside_runtime_event_loop() -> None:
    store = AsyncMock(spec=ProtocolSecretStore)
    store.get_secret.return_value = "postgresql://fixture/store-binding"
    assert (
        prune_database_url(
            ModelPruneBinding(database_secret_ref="database.retention.url"),
            "dead_letter",
            store=store,
        )
        == "postgresql://fixture/store-binding"
    )


def test_missing_stored_secret_refuses_even_when_same_env_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def missing(self: LocalByokCredentialStore, key: str) -> None:
        seen.append(key)

    monkeypatch.setattr(binding_module, "_configured_secret_store", lambda: None)
    monkeypatch.setattr(LocalByokCredentialStore, "get_secret", missing)
    monkeypatch.setenv("OMNIBASE_INFRA_DB_URL", "postgresql://ignored/environment")
    with pytest.raises(
        PruneConfigurationError, match="database_secret_ref is unresolved"
    ):
        prune_database_url(
            ModelPruneBinding(database_secret_ref="OMNIBASE_INFRA_DB_URL"),
            "dead_letter",
        )
    assert seen == ["OMNIBASE_INFRA_DB_URL"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"consumer_flow.archive_dir": "relative"},
        {
            "consumer_flow.database_url": "",
            "consumer_flow.database_secret_ref": "db.url",
        },
        {"consumer_flow.unknown_key": "postgresql://secret:must-not-leak@fixture/db"},
    ],
)
def test_invalid_overlay_refuses_without_disclosing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: dict[str, str]
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "overlay_version": "1.0.0",
                "environment": "test",
                "scope": "env",
                "services": {"prune": overrides},
            }
        )
    )
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    with pytest.raises(PruneConfigurationError) as err:
        load_prune_binding(ModelPruneBinding(), "consumer_flow")
    assert "OMNIMARKET_PRUNE_BINDING_OVERLAY" in str(err.value)
    assert "must-not-leak" not in str(err.value)


def test_explicit_missing_overlay_refuses_instead_of_using_contract_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "OMNIMARKET_PRUNE_BINDING_OVERLAY", str(tmp_path / "missing.yaml")
    )
    with pytest.raises(PruneConfigurationError, match="OverlayNotFoundError"):
        load_prune_binding(ModelPruneBinding(archive_dir=tmp_path), "consumer_flow")


def test_overlay_database_ref_replaces_contract_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "overlay_version": "1.0.0",
                "environment": "test",
                "scope": "env",
                "services": {
                    "prune": {"consumer_flow.database_secret_ref": "db.retention.url"}
                },
            }
        )
    )
    overlay.chmod(0o600)
    monkeypatch.setenv("OMNIMARKET_PRUNE_BINDING_OVERLAY", str(overlay))
    resolved = load_prune_binding(
        ModelPruneBinding(
            archive_dir=tmp_path, database_url=SecretStr("postgresql://fixture/base")
        ),
        "consumer_flow",
    )
    assert resolved.database_url is None
    assert resolved.database_secret_ref == "db.retention.url"
    assert resolved.archive_dir == tmp_path


def test_binding_credentials_are_redacted_and_sources_are_exclusive() -> None:
    binding = ModelPruneBinding(
        database_url=SecretStr("postgresql://secret:must-not-leak@fixture/db")
    )
    assert "must-not-leak" not in repr(binding)
    assert "must-not-leak" not in binding.model_dump_json()
    with pytest.raises(ValidationError):
        ModelPruneBinding(
            database_url=binding.database_url, database_secret_ref="db.url"
        )


def test_database_secret_ref_uses_configured_lane_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AsyncMock(spec=ProtocolSecretStore)
    store.get_secret.return_value = "postgresql://fixture/lane-binding"
    monkeypatch.setattr(binding_module, "_configured_secret_store", lambda: store)
    assert (
        prune_database_url(
            ModelPruneBinding(database_secret_ref="database.retention.url"),
            "dead_letter",
        )
        == "postgresql://fixture/lane-binding"
    )
    store.get_secret.assert_awaited_once_with("database.retention.url")
