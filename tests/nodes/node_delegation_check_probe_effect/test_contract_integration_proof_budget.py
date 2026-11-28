# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done gate lab proof of OMN-26642 fits the per-check time budget.

The verifier kills a check at its 30 s ceiling and reads it as not evaluated, so a
lab proof that runs the whole probe test directory never reaches a verdict (the
dispatch alone costs about 25 s). The item names one test file and a ``-k`` that
selects the green reading (the run record names the gateway address and tenant)
and the red one (no gateway row reads precondition-absent, never green). This
test fails on a command that runs the whole directory and on a selector that
matches no test, so the narrowing never empties the falsifier.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT = _ROOT / "contracts" / "OMN-26642.yaml"
_ITEM_ID = "dod-omn26642-integration-proof-dev-lane"
_GREEN = "names_the_gateway_address_and_the_tenant"
_RED = "with_no_gateway_row"


def _pytest_argv(contract: dict[str, Any]) -> list[str]:
    item = next(i for i in contract["dod_evidence"] if i["id"] == _ITEM_ID)
    argv = shlex.split(item["checks"][0]["check_value"])
    return argv[argv.index("--") + 1 :]


def _selected(argv: list[str]) -> tuple[Path, str]:
    paths = [a for a in argv if a.startswith("tests/") and a.endswith(".py")]
    assert len(paths) == 1, f"the proof must name exactly one test file, got {paths}"
    assert "-k" in argv, "the proof must select its tests with -k"
    return _ROOT / paths[0], argv[argv.index("-k") + 1]


def _test_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def test_proof_names_one_file_and_selects_the_green_and_red_readings() -> None:
    path, expr = _selected(_pytest_argv(yaml.safe_load(_CONTRACT.read_text())))
    names = _test_names(path)
    for term in (_GREEN, _RED):
        assert term in expr
        assert any(term in n for n in names)


def test_known_bad_whole_directory_command_is_refused() -> None:
    argv = ["uv", "run", "pytest", "tests/nodes/node_delegation_check_probe_effect"]
    try:
        _selected(argv)
    except AssertionError:
        return
    raise AssertionError("a whole-directory proof must be refused")
