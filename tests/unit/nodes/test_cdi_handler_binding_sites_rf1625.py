# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Six handler modules of five nodes resolve their handlers through the node's binding module."""

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

_NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_SITES = (
    (
        "node_operator_access_mode_effect",
        "handlers/handler_operator_hook_cli.py",
    ),
    (
        "node_llm_delegation_call_effect",
        "handlers/handler_delegation_target.py",
    ),
    (
        "node_lane_placement_recovery_effect",
        "handlers/handler_lane_placement_recovery.py",
    ),
    (
        "node_operator_capture_effect",
        "handlers/handler_capture_process.py",
    ),
    (
        "node_operator_capture_effect",
        "handlers/handler_capture_cli.py",
    ),
    (
        "node_idle_slot_fill_effect",
        "handlers/handler_idle_slot_fill.py",
    ),
)
_RULE = "handler_hand_construction"


def _rules(path: Path) -> list[str]:
    result = ValidatorContractsAndDi().validate(path)
    return [str(issue.code) for issue in result.issues]


@pytest.mark.unit
@pytest.mark.parametrize(("node", "module"), _SITES)
def test_handler_module_holds_no_hand_construction(node: str, module: str) -> None:
    assert _RULE not in _rules(_NODES_DIR / node / module)
    assert (_NODES_DIR / node / "container").is_dir()


@pytest.mark.unit
def test_checker_flags_an_inline_construction(tmp_path: Path) -> None:
    source = tmp_path / "handler_probe.py"
    source.write_text(
        "from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (\n"
        "    HandlerCaptureDigest,\n)\n\n\ndef run() -> object:\n"
        "    return HandlerCaptureDigest()\n"
    )
    assert _RULE in _rules(source)
