from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

NODES_ROOT = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"


def _load_contract(node_name: str) -> dict[str, Any]:
    with (NODES_ROOT / node_name / "contract.yaml").open() as handle:
        contract = yaml.safe_load(handle)
    assert isinstance(contract, dict)
    return contract


def _assert_effects_runtime_owner(node_name: str) -> None:
    contract = _load_contract(node_name)
    descriptor = contract.get("descriptor")
    assert isinstance(descriptor, dict)
    assert "effects" in descriptor.get("runtime_profiles", [])


def test_session_command_consumers_are_effects_runtime_owned() -> None:
    """Session command consumers must not join dead/main runtime groups."""
    _assert_effects_runtime_owner("node_session_orchestrator")
    _assert_effects_runtime_owner("node_session_bootstrap")
