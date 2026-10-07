# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No module imports the removed delegate-skill request/response shims.

This test imports nothing from the node package, so a stale shim import
anywhere in the node's import chain is reported by path instead of breaking
collection.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SHIM_PACKAGE = "omnimarket.nodes.node_delegate_skill_orchestrator.models"
_SHIM_MODULES = frozenset(
    {
        f"{_SHIM_PACKAGE}.model_delegate_skill_request",
        f"{_SHIM_PACKAGE}.model_delegate_skill_response",
    }
)
_SHIM_NAMES = frozenset({"ModelDelegateSkillRequest", "ModelDelegateSkillResponse"})


def _shim_import_lines(source: str) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        imported = {alias.name for alias in node.names}
        if node.module in _SHIM_MODULES or (
            node.module == _SHIM_PACKAGE and imported & _SHIM_NAMES
        ):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.unit
def test_no_module_imports_the_removed_delegate_skill_shims() -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{line}"
        for root in (_REPO_ROOT / "src", _REPO_ROOT / "tests")
        for path in sorted(root.rglob("*.py"))
        for line in _shim_import_lines(path.read_text())
    ]
    assert offenders == []


@pytest.mark.unit
def test_the_scan_catches_both_shim_import_forms() -> None:
    source = (
        f"from {_SHIM_PACKAGE}.model_delegate_skill_request import (\n"
        "    ModelDelegateSkillRequest,\n"
        ")\n"
        f"from {_SHIM_PACKAGE} import ModelDelegateSkillResponse\n"
        f"from {_SHIM_PACKAGE} import ModelDelegationReapContext\n"
        "from omnimarket.models.delegation.wire.model_delegate_skill_request import (\n"
        "    ModelDelegateSkillRequest,\n"
        ")\n"
    )
    assert _shim_import_lines(source) == [1, 4]
