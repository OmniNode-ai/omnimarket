# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two proof scripts run the core CLI as a module, not the onex launcher (OMN-25070 AC3).

omnibase-core stops declaring the onex console script once the customer package omninode-onex carries it, so a script
that runs `uv run onex` would find no command. scripts/run_four_node_proof.sh and scripts/run_l1_proof.sh run
`uv run python -m omnibase_core.cli.cli_commands node ...` with the same arguments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = (
    REPO / "scripts" / "run_four_node_proof.sh",
    REPO / "scripts" / "run_l1_proof.sh",
)
_LAUNCHER = re.compile(r"(^|[\s(\"'=])onex(\s|$)")


def _code_lines(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return [line for line in lines if not line.lstrip().startswith("#")]


@pytest.mark.parametrize("script", SCRIPTS, ids=[p.name for p in SCRIPTS])
def test_the_proof_scripts_run_no_onex_launcher(script: Path) -> None:
    offenders = [
        line for line in _code_lines(script.read_text()) if _LAUNCHER.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("script", SCRIPTS, ids=[p.name for p in SCRIPTS])
def test_the_proof_scripts_run_nodes_through_the_core_module(script: Path) -> None:
    assert "uv run python -m omnibase_core.cli.cli_commands node " in script.read_text()


def test_the_launcher_pattern_matches_a_launcher_line() -> None:
    assert _LAUNCHER.search("uv run onex node node_ledger_state_reducer \\") is not None
    assert _LAUNCHER.search("packages/omninode-onex") is None
