# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The convergence orchestrator's overlay model refuses broken overlays (OMN-24198)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceMode,
    ModelConvergenceOrchestratorOverlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_convergence_orchestrator"
)


def _data(profile: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlays" / profile).read_text()
    )
    return loaded


@pytest.mark.parametrize("profile", ["in_memory.yaml", "lab_work.example.yaml"])
def test_both_shipped_profiles_validate_and_start_in_observe_mode(profile: str) -> None:
    overlay = ModelConvergenceOrchestratorOverlay.model_validate(_data(profile))

    assert overlay.mode is EnumConvergenceMode.OBSERVE
    assert overlay.command is overlay.observe_command
    assert [h.name for h in overlay.hosts] == [
        "example-host-a",
        "example-host-b",
        "example-host-c",
    ]


def test_the_act_value_selects_the_act_command() -> None:
    overlay = ModelConvergenceOrchestratorOverlay.model_validate(
        {**_data("in_memory.yaml"), "mode": "act"}
    )

    assert overlay.command is overlay.act_command


def test_a_mode_that_is_neither_observe_nor_act_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelConvergenceOrchestratorOverlay.model_validate(
            {**_data("in_memory.yaml"), "mode": "dry-run"}
        )


def test_a_duplicate_host_is_refused() -> None:
    data = _data("in_memory.yaml")
    data["hosts"] = [data["hosts"][0], data["hosts"][0]]

    with pytest.raises(ValidationError, match="duplicate host"):
        ModelConvergenceOrchestratorOverlay.model_validate(data)


def test_an_empty_host_list_and_an_unknown_key_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelConvergenceOrchestratorOverlay.model_validate(
            {**_data("in_memory.yaml"), "hosts": []}
        )
    with pytest.raises(ValidationError):
        ModelConvergenceOrchestratorOverlay.model_validate(
            {**_data("in_memory.yaml"), "surprise": 1}
        )
