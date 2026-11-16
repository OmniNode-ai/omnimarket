# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retired sibling package has no running reference in omnimarket (OMN-22385).

The PR OCC stamp schema lives in omnibase_core since 0.47.60, so no module under
``src/omnimarket`` imports the retired package, ``pyproject.toml`` declares no
requirement, override or mypy module for it, no node ``metadata.yaml`` lists it
as a dependency, and nothing in CI, the benchmarks, the scripts, the source or
the tests installs it, checks it out or imports it.

The package's name is assembled from parts below so that this file, which is the
guard, does not itself carry the name it forbids.
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
_STEM = "omnibase"
_TAIL = "compat"
_MODULE = f"{_STEM}_{_TAIL}"
_COMPAT = re.compile(rf"{_STEM}[_-]{_TAIL}", re.IGNORECASE)

#: Where a running reference could live: the CI workflows, the benchmarks, the
#: scripts, the source and the tests. Data files (fixtures, recorded bundles) are
#: history and are read by no install, so only these suffixes are scanned.
SCANNED_DIRS = (".github/workflows", "benchmarks", "scripts", "src", "tests")
SCANNED_SUFFIXES = frozenset({".py", ".yml", ".yaml", ".sh", ".toml", ".cfg"})

#: A line that installs, clones or checks the package out.
_INSTALL_LINE = re.compile(
    rf"\b(?:pip3?|uv|poetry|pipx|conda)\b[^\n#]*?\b(?:install|add)\b[^\n]*{_COMPAT.pattern}"
    rf"|\bgit\s+clone\b[^\n]*{_COMPAT.pattern}"
    rf"|^\s*repository:\s*\S*{_COMPAT.pattern}",
    re.IGNORECASE | re.MULTILINE,
)


def _imports_retired(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == _MODULE for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").split(".")[0] == _MODULE
        ):
            return True
    return False


def _running_references(root: Path) -> list[str]:
    """Files under the scanned directories of ``root`` that import, install or check it out."""
    offenders: list[str] = []
    for directory in SCANNED_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _INSTALL_LINE.search(text):
                offenders.append(str(path.relative_to(root)))
            elif path.suffix == ".py" and _MODULE in text:
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                if _imports_retired(tree):
                    offenders.append(str(path.relative_to(root)))
    return offenders


@pytest.mark.unit
def test_no_module_under_src_imports_the_retired_package() -> None:
    offenders = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in SRC_ROOT.rglob("*.py")
        if _MODULE in (text := path.read_text(encoding="utf-8"))
        and _imports_retired(ast.parse(text))
    )
    assert offenders == []


@pytest.mark.unit
def test_import_scan_flags_an_import_of_the_retired_package() -> None:
    """Positive control: the scan reports both import forms."""
    assert _imports_retired(ast.parse(f"from {_MODULE}.contracts import x"))
    assert _imports_retired(ast.parse(f"import {_MODULE}.contracts"))
    assert not _imports_retired(ast.parse("from omnibase_core.contracts import x"))


@pytest.mark.unit
def test_pyproject_names_no_retired_requirement_or_override() -> None:
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
def test_no_node_metadata_lists_the_retired_dependency() -> None:
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


@pytest.mark.unit
def test_nothing_in_the_running_trees_installs_checks_out_or_imports_it() -> None:
    """CI workflows, benchmarks, scripts, source and tests name no install of it.

    Falsifier: any file under those directories installs the package beside a
    historical tree, clones or checks it out, or imports it; or the vendoring
    helper the replays once used is back.
    """
    assert _running_references(REPO_ROOT) == []
    assert not (SRC_ROOT / "handlers" / "vendor_compat.py").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("relative", "body"),
    [
        (
            ".github/workflows/other.yml",
            f"      - run: uv pip install '{_STEM}-{_TAIL}==0.5.7'\n",
        ),
        ("scripts/build.sh", f"pip install {_STEM}_{_TAIL}\n"),
        ("benchmarks/run.py", f"import {_MODULE}\n"),
        ("src/omnimarket/x.py", f"from {_MODULE}.y import Z\n"),
        (
            "tests/helper.py",
            f"CMD = 'git clone https://example.invalid/{_MODULE}.git'\n",
        ),
        (
            ".github/workflows/co.yml",
            f"        with:\n          repository: org/{_MODULE}\n",
        ),
    ],
)
def test_an_install_planted_in_any_scanned_tree_fails_the_scan(
    tmp_path: Path, relative: str, body: str
) -> None:
    """Control: a reference outside any two files is found, and a clean tree is not."""
    assert _running_references(tmp_path) == []
    planted = tmp_path / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(body, encoding="utf-8")
    assert _running_references(tmp_path) == [relative]
