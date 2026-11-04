# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnibase_compat is retired: nothing in omnimarket may import or depend on it (OMN-22385).

The PR OCC stamp schema lives in omnibase_core since 0.47.60, so no module under
``src/omnimarket`` imports ``omnibase_compat``, ``pyproject.toml`` declares no
``omnibase-compat`` requirement, override or mypy module, and no node
``metadata.yaml`` lists it as a dependency.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "omnimarket"
_COMPAT = re.compile(r"omnibase[_-]compat", re.IGNORECASE)


def _imports_compat(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name.split(".")[0] == "omnibase_compat" for alias in node.names
            ):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").split(".")[0] == "omnibase_compat"
        ):
            return True
    return False


@pytest.mark.unit
def test_no_module_under_src_imports_omnibase_compat() -> None:
    offenders = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in SRC_ROOT.rglob("*.py")
        if _imports_compat(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert offenders == []


@pytest.mark.unit
def test_import_scan_flags_an_omnibase_compat_import() -> None:
    """Positive control: the scan reports both import forms."""
    assert _imports_compat(ast.parse("from omnibase_compat.contracts import x"))
    assert _imports_compat(ast.parse("import omnibase_compat.contracts"))
    assert not _imports_compat(ast.parse("from omnibase_core.contracts import x"))


@pytest.mark.unit
def test_pyproject_names_no_omnibase_compat_requirement_or_override() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements: list[str] = list(data["project"].get("dependencies", []))
    for group in data["project"].get("optional-dependencies", {}).values():
        requirements.extend(group)
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(item for item in group if isinstance(item, str))
    requirements.extend(
        data.get("tool", {}).get("uv", {}).get("override-dependencies", [])
    )
    mypy_modules = [
        module
        for override in data.get("tool", {}).get("mypy", {}).get("overrides", [])
        for module in (
            [override["module"]]
            if isinstance(override.get("module"), str)
            else override.get("module", [])
        )
    ]
    offenders = [item for item in requirements + mypy_modules if _COMPAT.search(item)]
    assert offenders == []


@pytest.mark.unit
def test_no_node_metadata_lists_omnibase_compat_dependency() -> None:
    offenders = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (SRC_ROOT / "nodes").glob("*/metadata.yaml")
        if any(
            _COMPAT.search(str(dep))
            for dep in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
                "dependencies"
            )
            or []
        )
    )
    assert offenders == []
