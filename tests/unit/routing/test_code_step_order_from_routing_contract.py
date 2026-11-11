# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the code step order comes from the routing contract and overlay."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_code_step_order_effect.handlers.handler_code_step_order import (
    HandlerCodeStepOrder,
)
from omnimarket.nodes.node_delegation_code_step_order_effect.models.model_code_step_order import (
    EnumCodeStepEngine,
    EnumCodeStepRefusal,
    ModelCodeStepOrderRequest,
)

CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
CONTRACTS = CONFIGS / "task_class_contracts.v1.yaml"
TIERS = CONFIGS / "routing_tiers.yaml"


def _overlay(
    tmp_path: Path,
    engines_by_tier: dict[str, list[str]],
    disabled: dict[str, list[str]],
) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "engines_by_tier": {
                    tier: [EnumCodeStepEngine(e).value for e in engines]
                    for tier, engines in engines_by_tier.items()
                },
                "disabled_engines_by_task_class": {
                    cls: [EnumCodeStepEngine(e).value for e in engines]
                    for cls, engines in disabled.items()
                },
            }
        )
    )
    return path


def _run(
    contracts: Path, tiers: Path, overlay: Path, task_class: str = "code_generation"
) -> object:
    return HandlerCodeStepOrder().handle(
        ModelCodeStepOrderRequest(
            task_class=task_class,
            task_class_contracts_path=str(contracts),
            routing_tiers_path=str(tiers),
            overlay_path=str(overlay),
        )
    )


def test_enabled_engine_order_and_lab_first(tmp_path: Path) -> None:
    overlay = _overlay(
        tmp_path,
        {
            "local": ["code_edit", "lab_agent"],
            "cheap_frontier": [],
            "cheap_cloud": ["codex", "glm"],
            "claude": [],
        },
        {"code_generation": ["codex"]},
    )
    result = _run(CONTRACTS, TIERS, overlay)
    assert result.ok is True
    assert [step.engine for step in result.steps if step.enabled] == [
        EnumCodeStepEngine.CODE_EDIT,
        EnumCodeStepEngine.LAB_AGENT,
        EnumCodeStepEngine.GLM,
    ]
    tiers_doc = yaml.safe_load(TIERS.read_text())
    lab_tier = yaml.safe_load(CONTRACTS.read_text())["lab_first_policy"]["lab_tier"]
    assert result.steps[0].tier == lab_tier
    by_name = {tier["name"]: tier for tier in tiers_doc["tiers"]}
    for step in result.steps:
        assert step.attempts == by_name[step.tier]["max_retries"]


def test_overlay_change_changes_result_without_code_change(tmp_path: Path) -> None:
    overlay = _overlay(
        tmp_path,
        {
            "local": ["code_edit", "lab_agent"],
            "cheap_frontier": [],
            "cheap_cloud": ["codex", "glm"],
            "claude": [],
        },
        {"code_generation": ["codex"]},
    )
    base = _run(CONTRACTS, TIERS, overlay)
    contracts_text = CONTRACTS.read_text()
    doc = yaml.safe_load(contracts_text)
    doc["task_classes"]["code_generation"]["escalation_policy"]["tier_order"] = [
        "cheap_cloud",
        "local",
    ]
    swapped = tmp_path / "contracts.yaml"
    swapped.write_text(yaml.safe_dump(doc))
    changed = _run(swapped, TIERS, overlay)
    assert changed.refusal == EnumCodeStepRefusal.NOT_LAB_FIRST
    assert changed.ok is False
    assert base.ok is True


def test_no_fallback_to_script_order(tmp_path: Path) -> None:
    overlay = _overlay(
        tmp_path,
        {
            "local": ["glm"],
            "cheap_frontier": ["glm"],
            "cheap_cloud": ["glm"],
            "claude": ["glm"],
        },
        {},
    )
    result = _run(CONTRACTS, TIERS, overlay)
    assert result.ok is True
    assert [step.engine for step in result.steps] == [EnumCodeStepEngine.GLM] * len(
        result.steps
    )
    assert all(step.engine == EnumCodeStepEngine.GLM for step in result.steps)


def test_lab_tier_moved_off_front_is_refused(tmp_path: Path) -> None:
    overlay = _overlay(
        tmp_path,
        {
            "local": ["code_edit", "lab_agent"],
            "cheap_frontier": [],
            "cheap_cloud": ["codex", "glm"],
            "claude": [],
        },
        {"code_generation": ["codex"]},
    )
    doc = yaml.safe_load(CONTRACTS.read_text())
    doc["task_classes"]["code_generation"]["escalation_policy"]["tier_order"] = [
        "cheap_cloud",
        "local",
    ]
    moved = tmp_path / "contracts.yaml"
    moved.write_text(yaml.safe_dump(doc))
    result = _run(moved, TIERS, overlay)
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.NOT_LAB_FIRST


def test_overlay_reversal_puts_glm_first_with_no_code_change(tmp_path: Path) -> None:
    """AC4 shape: the same contracts, an overlay listing GLM ahead of the lab engines on the lab tier."""
    reversed_overlay = _overlay(
        tmp_path,
        {
            "local": ["glm", "code_edit", "lab_agent"],
            "cheap_frontier": [],
            "cheap_cloud": ["codex"],
            "claude": [],
        },
        {"code_generation": ["codex"]},
    )
    result = _run(CONTRACTS, TIERS, reversed_overlay)
    assert result.ok is True
    assert (
        next(step.engine for step in result.steps if step.enabled)
        == EnumCodeStepEngine.GLM
    )


def test_contract_without_lab_policy_is_a_typed_refusal(tmp_path: Path) -> None:
    contracts = yaml.safe_load(CONTRACTS.read_text())
    del contracts["lab_first_policy"]
    broken = tmp_path / "contracts.yaml"
    broken.write_text(yaml.safe_dump(contracts))
    overlay = _overlay(tmp_path, {"local": ["code_edit"]}, {})
    result = _run(broken, TIERS, overlay)
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.CONTRACT_UNREADABLE
    assert result.steps == ()
