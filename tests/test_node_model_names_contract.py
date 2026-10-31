# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Nodes read their model names from the node model-name manifest, with every name as before."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers.node_model_names_reader import load_node_model_names
from omnimarket.models.lane_brief.enum_lane_engine import EnumLaneEngine
from omnimarket.models.model_execution_identity import engine_model
from omnimarket.models.model_node_model_names import ModelNodeModelNames
from omnimarket.nodes.node_lane_brief_parse_compute.handlers.handler_lane_brief_parse import (
    _ALIASES,
)
from omnimarket.nodes.node_thread_reply_effect.handlers import handler_thread_reply

# node -> {declared name: the model name its code held as a literal before it moved}
DECLARED: dict[str, dict[str, str]] = {
    "node_thread_reply_effect": {
        "fast_model": "deepseek-r1-14b",
        "coder_model": "qwen3-coder-30b",
        "glm_model": "glm-4.5",
    },
}

SLICE_FILES = (
    "nodes/node_idle_slot_fill_effect/handlers/handler_idle_slot_fill.py",
    "nodes/node_lane_brief_parse_compute/handlers/handler_lane_brief_parse.py",
    "nodes/node_thread_reply_effect/handlers/handler_thread_reply.py",
)


@pytest.mark.unit
def test_slice_has_no_literal_model_name_findings() -> None:
    root = Path(__file__).resolve().parents[1] / "src/omnimarket"
    paths = [root / name for name in SLICE_FILES]
    assert all(path.is_file() for path in paths), "a target file is missing"
    result = ValidatorContractsAndDi().validate(paths)
    assert result.metadata is not None, "the validator returned no scan metadata"
    processed = result.metadata.files_processed
    assert processed is not None, "the validator returned no file count"
    assert processed >= len(paths), "the validator did not scan the targets"
    findings = [
        issue for issue in result.issues if issue.rule_name == "literal_model_name"
    ]
    assert not findings, findings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("node_name", "declared"), sorted(DECLARED.items()), ids=sorted(DECLARED)
)
def test_manifest_declares_every_model_name_with_its_former_value(
    node_name: str, declared: dict[str, str]
) -> None:
    names = load_node_model_names(node_name)
    assert dict(names.names) == declared
    for name, value in declared.items():
        assert names.get(name) == value


@pytest.mark.unit
def test_undeclared_name_stops_the_node() -> None:
    with pytest.raises(KeyError, match="declares no model name 'nope'"):
        ModelNodeModelNames(names={"a": "m"}).get("nope")


@pytest.mark.unit
def test_node_without_a_manifest_entry_refuses() -> None:
    with pytest.raises(KeyError, match="node_lane_model_policy_compute"):
        load_node_model_names("node_lane_model_policy_compute")


@pytest.mark.unit
def test_thread_reply_registry_comes_from_the_manifest() -> None:
    assert handler_thread_reply._CONTRACT_BACKENDS == {
        "qwen3-coder-30b": "local-coder",
        "glm-4.5": "cloud-glm",
    }
    assert "deepseek-r1-14b" in handler_thread_reply._BASE_REGISTRY


@pytest.mark.unit
def test_engine_names_come_from_the_lane_engine_enum() -> None:
    assert _ALIASES == {"sonnet": "claude-sonnet", "opus": "claude-opus"}
    assert engine_model("sonnet") == EnumLaneEngine.CLAUDE_SONNET.value
    assert engine_model("opus") == EnumLaneEngine.CLAUDE_OPUS.value
    assert engine_model("haiku") == "claude-haiku"
