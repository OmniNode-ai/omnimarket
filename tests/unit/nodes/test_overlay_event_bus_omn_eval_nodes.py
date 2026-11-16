# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two omn eval node overlay models accept an optional ``event_bus`` key (OMN-24122).

The key is read by the bus overlay reader behind ``onex run-node``; the node
never reads it. The models keep refusing every other unknown key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.code_eval_trial.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)
from omnimarket.models.eval_suite_census.model_suite_overlay import (
    ModelCodeEvalSuiteCensusOverlay,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]

_MODELS = [
    (
        ModelCodeEvalSuiteCensusOverlay,
        _ROOT
        / "src/omnimarket/nodes/node_code_eval_suite_census_compute/overlay.example.yaml",
    ),
    (
        ModelCodeEvalRunnerOverlay,
        _ROOT
        / "src/omnimarket/nodes/node_code_eval_trial_orchestrator/overlay.example.yaml",
    ),
]


@pytest.fixture(params=_MODELS, ids=[model.__name__ for model, _ in _MODELS])
def model_and_example(request: pytest.FixtureRequest) -> tuple[type, Path]:
    return request.param


def test_the_shipped_example_loads_and_names_the_bus(
    model_and_example: tuple[type, Path],
) -> None:
    model, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    overlay = model.model_validate(document)
    assert overlay.event_bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_the_example_without_event_bus_loads_with_none(
    model_and_example: tuple[type, Path],
) -> None:
    model, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    document.pop("event_bus", None)
    overlay = model.model_validate(document)
    assert overlay.event_bus is None


def test_event_bus_kafka_without_a_lane_is_refused(
    model_and_example: tuple[type, Path],
) -> None:
    model, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    document["event_bus"] = {"type": "kafka"}
    with pytest.raises(ValidationError):
        model.model_validate(document)


def test_an_unknown_extra_key_is_still_refused(
    model_and_example: tuple[type, Path],
) -> None:
    model, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    document["some_other_key"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(document)


def test_the_bus_reader_reads_the_same_bus(
    model_and_example: tuple[type, Path],
    tmp_path: Path,
) -> None:
    _, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_a_bogus_event_bus_type_is_refused(
    model_and_example: tuple[type, Path],
) -> None:
    model, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    document["event_bus"] = {"type": "bogus"}
    with pytest.raises(ValidationError):
        model.model_validate(document)


def test_the_model_does_not_refuse_the_event_bus_key(
    model_and_example: tuple[type, Path],
) -> None:
    """Failing control: at the base commit both models forbid the key."""
    model, example = model_and_example
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    overlay = model.model_validate(document)
    assert overlay.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
