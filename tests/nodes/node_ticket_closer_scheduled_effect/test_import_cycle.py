# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No import cycle runs through the scheduled ticket closer's modules (OMN-24301).

Code scanning flags ``py/unsafe-cyclic-import`` for a cycle even when one side of it is guarded by
``TYPE_CHECKING``. The graph below reads every import statement, guarded or not, and the control
shows the reader seeing a guarded cycle, so the real reading cannot pass by reading too little.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
CLOSER_PACKAGE = "omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers"
CLOSER_HANDLERS_DIR = SRC.joinpath(*CLOSER_PACKAGE.split("."))


def _module_file(root: Path, dotted: str) -> Path | None:
    base = root.joinpath(*dotted.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _imports_of(root: Path, dotted: str) -> set[str]:
    """Every module this module imports anywhere in its text, inside a guard or a function too."""
    path = _module_file(root, dotted)
    if path is None:
        return set()
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [
                f"{node.module}.{alias.name}" for alias in node.names
            ]
        else:
            continue
        found.update(n for n in names if _module_file(root, n) is not None)
    return found


def find_cycles_through(root: Path, starts: list[str]) -> list[list[str]]:
    """Return one import cycle for each start module that can reach itself, else nothing."""
    cycles: list[list[str]] = []
    for start in starts:
        seen: set[str] = set()
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            current, path = stack.pop()
            for target in sorted(_imports_of(root, current)):
                if target == start:
                    cycles.append([*path, start])
                    stack.clear()
                    break
                if target not in seen:
                    seen.add(target)
                    stack.append((target, [*path, target]))
    return cycles


def _closer_modules() -> list[str]:
    return sorted(
        f"{CLOSER_PACKAGE}.{p.stem}"
        for p in CLOSER_HANDLERS_DIR.glob("*.py")
        if p.stem != "__init__"
    )


def test_control_a_cycle_guarded_by_type_checking_is_read_as_a_cycle(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "left.py").write_text(
        "from pkg.right import Right\n\nclass Left: ...\n", encoding="utf-8"
    )
    (package / "right.py").write_text(
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
        "    from pkg.left import Left\n\nclass Right: ...\n",
        encoding="utf-8",
    )
    cycles = find_cycles_through(tmp_path, ["pkg.left"])
    assert cycles == [["pkg.left", "pkg.right", "pkg.left"]]


def test_control_a_function_local_import_is_read_as_a_cycle(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "left.py").write_text("import pkg.right\n", encoding="utf-8")
    (package / "right.py").write_text(
        "def use() -> None:\n    import pkg.left\n", encoding="utf-8"
    )
    assert find_cycles_through(tmp_path, ["pkg.left"]) != []


def test_the_closer_modules_were_found() -> None:
    modules = _closer_modules()
    assert f"{CLOSER_PACKAGE}.bound_profile" in modules
    assert f"{CLOSER_PACKAGE}.handler_ticket_closer_scheduled" in modules


def test_no_closer_module_is_part_of_an_import_cycle() -> None:
    cycles = find_cycles_through(SRC, _closer_modules())
    assert cycles == [], "import cycles through the closer node: " + "; ".join(
        " -> ".join(cycle) for cycle in cycles
    )


@pytest.mark.parametrize("module", _closer_modules())
def test_each_closer_module_imports_alone(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
