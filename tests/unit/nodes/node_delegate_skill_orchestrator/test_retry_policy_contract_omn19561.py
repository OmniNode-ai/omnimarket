# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19561 AC1: no delegation contract declares a retry_policy no handler honours."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES_DIR = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
_DELEGATION_NODES = ("node_delegate_skill_orchestrator", "node_delegation_orchestrator")


def _declared_retry_policy(contract: dict[str, Any]) -> Any:
    return (contract.get("error_handling") or {}).get("retry_policy")


def _load(node: str) -> dict[str, Any]:
    path = _NODES_DIR / node / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("node", _DELEGATION_NODES)
def test_delegation_contract_declares_no_unhonoured_retry_policy(node: str) -> None:
    # Neither handler reads error_handling.retry_policy: the workflow's retries
    # come from routing_tiers.yaml (per-tier max_retries) and same-tier backend
    # fallback, and the skill handler never retries a dispatch timeout. A
    # declared max_retries here would be a promise nothing keeps.
    assert _declared_retry_policy(_load(node)) is None


@pytest.mark.parametrize("node", _DELEGATION_NODES)
def test_delegation_contract_keeps_its_error_types(node: str) -> None:
    error_types = _load(node)["error_handling"]["error_types"]
    assert error_types, f"{node} lost its error_types along with retry_policy"


def test_detector_flags_a_declared_retry_policy() -> None:
    declared = {"error_handling": {"retry_policy": {"max_retries": 1}}}
    assert _declared_retry_policy(declared) == {"max_retries": 1}
    assert _declared_retry_policy({"error_handling": {"error_types": []}}) is None
