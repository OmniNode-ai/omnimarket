# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only ``projection/runner.py`` reads the projection binding overlay variables.

F6 of OMN-20159, the projection side of the env-read discipline (OMN-10915):
the runtime overlay variable and the read overlay variable are each read in
``projection/runner.py`` and nowhere else. Every other module reaches a binding
through the runner's public resolvers, so which variable selects which store is
decided in one place. A second reader is how one variable came to select both
the read node's database and the claim store's.

``scripts/ci/check_delegation_env_reads.py`` covers delegation modules only and
any env read there; this covers every module under ``src/omnimarket`` for these
two names, by literal or by the runner's constant.

A module "touches" a variable when its code (not a docstring or comment) holds
the variable's name as a string literal, or names the runner's constant for it.
A module that builds the name (concatenation, an f-string, a lookup) is not
found by this scan.

That the runner does read each variable is behaviour, checked where it shows:
the F1 to F5 tests beside the read node and the runner's own tests fail when
the runner stops following either variable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import omnimarket

_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"
_CONSTANTS = {
    _RUNTIME_ENV: "PROJECTION_RUNTIME_BINDING_OVERLAY_ENV",
    _READ_ENV: "PROJECTION_READ_BINDING_OVERLAY_ENV",
}
_SRC = Path(omnimarket.__file__).resolve().parent
_RUNNER = "projection/runner.py"


def _touches(source: str, variable: str) -> bool:
    constant = _CONSTANTS[variable]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and node.value == variable:
            return True
        if isinstance(node, ast.Name) and node.id == constant:
            return True
        if isinstance(node, ast.Attribute) and node.attr == constant:
            return True
        if isinstance(node, ast.alias) and constant in {node.name, node.asname}:
            return True
    return False


def _modules_touching(root: Path, variable: str) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if _touches(path.read_text(encoding="utf-8"), variable)
    }


@pytest.mark.unit
@pytest.mark.parametrize("variable", [_RUNTIME_ENV, _READ_ENV])
def test_only_the_runner_reads_the_binding_overlay_variable(variable: str) -> None:
    assert _modules_touching(_SRC, variable) == {_RUNNER}


@pytest.mark.unit
def test_the_scan_catches_a_second_reader(tmp_path: Path) -> None:
    """Positive control: a planted reader is found by literal and by constant."""
    root = tmp_path / "omnimarket"
    (root / "projection").mkdir(parents=True)
    (root / "projection" / "runner.py").write_text(
        f'import os\nPROJECTION_READ_BINDING_OVERLAY_ENV = "{_READ_ENV}"\n'
        "os.environ.get(PROJECTION_READ_BINDING_OVERLAY_ENV)\n",
        encoding="utf-8",
    )
    (root / "by_literal.py").write_text(
        f'import os\npath = os.environ.get("{_READ_ENV}", "")\n', encoding="utf-8"
    )
    (root / "by_constant.py").write_text(
        "import os\n"
        "from omnimarket.projection.runner import "
        "PROJECTION_READ_BINDING_OVERLAY_ENV\n"
        "path = os.environ[PROJECTION_READ_BINDING_OVERLAY_ENV]\n",
        encoding="utf-8",
    )
    (root / "by_attribute.py").write_text(
        "import os\nfrom omnimarket.projection import runner\n"
        "path = os.getenv(runner.PROJECTION_READ_BINDING_OVERLAY_ENV)\n",
        encoding="utf-8",
    )
    (root / "docstring_only.py").write_text(
        f'"""Mentions {_READ_ENV} in prose only."""\n', encoding="utf-8"
    )

    assert _modules_touching(root, _READ_ENV) == {
        "projection/runner.py",
        "by_literal.py",
        "by_constant.py",
        "by_attribute.py",
    }
