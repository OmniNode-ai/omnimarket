# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Setup parse failures retain typed, secret-free JSON before callback dispatch."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    "arguments",
    [
        ["--unknown-private-sentinel", "--json"],
        ["--json", "--workspace"],
        ["--json", "--endpoint-auth", "private-sentinel"],
    ],
)
def test_actual_setup_parser_errors_are_typed_without_input_echo(
    tmp_path: Path, arguments: list[str]
) -> None:
    cli = str(Path(sys.executable).parent / "onex")
    result = subprocess.run(
        [cli, "setup", *arguments],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": str(Path(cli).parent) + ":/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "refused", result.stderr
    assert "private-sentinel" not in result.stdout + result.stderr
    assert "Usage:" not in result.stdout
    assert not list(tmp_path.iterdir())


def test_setup_help_and_human_parser_errors_remain_normal(tmp_path: Path) -> None:
    cli = str(Path(sys.executable).parent / "onex")
    for args, code in [(["--help", "--json"], 0), (["--unknown-option"], 2)]:
        result = subprocess.run(
            [cli, "setup", *args],
            cwd=tmp_path,
            env={
                "HOME": str(tmp_path),
                "PATH": str(Path(cli).parent) + ":/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == code
        assert "Usage:" in result.stdout + result.stderr
        assert not result.stdout.startswith("{")
