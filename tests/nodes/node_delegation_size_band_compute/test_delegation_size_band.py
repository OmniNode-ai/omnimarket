# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Size band is measured from the text, never declared by the caller."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_delegation_size_band_compute as node_package
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
)
from omnimarket.models.delegation.model_size_band import (
    EnumSizeBand,
    ModelSizeBand,
    ModelSizeBandRefusal,
    ModelSizeBandRequest,
)
from omnimarket.nodes.node_delegation_size_band_compute.handlers.handler_delegation_size_band import (
    HandlerDelegationSizeBand,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
CLASS = "summarization"
CALLER_FIELDS = ("size_band", "band", "input_tokens", "units", "steps")


def _handle(**fields: Any) -> ModelSizeBand | ModelSizeBandRefusal:
    request = ModelSizeBandRequest.model_validate(
        {"task_class": CLASS, "prompt": "Summarise the notes.", **fields}
    )
    return HandlerDelegationSizeBand().handle(request)


def _measured(**fields: Any) -> ModelSizeBand:
    result = _handle(**fields)
    assert isinstance(result, ModelSizeBand), result
    return result


def _headings(count: int) -> str:
    return "".join(f"# Section {index}\nbody\n" for index in range(count))


def _criteria(count: int) -> tuple[str, ...]:
    return tuple(f"criterion_{index}" for index in range(count))


def test_tokens_are_characters_over_four_rounded_up() -> None:
    assert _measured(prompt="").input_tokens.value == 0
    assert _measured(prompt="a" * 4).input_tokens.value == 1
    assert _measured(prompt="a" * 5).input_tokens.value == 2
    split = _measured(prompt="a" * 5, context_pack="b" * 4, sources=("c" * 3,))
    assert split.input_tokens.value == 3


@pytest.mark.parametrize(
    ("chars", "band"),
    [
        (16000, EnumSizeBand.S),
        (16001, EnumSizeBand.M),
        (64000, EnumSizeBand.M),
        (64001, EnumSizeBand.L),
    ],
)
def test_token_edges(chars: int, band: EnumSizeBand) -> None:
    result = _measured(prompt="a" * chars)
    assert result.input_tokens.band is band
    assert result.band is band


@pytest.mark.parametrize(
    ("count", "band"),
    [
        (1, EnumSizeBand.S),
        (3, EnumSizeBand.S),
        (4, EnumSizeBand.M),
        (10, EnumSizeBand.M),
        (11, EnumSizeBand.L),
    ],
)
def test_unit_edges(count: int, band: EnumSizeBand) -> None:
    result = _measured(context_pack=_headings(count))
    assert result.units.value == count
    assert result.units.band is band
    assert result.band is band


@pytest.mark.parametrize(
    ("count", "band"),
    [
        (1, EnumSizeBand.S),
        (3, EnumSizeBand.S),
        (4, EnumSizeBand.M),
        (8, EnumSizeBand.M),
        (9, EnumSizeBand.L),
    ],
)
def test_step_edges(count: int, band: EnumSizeBand) -> None:
    result = _measured(acceptance_criteria=_criteria(count))
    assert result.steps.value == count
    assert result.steps.band is band
    assert result.band is band


def test_request_band_is_the_largest_feature_band() -> None:
    result = _measured(
        prompt="a" * 20000,
        context_pack=_headings(11),
        acceptance_criteria=_criteria(1),
    )
    assert result.input_tokens.band is EnumSizeBand.M
    assert result.units.band is EnumSizeBand.L
    assert result.steps.band is EnumSizeBand.S
    assert result.band is EnumSizeBand.L


def test_units_count_diff_files_sections_row_blocks_and_enumerated_items() -> None:
    diff = (
        "```diff\n"
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        "```\n"
    )
    table = "| a | b |\n| - | - |\n| 1 | 2 |\n\ntext\n\n| c |\n| 3 |\n"
    fenced = "```\n# not a heading\n- not an item\n```\n"
    result = _measured(
        prompt="Do these:\n1. read\n2. write\n- check\n" + fenced,
        context_pack="# One\n" + table,
        sources=(diff,),
    )
    # 3 enumerated items + 1 section + 2 row blocks + 2 diff files.
    assert result.units.value == 8


def test_steps_count_distinct_criteria_and_enumerated_deliverables() -> None:
    result = _measured(
        prompt="Produce:\n1. a table\n2. a note\n3) a table\n",
        acceptance_criteria=("concise", "concise", " no_refusal "),
    )
    # Distinct deliverables {a table, a note} + distinct criteria {concise, no_refusal}.
    assert result.steps.value == 4


def test_every_feature_carries_its_source_and_its_contract_threshold() -> None:
    result = _measured(prompt="a" * 400)
    for name, key in (
        ("input_tokens", "tokens"),
        ("units", "units"),
        ("steps", "steps"),
    ):
        feature = getattr(result, name)
        assert feature.source.source == "text_measurement"
        assert feature.source.reference
        assert feature.source.rule
        assert feature.threshold.source == "contract"
        assert feature.threshold.reference.startswith(
            "task_class_contracts.v1.yaml#size_band_thresholds"
        )
        assert feature.threshold.reference.endswith(f".{key}")
    assert result.task_class == CLASS


def test_thresholds_are_declared_in_the_class_contract() -> None:
    authority = load_task_class_authority()
    thresholds = authority.size_band_thresholds
    assert thresholds is not None
    assert (thresholds.tokens.s_max, thresholds.tokens.m_max) == (4000, 16000)
    assert (thresholds.units.s_max, thresholds.units.m_max) == (3, 10)
    assert (thresholds.steps.s_max, thresholds.steps.m_max) == (3, 8)


def test_a_changed_contract_changes_the_band_without_a_code_change() -> None:
    raw = yaml.safe_load(
        (ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml").read_text()
    )
    raw["size_band_thresholds"]["steps"] = {"s_max": 1, "m_max": 2}
    authority = ModelTaskClassAuthority.model_validate(raw)
    request = ModelSizeBandRequest(
        task_class=CLASS, prompt="p", acceptance_criteria=_criteria(2)
    )
    changed = HandlerDelegationSizeBand(authority).handle(request)
    shipped = HandlerDelegationSizeBand().handle(request)
    assert isinstance(changed, ModelSizeBand)
    assert isinstance(shipped, ModelSizeBand)
    assert (shipped.steps.band, changed.steps.band) == (EnumSizeBand.S, EnumSizeBand.M)


def test_a_class_may_override_the_default_thresholds() -> None:
    raw = yaml.safe_load(
        (ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml").read_text()
    )
    raw["task_classes"][CLASS]["size_band_thresholds"] = {
        "tokens": {"s_max": 1, "m_max": 2},
        "units": {"s_max": 1, "m_max": 2},
        "steps": {"s_max": 1, "m_max": 2},
    }
    authority = ModelTaskClassAuthority.model_validate(raw)
    handler = HandlerDelegationSizeBand(authority)
    request = ModelSizeBandRequest(task_class=CLASS, prompt="a" * 12)
    result = handler.handle(request)
    assert isinstance(result, ModelSizeBand)
    assert result.band is EnumSizeBand.L
    assert result.input_tokens.threshold.reference.endswith(
        f"task_classes.{CLASS}.size_band_thresholds.tokens"
    )
    other = handler.handle(ModelSizeBandRequest(task_class="document", prompt="a" * 12))
    assert isinstance(other, ModelSizeBand)
    assert other.band is EnumSizeBand.S


def test_no_threshold_or_measurement_literal_in_code() -> None:
    declared = {4000, 16000, 3, 10, 8}
    for path in (
        NODE_DIR / "handlers/handler_delegation_size_band.py",
        ROOT / "src/omnimarket/handlers/handler_size_band_measurement.py",
        ROOT / "src/omnimarket/models/delegation/model_size_band.py",
    ):
        tree = ast.parse(path.read_text())
        numbers = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
        }
        assert not numbers & declared, (path.name, numbers & declared)


def test_caller_metadata_ignored() -> None:
    """Free-form caller metadata cannot move the measurement."""
    baseline = _measured(prompt="a" * 400)
    for metadata in (
        {"size_band": "L", "band": "L", "input_tokens": 99999},
        {"units": 99, "steps": 99, "estimated_tokens": 1},
        {"nested": {"band": "S"}, "note": "this is tiny"},
    ):
        with_metadata = _measured(prompt="a" * 400, caller_metadata=metadata)
        assert with_metadata == baseline


@pytest.mark.parametrize("field", CALLER_FIELDS)
@pytest.mark.parametrize("value", [None, "S", "L", 0, 1, 99999])
def test_caller_band_refused(field: str, value: object) -> None:
    """Presence alone refuses, naming the field, even for null or matching."""
    result = _handle(**{field: value})
    assert isinstance(result, ModelSizeBandRefusal)
    assert result.status == "refused"
    assert result.reason == "size_input_supplied"
    assert result.fields == (field,)


def test_caller_band_refusal_names_every_supplied_field_in_declared_order() -> None:
    result = _handle(units=3, size_band="S", steps=1)
    assert isinstance(result, ModelSizeBandRefusal)
    assert result.fields == ("size_band", "units", "steps")


def test_refusal_never_carries_a_band() -> None:
    result = _handle(band="S")
    assert isinstance(result, ModelSizeBandRefusal)
    assert not {"band", "input_tokens", "units", "steps"} & set(
        ModelSizeBandRefusal.model_fields
    )


def test_unknown_task_class_is_refused_by_name() -> None:
    result = _handle(task_class="not_in_catalogue")
    assert isinstance(result, ModelSizeBandRefusal)
    assert result.reason == "task_class_unavailable"
    assert result.fields == ("task_class",)


def test_supplied_field_refusal_precedes_unknown_task_class() -> None:
    result = _handle(task_class="not_in_catalogue", band=None)
    assert isinstance(result, ModelSizeBandRefusal)
    assert result.reason == "size_input_supplied"
    assert result.fields == ("band",)


def test_missing_thresholds_refuse_rather_than_default() -> None:
    raw = yaml.safe_load(
        (ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml").read_text()
    )
    del raw["size_band_thresholds"]
    authority = ModelTaskClassAuthority.model_validate(raw)
    result = HandlerDelegationSizeBand(authority).handle(
        ModelSizeBandRequest(task_class=CLASS, prompt="p")
    )
    assert isinstance(result, ModelSizeBandRefusal)
    assert result.reason == "size_thresholds_unavailable"
    assert result.fields == ("size_band_thresholds",)


def test_measurement_is_deterministic_and_stateless() -> None:
    handler = HandlerDelegationSizeBand()
    request = ModelSizeBandRequest(
        task_class=CLASS,
        prompt="Do:\n1. a\n2. b\n",
        context_pack=_headings(2),
        acceptance_criteria=("concise",),
    )
    assert handler.handle(request) == handler.handle(request)
    assert handler.handle(request) == HandlerDelegationSizeBand().handle(request)
