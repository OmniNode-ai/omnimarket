# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two node overlay documents carry the optional ``event_bus`` key (OMN-24265).

``onex run-node`` reads ``event_bus`` from the node's overlay document. Each of the two models
must accept the optional key while keeping ``extra="forbid"`` on every other key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.model_code_score import ModelCodeScoreOverlay
from omnimarket.models.model_delegation_code_budget import ModelCodeBudgetOverlay

pytestmark = pytest.mark.unit

_CODE_SCORE_EFFECT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_contract_score_effect"
)

_BUDGET_REQUIRED = {
    "class_budgets_s": {"code_generation": 600.0},
    "sizing_margin": 1.5,
    "round_up_to_s": 60,
    "min_completed_samples": 3,
    "ceiling_s": 600,
    "input_ceilings": {"local-coder": {"code_generation": 65536}},
}
_SCORE_REQUIRED = {
    "check_timeout_s": 600,
    "env_sync_timeout_s": 900,
    "runnable_check_types": ["test_passes"],
    "env_sync_command": "uv sync --frozen",
    "scratch_root": "/tmp/scratch",
    "repo_roots": {"omnimarket": "/tmp/omnimarket"},
    "protected_paths": {"omnimarket": ["contracts/"]},
    "output_tail_chars": 600,
}


def _document(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# --- ModelCodeBudgetOverlay ---


def test_budget_golden_event_bus_kafka_dev(tmp_path: Path) -> None:
    document = {**_BUDGET_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelCodeBudgetOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_budget_without_event_bus_loads_with_none(tmp_path: Path) -> None:
    path = _document(tmp_path, _BUDGET_REQUIRED)
    model = ModelCodeBudgetOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus is None


def test_budget_event_bus_kafka_without_lane_is_refused(tmp_path: Path) -> None:
    document = {**_BUDGET_REQUIRED, "event_bus": {"type": "kafka"}}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelCodeBudgetOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_budget_unknown_top_level_key_is_still_refused(tmp_path: Path) -> None:
    document = {**_BUDGET_REQUIRED, "some_other_key": 1}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelCodeBudgetOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_budget_unknown_inner_key_is_still_refused(tmp_path: Path) -> None:
    document = {
        **_BUDGET_REQUIRED,
        "event_bus": {"type": "kafka", "lane": "dev", "bogus": 1},
    }
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelCodeBudgetOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_model_accepts_event_bus_key_failing_control_budget(tmp_path: Path) -> None:
    """Failing control: at the base commit extra=forbid refused the key."""
    document = {**_BUDGET_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelCodeBudgetOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")


# --- ModelCodeScoreOverlay ---


def test_score_golden_event_bus_kafka_dev(tmp_path: Path) -> None:
    document = {**_SCORE_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelCodeScoreOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_score_shipped_overlay_example_validates() -> None:
    document = yaml.safe_load(
        (_CODE_SCORE_EFFECT / "overlay.example.yaml").read_text("utf-8")
    )
    model = ModelCodeScoreOverlay.model_validate(document)
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="example-lane")


def test_score_without_event_bus_loads_with_none(tmp_path: Path) -> None:
    path = _document(tmp_path, _SCORE_REQUIRED)
    model = ModelCodeScoreOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus is None


def test_score_event_bus_kafka_without_lane_is_refused(tmp_path: Path) -> None:
    document = {**_SCORE_REQUIRED, "event_bus": {"type": "kafka"}}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelCodeScoreOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_score_unknown_top_level_key_is_still_refused(tmp_path: Path) -> None:
    document = {**_SCORE_REQUIRED, "some_other_key": 1}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelCodeScoreOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_score_unknown_inner_key_is_still_refused(tmp_path: Path) -> None:
    document = {
        **_SCORE_REQUIRED,
        "event_bus": {"type": "kafka", "lane": "dev", "bogus": 1},
    }
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelCodeScoreOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_model_accepts_event_bus_key_failing_control_score(tmp_path: Path) -> None:
    """Failing control: at the base commit extra=forbid refused the key."""
    document = {**_SCORE_REQUIRED, "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelCodeScoreOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
