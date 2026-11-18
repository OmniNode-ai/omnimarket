# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Removing a local model must leave the next delegation's config usable."""

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    HandlerModelSetup,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        handler_model_setup,
        "default_evidence_db_path",
        lambda: tmp_path / "delegation.sqlite",
    )
    return tmp_path / "bifrost_overrides.yaml"


@pytest.mark.parametrize("keep_other", [False, True])
def test_remove_last_local_model_preserves_other_overlay_keys(
    overlay: Path, keep_other: bool
) -> None:
    other = [{"backend_id": "byok-openai", "max_tokens": 1024}] if keep_other else []
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    *other,
                    {
                        "backend_id": "local-url",
                        "endpoint_url": "http://127.0.0.1:9999/v1/chat/completions",
                        "model_name": "incident-model",
                    },
                ],
                "shadow_mode": {"enabled": False},
                "operator_note": "keep this",
            },
            sort_keys=False,
        )
    )
    HandlerModelSetup().handle(ModelModelSetupRequest(operation="remove_local"))
    written = yaml.safe_load(overlay.read_text())
    assert written.get("backends", []) == other
    assert written["shadow_mode"] == {"enabled": False}
    assert written["operator_note"] == "keep this"


def test_remove_last_local_then_delegate_config_loads(overlay: Path) -> None:
    """Catch the schema failure on the next delegation's real loader path."""
    handler = HandlerModelSetup(
        probe_url=lambda _url: None,
        run_delegation=lambda _backend, **_kw: (1, "", "test transport unavailable"),
    )
    handler.handle(
        ModelModelSetupRequest(
            operation="add_local",
            url="http://127.0.0.1:9999",
            models=("incident-model",),
        )
    )
    handler.handle(ModelModelSetupRequest(operation="remove_local"))
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/configs/bifrost_delegation.yaml"
    )
    expected = load_bifrost_delegation_config(config_path=contract)
    actual = load_bifrost_delegation_config(config_path=contract, overlay_path=overlay)
    assert actual.backends == expected.backends
    assert actual.default_backends == expected.default_backends
