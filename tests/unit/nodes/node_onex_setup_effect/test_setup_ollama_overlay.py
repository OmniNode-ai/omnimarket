# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Actual file controls before the Ollama overlay composition.

Catch a verify that writes, replacement of unrelated local routes, and success
from stale Ollama route metadata. These do not impersonate an inference result.
"""

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_ollama_config,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_environment import (
    reconcile_ollama_overlay,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModelSetupPlan, Path]:
    monkeypatch.setattr(
        handler_model_setup,
        "default_evidence_db_path",
        lambda: tmp_path / "store" / "delegation.sqlite",
    )
    config = load_ollama_config()
    assert config is not None
    return ModelSetupPlan(
        workspace=tmp_path,
        providers=({"provider": "ollama", "models": (config.models[-1].model,)},),
    ), tmp_path / "store" / "bifrost_overrides.yaml"


def test_preview_is_read_only_then_actual_owner_writes_and_reads_back(
    selected: tuple,
) -> None:
    plan, path = selected
    assert not reconcile_ollama_overlay(plan)
    assert not path.exists()
    assert reconcile_ollama_overlay(plan, write=True)
    before = path.read_bytes()
    assert reconcile_ollama_overlay(plan)
    assert reconcile_ollama_overlay(plan, write=True)
    assert path.read_bytes() == before


def test_unrelated_local_route_survives_verification_and_refused_reaim(
    selected: tuple,
) -> None:
    plan, path = selected
    assert reconcile_ollama_overlay(plan, write=True)
    data = yaml.safe_load(path.read_text())
    data["backends"].append(
        {
            "backend_id": "user-route",
            "endpoint_url": "http://127.0.0.1:19999/v1/chat/completions",
            "model_name": "user-model",
        }
    )
    marker = path.read_text().splitlines()[0]
    path.write_text(marker + "\n" + yaml.safe_dump(data))
    before = path.read_bytes()
    assert reconcile_ollama_overlay(plan)
    assert path.read_bytes() == before
    data["backends"][0]["model_name"] = "changed-user-model"
    path.write_text(marker + "\n" + yaml.safe_dump(data))
    changed = path.read_bytes()
    assert not reconcile_ollama_overlay(plan)
    with pytest.raises(ValueError, match=r"preserv|unrelated|route"):
        reconcile_ollama_overlay(plan, write=True)
    assert path.read_bytes() == changed


def test_a_test_from_another_endpoint_does_not_prove_the_native_route(
    selected: tuple,
) -> None:
    plan, path = selected
    assert reconcile_ollama_overlay(plan, write=True)
    data = yaml.safe_load(path.read_text())
    endpoint = data["backends"][0]["endpoint_url"]
    assert reconcile_ollama_overlay(plan, tested_endpoint=endpoint)
    assert not reconcile_ollama_overlay(
        plan, tested_endpoint="http://example.invalid:11434/v1/chat/completions"
    )
