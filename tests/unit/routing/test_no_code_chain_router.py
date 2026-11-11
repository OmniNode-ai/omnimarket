# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6: the budget node has no resolve_chain operation and no delegation.code_chain string."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
BUDGET = ROOT / "src" / "omnimarket" / "nodes" / "node_delegation_code_budget_compute"


def test_budget_node_has_no_resolve_chain_operation() -> None:
    contract = yaml.safe_load((BUDGET / "contract.yaml").read_text())
    operations = [h["operation"] for h in contract["handler_routing"]["handlers"]]
    assert "resolve_chain" not in operations
    for handler_file in (BUDGET / "handlers").glob("*.py"):
        assert "resolve_chain" not in handler_file.read_text()


def test_no_delegation_code_chain_string() -> None:
    src = ROOT / "src" / "omnimarket"
    for pattern in ("*.py", "*.yaml"):
        for path in src.rglob(pattern):
            if "test_no_code_chain_router" in path.name:
                continue
            assert "delegation.code_chain" not in path.read_text()
