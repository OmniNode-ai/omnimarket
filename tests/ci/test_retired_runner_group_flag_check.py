# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retired-flag check of the runner group flag change fails on any hit (OMN-24984).

The contract of the review ticket carries a shell check that searches src/ and tests/ for the retired
runner group flag name. This test runs that command from the contract file in a scratch tree, with
the real contract of the change beside it, and plants the name in different kinds of file: the check
must fail on every hit, whatever the contract of the change says.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
# built from two literals so a search of src/ and tests/ for the whole name stays empty
RETIRED = "restricted_to_selected" + "_workflows"


def _check() -> str:
    loaded = yaml.safe_load(
        (ROOT / "contracts" / "OMN-24963.yaml").read_text(encoding="utf-8")
    )
    (entry,) = [
        e for e in loaded["dod_evidence"] if e["id"] == "dod-omn24963-ac2-interface"
    ]
    (check,) = [c for c in entry["checks"] if c["check_type"] == "command"]
    value = check["check_value"]
    assert isinstance(value, str)
    return value


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "contracts").mkdir()
    shutil.copy(ROOT / "contracts" / "OMN-24871.yaml", tmp_path / "contracts")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "reader.py").write_text("FLAG = 'restricted_to_workflows'\n")
    return tmp_path


def _run(tree: Path) -> int:
    return subprocess.run(
        ["/bin/sh", "-c", _check()], cwd=tree, capture_output=True, check=False
    ).returncode


def test_a_tree_without_the_retired_name_passes(tmp_path: Path) -> None:
    """The positive control: the real contract of the change and a tree that names only the new flag."""
    assert _run(_tree(tmp_path)) == 0


@pytest.mark.parametrize(
    "relative",
    [
        "tests/fixture_body.json",
        "tests/notes.md",
        "src/reader_constants.py",
        "src/overlay.yaml",
        "tests/data/payload.txt",
    ],
)
def test_the_check_fails_on_any_hit_in_any_kind_of_file(
    tmp_path: Path, relative: str
) -> None:
    tree = _tree(tmp_path)
    planted = tree / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(f'{{"{RETIRED}": true}}\n', encoding="utf-8")
    assert _run(tree) != 0


def test_the_check_fails_on_a_hit_when_the_contract_of_the_change_declares_the_interface_change(
    tmp_path: Path,
) -> None:
    """The earlier falsifier could fire only while interface_change was false."""
    tree = _tree(tmp_path)
    declared = (tree / "contracts" / "OMN-24871.yaml").read_text(encoding="utf-8")
    assert "interface_change: true" in declared
    (tree / "tests" / "late.py").write_text(f'FLAG = "{RETIRED}"\n', encoding="utf-8")
    assert _run(tree) != 0
