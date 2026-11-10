# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22609: the onex.nodes entry-point table is sorted, so node pull requests merge.

Every node-adding pull request used to append one line at the end of the
``[project.entry-points."onex.nodes"]`` table, so any two of them conflicted and
a conflicted head gets no pull_request CI. The table is now kept sorted by node
name and this test refuses an unsorted table (the entry-point format script
refuses it too, for the pre-commit hook).

Three reads, each with a failing control:

* AC1: the real table is sorted; the same table with two names swapped is
  reported out of order, and ``scripts/ci/check_entry_point_format.py`` exits 1 on it.
* AC2: a node directory with no entry still fails the node drift validator; with
  its entry it passes.
* AC3: two branches that add different nodes merge without a conflict when each
  inserts at its sorted position; the old append-at-the-end habit conflicts.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DRIFT_SCRIPT = REPO_ROOT / "scripts" / "validate_node_drift.py"
FORMAT_SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_entry_point_format.py"
TABLE_HEADER = '[project.entry-points."onex.nodes"]'
ENTRY_LINE = re.compile(r'^([A-Za-z0-9_.-]+) = "[^"]+"$')


def _entry_lines(text: str) -> list[str]:
    """The entry lines of the onex.nodes table, in file order."""
    match = re.search(
        re.escape(TABLE_HEADER) + r"\n(.*?)(?=\n\[)", text, flags=re.DOTALL
    )
    assert match is not None, "pyproject.toml has no onex.nodes table"
    return [line for line in match.group(1).split("\n") if ENTRY_LINE.match(line)]


def _names(text: str) -> list[str]:
    """The node names of the onex.nodes table, in file order."""
    return [line.split(" = ")[0] for line in _entry_lines(text)]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _out_of_order(names: list[str]) -> list[tuple[str, str]]:
    """Each adjacent (earlier, later) pair of names that is not in sorted order."""
    return [(a, b) for a, b in pairwise(names) if a > b]


def test_real_table_is_sorted() -> None:
    text = PYPROJECT.read_text()
    names = _names(text)
    assert len(names) > 100
    assert not _out_of_order(names), (
        'keep [project.entry-points."onex.nodes"] sorted by name and insert a new '
        "node at its sorted position, never at the end"
    )
    assert len(names) == len(
        tomllib.loads(text)["project"]["entry-points"]["onex.nodes"]
    )


def test_two_swapped_names_are_reported_out_of_order() -> None:
    """Failing control for AC1: two names swapped in the real table are caught."""
    names = _names(PYPROJECT.read_text())
    swapped = [*names]
    swapped[10], swapped[11] = swapped[11], swapped[10]
    assert _out_of_order(swapped) == [(names[11], names[10])]


def _run_format_script(
    tmp_path: Path, pyproject_text: str
) -> subprocess.CompletedProcess[str]:
    """Run a copy of the format script against a pyproject.toml in a throwaway tree."""
    script = tmp_path / "scripts" / "ci" / FORMAT_SCRIPT.name
    script.parent.mkdir(parents=True)
    script.write_text(FORMAT_SCRIPT.read_text())
    (tmp_path / "pyproject.toml").write_text(pyproject_text)
    return subprocess.run(
        [sys.executable, str(script), "pyproject.toml"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_format_script_passes_the_real_sorted_table(tmp_path: Path) -> None:
    """AC1: the format script exits 0 on the real, sorted table."""
    result = _run_format_script(tmp_path, PYPROJECT.read_text())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "entries OK" in result.stdout


def test_format_script_exits_1_on_two_swapped_names(tmp_path: Path) -> None:
    """AC1 failing control: the real table with two adjacent lines swapped exits 1."""
    text = PYPROJECT.read_text()
    lines = _entry_lines(text)
    swapped = text.replace(
        lines[10] + "\n" + lines[11] + "\n", lines[11] + "\n" + lines[10] + "\n", 1
    )
    assert swapped != text
    result = _run_format_script(tmp_path, swapped)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not sorted by name" in result.stdout


def test_a_node_without_an_entry_fails_the_drift_validator(tmp_path: Path) -> None:
    """AC2: dropping the entry of a node that has a contract fails ``pyproject_entry``."""
    drift = _load(DRIFT_SCRIPT, "validate_node_drift_omn22609")
    node_dir = tmp_path / "node_example_new_compute"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "name: node_example_new_compute\nnode_type: compute\nhandler:\n  module: x\n"
    )
    registered = drift.validate_node(node_dir, {"node_example_new_compute"})
    assert registered.passed
    left_out = drift.validate_node(node_dir, set())
    assert not left_out.passed
    assert [f.check for f in left_out.findings if f.level == "FAIL"] == [
        "pyproject_entry"
    ]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def _with_entries_inserted(
    text: str, new_lines: list[str], *, sorted_insert: bool
) -> str:
    """Add entry lines at their sorted position, or after the last entry."""
    lines = _entry_lines(text)
    for new in new_lines:
        if sorted_insert:
            key = new.split(" = ")[0]
            after = [line for line in lines if line.split(" = ")[0] < key]
            anchor = after[-1]
        else:
            anchor = lines[-1]
        text = text.replace(anchor + "\n", anchor + "\n" + new + "\n", 1)
        lines = _entry_lines(text)
    return text


def _merge_two_branches(
    tmp_path: Path, *, sorted_insert: bool, first: str, second: str
) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "dev").returncode == 0
    base = PYPROJECT.read_text()
    (repo / "pyproject.toml").write_text(base)
    _git(repo, "add", "pyproject.toml")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    for branch, name in (("a", first), ("b", second)):
        assert _git(repo, "checkout", "-q", "-b", branch, "dev").returncode == 0
        line = f'{name} = "omnimarket.nodes.{name}"'
        (repo / "pyproject.toml").write_text(
            _with_entries_inserted(base, [line], sorted_insert=sorted_insert)
        )
        _git(repo, "commit", "-q", "-am", name)
    assert _git(repo, "checkout", "-q", "a").returncode == 0
    return _git(repo, "merge", "--no-edit", "b")


def test_two_nodes_added_on_two_branches_merge_without_conflict(
    tmp_path: Path,
) -> None:
    """AC3: sorted insertion of two different nodes merges clean."""
    merged = _merge_two_branches(
        tmp_path,
        sorted_insert=True,
        first="node_cc0925_probe_compute",
        second="node_merge_cc0925_probe_effect",
    )
    assert merged.returncode == 0, merged.stdout + merged.stderr
    text = (tmp_path / "repo" / "pyproject.toml").read_text()
    names = _names(text)
    assert "node_cc0925_probe_compute" in names
    assert "node_merge_cc0925_probe_effect" in names
    assert names == sorted(names)


def test_appending_both_nodes_at_the_end_conflicts(tmp_path: Path) -> None:
    """Failing control for AC3: the old habit, both appended at the end, conflicts."""
    merged = _merge_two_branches(
        tmp_path,
        sorted_insert=False,
        first="node_cc0925_probe_compute",
        second="node_merge_cc0925_probe_effect",
    )
    assert merged.returncode != 0
    assert "CONFLICT" in merged.stdout
