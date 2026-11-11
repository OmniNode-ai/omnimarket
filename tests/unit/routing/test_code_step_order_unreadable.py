# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each fault returns the exact refusal, never an exception."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def _overlay(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(data))
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


def _valid_overlay(tmp_path: Path) -> Path:
    return _overlay(
        tmp_path,
        {
            "engines_by_tier": {"local": [EnumCodeStepEngine.CODE_EDIT.value]},
            "disabled_engines_by_task_class": {},
        },
    )


def test_missing_contracts(tmp_path: Path) -> None:
    result = _run(tmp_path / "no_contracts.yaml", TIERS, _valid_overlay(tmp_path))
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.CONTRACT_UNREADABLE
    assert result.steps == ()


def test_missing_tiers(tmp_path: Path) -> None:
    result = _run(CONTRACTS, tmp_path / "no_tiers.yaml", _valid_overlay(tmp_path))
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.CONTRACT_UNREADABLE
    assert result.steps == ()


def test_missing_overlay(tmp_path: Path) -> None:
    result = _run(CONTRACTS, TIERS, tmp_path / "no_overlay.yaml")
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.OVERLAY_UNREADABLE
    assert result.steps == ()


def test_top_level_list(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts.yaml"
    contracts.write_text("- a\n- b\n")
    result = _run(contracts, TIERS, _valid_overlay(tmp_path))
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.CONTRACT_UNREADABLE
    assert result.steps == ()


def test_unknown_engine_name(tmp_path: Path) -> None:
    overlay = _overlay(
        tmp_path,
        {
            "engines_by_tier": {"local": ["not_a_real_engine"]},
            "disabled_engines_by_task_class": {},
        },
    )
    result = _run(CONTRACTS, TIERS, overlay)
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.OVERLAY_INVALID
    assert result.steps == ()


def test_unknown_task_class(tmp_path: Path) -> None:
    result = _run(
        CONTRACTS, TIERS, _valid_overlay(tmp_path), task_class="no_such_class"
    )
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.TASK_CLASS_UNKNOWN
    assert result.steps == ()


def test_unknown_tier_in_order(tmp_path: Path) -> None:
    doc = yaml.safe_load(CONTRACTS.read_text())
    doc["task_classes"]["code_generation"]["escalation_policy"]["tier_order"] = [
        "no_such_tier"
    ]
    contracts = tmp_path / "contracts.yaml"
    contracts.write_text(yaml.safe_dump(doc))
    result = _run(contracts, TIERS, _valid_overlay(tmp_path))
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.TIER_UNKNOWN
    assert result.steps == ()


def test_every_engine_disabled(tmp_path: Path) -> None:
    overlay = _overlay(
        tmp_path,
        {
            "engines_by_tier": {
                "local": [
                    EnumCodeStepEngine.CODE_EDIT.value,
                    EnumCodeStepEngine.LAB_AGENT.value,
                ]
            },
            "disabled_engines_by_task_class": {
                "code_generation": [
                    EnumCodeStepEngine.CODE_EDIT.value,
                    EnumCodeStepEngine.LAB_AGENT.value,
                ]
            },
        },
    )
    result = _run(CONTRACTS, TIERS, overlay)
    assert result.ok is False
    assert result.refusal == EnumCodeStepRefusal.NO_ENABLED_STEP
    assert result.steps == ()


def test_cli_exit_code_is_three_on_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The entry point the lane step runs exits 3 and prints the typed refusal; it never prints a default order."""
    import io

    from omnimarket.nodes.node_delegation_code_step_order_effect.__main__ import main

    request = ModelCodeStepOrderRequest(
        task_class="code_generation",
        task_class_contracts_path=str(tmp_path / "absent.yaml"),
        routing_tiers_path=str(tmp_path / "absent.yaml"),
        overlay_path=str(tmp_path / "absent.yaml"),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(request.model_dump_json()))
    assert main() == 3
    assert '"contract_unreadable"' in capsys.readouterr().out
