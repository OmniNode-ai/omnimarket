# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models the overlay home validates small node overlays through (OMN-22478)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.node_overlays.model_node_overlays import (
    ModelAbCompareRegistryOverlay,
    ModelDuplicateGuardGeneratedPathsOverlay,
    ModelIntegrationSweepOverlay,
    ModelLabDiskHygieneOverlay,
    ModelLabSliPackOverlay,
    ModelRouteCheckOverlay,
    ModelRuntimeHostOverlay,
)

pytestmark = pytest.mark.unit

SWEEP = {
    "runtime_host": "host-a",
    "runtime_repo_path": "srv/repo",
    "stability_test_runtime_url": "http://host-a:1",
    "container_health_host": "host-a",
    "infra_runtime_host": "host-a",
    "projection_api_url": "http://host-a:2",
}

AB_COMPARE_REGISTRY = {
    "schema_version": "1.0.0",
    "models": [
        {
            "id": "model-a",
            "display_name": "Model A",
            "endpoint_env": "MODEL_A_URL",
            "protocol": "openai_compatible",
            "model_id_env": "MODEL_A_NAME",
            "model_id_default": "",
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
            "location": "local",
            "context_window": 8192,
        }
    ],
}

DOCUMENTS: list[tuple[str, type[BaseModel], dict[str, object]]] = [
    ("lab_sli_pack", ModelLabSliPackOverlay, {"status": "enabled"}),
    ("node_data_flow_sweep", ModelRuntimeHostOverlay, {"runtime_host": "host-a"}),
    ("node_database_sweep", ModelRuntimeHostOverlay, {"runtime_host": "host-a"}),
    ("node_integration_sweep_orchestrator", ModelIntegrationSweepOverlay, SWEEP),
    (
        "node_delegation_rubric_check_compute",
        ModelRouteCheckOverlay,
        {"route_check_local_model": "model-a"},
    ),
    (
        "node_ab_compare_orchestrator",
        ModelAbCompareRegistryOverlay,
        AB_COMPARE_REGISTRY,
    ),
    (
        "node_lab_disk_hygiene_effect",
        ModelLabDiskHygieneOverlay,
        {"census_path": "repo/census.yaml"},
    ),
    (
        "node_pr_duplicate_guard_compute",
        ModelDuplicateGuardGeneratedPathsOverlay,
        {"generated_paths": {"repo": ["plans/INDEX.md"]}},
    ),
]


@pytest.mark.parametrize(("overlay_id", "model", "document"), DOCUMENTS)
def test_loads_the_way_the_overlay_home_loads_it(
    tmp_path: Path, overlay_id: str, model: type[BaseModel], document: dict[str, object]
) -> None:
    (tmp_path / overlay_id).mkdir()
    (tmp_path / overlay_id / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    loaded = load_structured_overlay(
        overlay_id, model, {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert loaded.model_dump(mode="json")


@pytest.mark.parametrize(("overlay_id", "model", "document"), DOCUMENTS)
def test_a_stray_key_refuses(
    overlay_id: str, model: type[BaseModel], document: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**document, "stray": "x"})


@pytest.mark.parametrize(("overlay_id", "model", "document"), DOCUMENTS)
def test_a_missing_or_blank_field_refuses(
    overlay_id: str, model: type[BaseModel], document: dict[str, object]
) -> None:
    key = next(iter(document))
    with pytest.raises(ValidationError):
        model.model_validate({k: v for k, v in document.items() if k != key})
    with pytest.raises(ValidationError):
        model.model_validate(
            {**document, key: "  " if isinstance(document[key], str) else {}}
        )


def test_the_sli_pack_status_is_one_of_two_words() -> None:
    with pytest.raises(ValidationError):
        ModelLabSliPackOverlay.model_validate({"status": "on"})


def test_the_models_import_with_pydantic_alone() -> None:
    probe = (
        "import sys\n"
        "for name in ('yaml',):\n"
        "    sys.modules[name] = None\n"
        "import omnimarket.models.node_overlays.model_node_overlays as m\n"
        "assert not [n for n in sys.modules if n.startswith('omnibase_')], 'imports a core package'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    assert result.returncode == 0, result.stderr
