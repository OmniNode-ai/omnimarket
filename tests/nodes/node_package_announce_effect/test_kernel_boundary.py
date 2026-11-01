# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The infra kernel reads the catalog and never reaches the announcer (OMN-21597).

The settled design: nodes announce themselves on the bus when they start, and the
runtime reads the registry catalog. The infra kernel imports nothing from omnimarket
and names no omnimarket announcer, so a kernel can run with this package absent.
The ratchet reads the installed omnibase_infra runtime as text and carries planted
violations, so a ratchet that can no longer fail is itself a failing test.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

ANNOUNCER_PACKAGE = "node_package_announce_effect"


def omnimarket_reaches(directory: Path) -> dict[str, list[str]]:
    """Files under ``directory`` that import omnimarket or name the announcer package.

    Each file maps to the reasons: an ``import``/``from`` of ``omnimarket``, or the
    announcer's package name appearing in a string (a dynamic import).
    """
    found: dict[str, list[str]] = {}
    for path in sorted(directory.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        reasons: list[str] = []
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.Import):
                reasons += [
                    f"import {a.name}"
                    for a in node.names
                    if a.name.split(".")[0] == "omnimarket"
                ]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.split(".")[0] == "omnimarket":
                    reasons.append(f"from {node.module}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ANNOUNCER_PACKAGE in node.value
            ):
                reasons.append(f"string {node.value!r}")
        if reasons:
            found[str(path.relative_to(directory))] = reasons
    return found


def installed_infra_runtime() -> Path:
    spec = importlib.util.find_spec("omnibase_infra.runtime")
    assert spec is not None
    assert spec.submodule_search_locations is not None
    return Path(next(iter(spec.submodule_search_locations)))


@pytest.mark.unit
def test_the_infra_runtime_imports_nothing_from_omnimarket() -> None:
    assert omnimarket_reaches(installed_infra_runtime()) == {}, (
        "falsifier: the kernel imports omnimarket or names its announcer"
    )


@pytest.mark.unit
def test_planted_kernel_imports_of_the_announcer_are_caught(tmp_path: Path) -> None:
    (tmp_path / "control.py").write_text(
        "from omnibase_core.models.nodes.package_announce.model_node_catalog import ModelNodeCatalog\n",
        encoding="utf-8",
    )
    (tmp_path / "planted_static.py").write_text(
        f"from omnimarket.nodes.{ANNOUNCER_PACKAGE}.announce_step0 import AnnounceStep0\n",
        encoding="utf-8",
    )
    (tmp_path / "planted_plain_import.py").write_text(
        "import omnimarket\n", encoding="utf-8"
    )
    (tmp_path / "planted_dynamic.py").write_text(
        f'import importlib\nimportlib.import_module("omnimarket.nodes.{ANNOUNCER_PACKAGE}")\n',
        encoding="utf-8",
    )
    assert set(omnimarket_reaches(tmp_path)) == {
        "planted_static.py",
        "planted_plain_import.py",
        "planted_dynamic.py",
    }
