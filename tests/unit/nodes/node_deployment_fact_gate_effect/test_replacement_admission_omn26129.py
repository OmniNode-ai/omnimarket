# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129: the gate admits one named replacement of a rung the provider stopped listing.

OpenRouter stopped listing cohere/north-mini-code:free, cheap_frontier's only rung.
The gate admits its successor in the same fields only while the replaced value was in
the base and is gone from the tree, at one occurrence. Keeping the old value beside
the new one, swapping in any other value, or adding any unrelated vendor value is
still refused.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    PACKAGED_CONFIG_DIR,
    PACKAGED_ROUTING_CONFIGS,
    judge_packaged_configs,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.models import (
    ModelDeploymentFactGateResult,
    ModelPackagedConfigTexts,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIGS = _REPO_ROOT / PACKAGED_CONFIG_DIR
_BIFROST = "bifrost_delegation.yaml"
_NEW_ID = "openrouter-nemotron-3-5-super"
_NEW_MODEL = "nvidia/nemotron-3.5-super-vl:free"
_OLD_ID = "openrouter-north-mini-code"
_OLD_MODEL = "cohere/north-mini-code:free"


def _tree() -> dict[str, dict[str, Any]]:
    return {
        name: yaml.safe_load((_CONFIGS / name).read_text(encoding="utf-8"))
        for name in PACKAGED_ROUTING_CONFIGS
    }


def _judge(
    base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]]
) -> ModelDeploymentFactGateResult:
    return judge_packaged_configs(
        [
            ModelPackagedConfigTexts(
                file_name=name,
                base_text=yaml.safe_dump(base[name]),
                head_text=yaml.safe_dump(head[name]),
            )
            for name in PACKAGED_ROUTING_CONFIGS
        ],
        base_ref="base",
    )


def _rung(config: dict[str, Any], backend_id: str) -> dict[str, Any]:
    (rung,) = [b for b in config["backends"] if b["backend_id"] == backend_id]
    return rung


def _with_old_rung() -> dict[str, dict[str, Any]]:
    """The tree as it was before the replacement: the rung still served north-mini-code."""
    tree = copy.deepcopy(_tree())
    rung = _rung(tree[_BIFROST], _NEW_ID)
    rung["backend_id"] = _OLD_ID
    rung["model_name"] = _OLD_MODEL
    return tree


def _new_values(result: ModelDeploymentFactGateResult) -> set[str]:
    return {fact.value for fact in result.new_facts}


def test_the_named_replacement_of_a_dropped_rung_is_admitted() -> None:
    assert not _judge(_with_old_rung(), _tree()).new_facts


def test_keeping_the_old_rung_beside_the_new_one_is_refused() -> None:
    head = copy.deepcopy(_tree())
    old = copy.deepcopy(_rung(head[_BIFROST], _NEW_ID))
    old["backend_id"] = _OLD_ID
    old["model_name"] = _OLD_MODEL
    head[_BIFROST]["backends"].append(old)
    assert {_NEW_ID, _NEW_MODEL} <= _new_values(_judge(_with_old_rung(), head))


def test_a_different_successor_is_refused() -> None:
    head = copy.deepcopy(_tree())
    _rung(head[_BIFROST], _NEW_ID)["model_name"] = "vendor/another-model:free"
    assert "vendor/another-model:free" in _new_values(_judge(_with_old_rung(), head))


def test_an_unrelated_new_vendor_value_is_still_refused() -> None:
    head = copy.deepcopy(_tree())
    extra = copy.deepcopy(_rung(head[_BIFROST], _NEW_ID))
    extra["backend_id"] = "openrouter-some-other-rung"
    extra["model_name"] = "vendor/some-other-model"
    head[_BIFROST]["backends"].append(extra)
    assert {"openrouter-some-other-rung", "vendor/some-other-model"} <= _new_values(
        _judge(_with_old_rung(), head)
    )


def test_the_successor_is_admitted_once_only() -> None:
    head = copy.deepcopy(_tree())
    twin = copy.deepcopy(_rung(head[_BIFROST], _NEW_ID))
    twin["backend_id"] = "openrouter-nemotron-3-5-super-twin"
    head[_BIFROST]["backends"].append(twin)
    assert _NEW_MODEL in _new_values(_judge(_with_old_rung(), head))


def test_the_successor_is_refused_when_the_replaced_value_was_never_in_the_base() -> (
    None
):
    base = copy.deepcopy(_tree())
    base[_BIFROST]["backends"] = [
        b for b in base[_BIFROST]["backends"] if b["backend_id"] != _NEW_ID
    ]
    assert {_NEW_ID, _NEW_MODEL} <= _new_values(_judge(base, _tree()))
