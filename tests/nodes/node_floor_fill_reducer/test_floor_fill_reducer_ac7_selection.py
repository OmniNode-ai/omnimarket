# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The AC7 evidence command selects every test its description names (OMN-22852).

The criterion says the node's contract loads, the handler reads no key from a clock or
a file, and the parity tests run on the in-memory bus. The command carried
``-m floor_fill_parity -k reducer``, and only the parity file holds that marker, so the
contract and purity tests it describes were deselected and the check could pass without
them.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "contracts" / "OMN-22852.yaml"
NAMED_BY_AC7 = (
    "test_the_contract_loads_and_names_the_node",
    "test_the_node_reads_no_clock_and_does_no_io",
    "test_floor_fill_reducer_parity.py",
)


def _ac7_command() -> list[str]:
    contract = yaml.safe_load(CONTRACT.read_text())
    item = next(i for i in contract["dod_evidence"] if i["id"] == "dod-omn22852-ac7")
    argv = shlex.split(item["checks"][0]["check_value"])
    # -q is dropped: two -q flags make --collect-only print file counts, not test ids.
    return [a for a in argv[argv.index("pytest") + 1 :] if a != "-q"]


def test_ac7_command_collects_every_test_its_description_names() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *_ac7_command(),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = result.stdout
    missing = [name for name in NAMED_BY_AC7 if name not in collected]
    assert not missing, f"AC7 command deselects {missing}"
