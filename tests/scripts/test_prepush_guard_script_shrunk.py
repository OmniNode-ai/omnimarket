# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre-push guard script calls the node and stays below its baselined size (OMN-23367).

The canonical-file-shape ratchet refused the per-login change because the script grew from 293 to 406
code lines. The decision now lives in node_pytest_full_suite_host_guard; the script is the pytest
hook, the grant and the CLI. Code lines are the non-blank lines that are not comments.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "hooks"
    / "pytest_full_suite_host_guard.py"
)
BASELINE_CODE_LINES = 293


def code_lines(path: Path) -> int:
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_guard_script_is_below_its_baselined_code_lines() -> None:
    assert code_lines(SCRIPT) < BASELINE_CODE_LINES


def test_guard_script_calls_the_node_and_holds_no_host_rule() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "node_pytest_full_suite_host_guard" in text
    assert ".handle(" in text
    for moved in ("def parse_login_rows", "def apply_login_rows", "def override_var"):
        assert moved not in text, f"{moved} belongs to the node"
