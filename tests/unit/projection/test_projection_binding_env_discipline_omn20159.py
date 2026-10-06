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

A module "touches" a variable when its code (not a docstring or comment)
holds the variable's name as a string literal; names the runner's constant for
it, or another module-level name ``runner.py`` binds to that constant (directly
or through another such name), by name, attribute, import or alias; holds one of those names as a string literal (a
``getattr`` on the runner); or assigns a name equal to the variable's name in
any letter case (a pydantic settings field, which reads the variable because
``Settings`` is case-insensitive with no prefix). A module that builds either
name at run time (concatenation, an f-string, a computed lookup) is not found
by this scan.

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


def _names_for(runner_source: str, variable: str) -> set[str]:
    """The runner's constant and every module-level name the runner binds to it.

    One pass in source order finds a chain (``B = A`` after ``A = CONSTANT``),
    because a module-level name is bound before a later line can use it.
    """
    names = {_CONSTANTS[variable]}
    for node in ast.parse(runner_source).body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not (isinstance(value, ast.Name) and value.id in names):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _touches(source: str, variable: str, names: set[str]) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and (
            node.value == variable or node.value in names
        ):
            return True
        if isinstance(node, ast.Name):
            if node.id in names:
                return True
            if isinstance(node.ctx, ast.Store) and node.id.lower() == variable.lower():
                return True
        if isinstance(node, ast.Attribute) and node.attr in names:
            return True
        if isinstance(node, ast.alias) and names & {node.name, node.asname}:
            return True
    return False


def _modules_touching(root: Path, variable: str) -> set[str]:
    names = _names_for((root / _RUNNER).read_text(encoding="utf-8"), variable)
    return {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if _touches(path.read_text(encoding="utf-8"), variable, names)
    }


@pytest.mark.unit
@pytest.mark.parametrize("variable", [_RUNTIME_ENV, _READ_ENV])
def test_only_the_runner_reads_the_binding_overlay_variable(variable: str) -> None:
    assert _modules_touching(_SRC, variable) == {_RUNNER}


@pytest.mark.unit
def test_the_scan_catches_a_second_reader(tmp_path: Path) -> None:
    """Positive control: a planted reader is found by every route the scan covers."""
    root = tmp_path / "omnimarket"
    (root / "projection").mkdir(parents=True)
    (root / "projection" / "runner.py").write_text(
        f'import os\nPROJECTION_READ_BINDING_OVERLAY_ENV = "{_READ_ENV}"\n'
        "READ_OVERLAY_NAME = PROJECTION_READ_BINDING_OVERLAY_ENV\n"
        "READ_OVERLAY_TYPED: str = PROJECTION_READ_BINDING_OVERLAY_ENV\n"
        "READ_OVERLAY_ALIAS = READ_OVERLAY_NAME\n"
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
    # Settings is case-insensitive with no prefix, so this field reads the
    # variable without ever naming it in capitals.
    (root / "by_settings_field.py").write_text(
        "from pydantic_settings import BaseSettings\n"
        "class Settings(BaseSettings):\n"
        f'    {_READ_ENV.lower()}: str = ""\n',
        encoding="utf-8",
    )
    (root / "by_second_name.py").write_text(
        "import os\nfrom omnimarket.projection.runner import READ_OVERLAY_NAME\n"
        "path = os.environ[READ_OVERLAY_NAME]\n",
        encoding="utf-8",
    )
    (root / "by_typed_name.py").write_text(
        "import os\nfrom omnimarket.projection.runner import READ_OVERLAY_TYPED\n"
        "path = os.environ[READ_OVERLAY_TYPED]\n",
        encoding="utf-8",
    )
    (root / "by_chained_name.py").write_text(
        "import os\nfrom omnimarket.projection.runner import READ_OVERLAY_ALIAS\n"
        "path = os.environ[READ_OVERLAY_ALIAS]\n",
        encoding="utf-8",
    )
    (root / "by_constant_name_string.py").write_text(
        "import os\nfrom omnimarket.projection import runner\n"
        'path = os.environ[getattr(runner, "PROJECTION_READ_BINDING_OVERLAY_ENV")]\n',
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
        "by_settings_field.py",
        "by_second_name.py",
        "by_typed_name.py",
        "by_chained_name.py",
        "by_constant_name_string.py",
    }
