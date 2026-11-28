# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26678: the lab integration-proof item of the contract names a resolvable command.

The Done gate runs every command check of a contract through an executable it can
resolve. An item whose command began with a bare ``onex`` read
``INVALID_CHECK_VALUE_NOT_A_COMMAND`` on the verifier host, so the lab reading that
proves AC3 (a dev build writes one row per check) never ran. The item is replaced by
``dod-omn26678-integration-proof-lab``, whose command starts with ``uv run``.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-26678.yaml"
LAB_ITEM_ID = "dod-omn26678-integration-proof-lab"
STALE_ITEM_ID = "dod-omn26678-integration-proof"
STATE_ROOT_TEST = "tests/nodes/node_build_delegation_checks/test_state_root.py"


def command_problems(contract: Mapping[str, Any]) -> list[str]:
    """Every way the lab integration-proof item fails to name a resolvable command."""
    items = {
        str(i.get("id")): i
        for i in contract.get("dod_evidence") or []
        if isinstance(i, Mapping)
    }
    if STALE_ITEM_ID in items:
        return [f"stale item {STALE_ITEM_ID} still carried"]
    item = items.get(LAB_ITEM_ID)
    if item is None:
        return [f"no item {LAB_ITEM_ID}"]
    problems: list[str] = []
    checks = [c for c in item.get("checks") or [] if c.get("check_type") == "command"]
    if not checks:
        problems.append("no command check")
    for check in checks:
        value = str(check.get("check_value", ""))
        first = shlex.split(value)[0] if value.strip() else ""
        if not first or shutil.which(first) is None:
            problems.append(f"first token {first!r} is not a resolvable executable")
        if STATE_ROOT_TEST not in value:
            problems.append(f"command does not run {STATE_ROOT_TEST}")
    return problems


def _synthetic(command: str, item_id: str = LAB_ITEM_ID) -> dict[str, Any]:
    return {
        "dod_evidence": [
            {
                "id": item_id,
                "checks": [{"check_type": "command", "check_value": command}],
            }
        ]
    }


def test_bare_onex_command_is_refused() -> None:
    """Known bad: the earlier item's command began with a bare, unresolvable onex."""
    bad = _synthetic(f"onex-not-installed-here lab-work run -- {STATE_ROOT_TEST}")
    assert any("not a resolvable executable" in p for p in command_problems(bad))


def test_stale_item_is_refused() -> None:
    assert command_problems(_synthetic("uv run pytest", STALE_ITEM_ID))


def test_command_that_skips_the_state_root_test_is_refused() -> None:
    assert any(
        "does not run" in p for p in command_problems(_synthetic("uv run pytest"))
    )


def test_contract_lab_item_command_resolves() -> None:
    """Known good: the contract's lab item starts with uv and runs the state root test."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert command_problems(contract) == []
