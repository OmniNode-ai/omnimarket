"""AC2: the CLI the create-ticket skill step names exists (OMN-21723)."""

from __future__ import annotations

import subprocess
import sys


def test_cli_help_names_clear_project_and_repair_project() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_create_ticket", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "clear_project" in result.stdout
    assert "repair_project" in result.stdout
