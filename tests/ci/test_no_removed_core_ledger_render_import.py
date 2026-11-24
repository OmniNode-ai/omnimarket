# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No omnimarket file imports a core module that the newest omnibase-core no longer ships.

omnibase_core removed ``omnibase_core.cli.cli_work_ledger_render`` (core#2361). The
latest-core smoke job installs the newest core release without the lockfile and imports
every node module, so a leftover import of the removed module turns that job red while
the locked core still carries the module and every other job stays green. The name of
the ledger path environment variable is omnimarket's own constant now.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
REMOVED_CORE_MODULES = frozenset({"omnibase_core.cli.cli_work_ledger_render"})
SCANNED_ROOTS = ("src", "tests", "scripts")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _offenders() -> list[str]:
    offenders: list[str] = []
    for root in SCANNED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if REMOVED_CORE_MODULES & _imported_modules(path):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    return offenders


def test_no_file_imports_a_module_core_removed() -> None:
    assert _offenders() == []


def test_the_scan_sees_an_import_of_a_removed_module(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from omnibase_core.cli.cli_work_ledger_render import MD_LEDGER_PATH_ENV\n",
        encoding="utf-8",
    )
    assert _imported_modules(planted) & REMOVED_CORE_MODULES
