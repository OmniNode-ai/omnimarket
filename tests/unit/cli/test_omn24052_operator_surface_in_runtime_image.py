# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime image keeps `onex settings` and `onex scm` reachable (OMN-24052).

Measured on the dev image built at 05:27:54Z (omnimarket f209ae51c6): `onex settings` and `onex scm` answered
"No such command" while `onex secret` worked. The omnibase_core in the image declares `secret` CUSTOMER and
`scm` INTERNAL (`settings` has no declaration, so it is INTERNAL too), and lists and resolves INTERNAL groups only
when ONEX_CLI_SHOW_ALL_COMMANDS is on. The image declared no such variable. The customer surface list is unchanged;
this image is the operator runtime on the lab and dev hosts, so it turns the switch on.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SWITCH = "ONEX_CLI_SHOW_ALL_COMMANDS"
_ENV_LINE = re.compile(rf"^ENV\s+{SWITCH}=(\S+)\s*$", re.MULTILINE)


def _image_environment() -> dict[str, str]:
    """The process environment plus the switch exactly as Dockerfile.runtime declares it."""
    env = {k: v for k, v in os.environ.items() if k != SWITCH}
    match = _ENV_LINE.search(
        (REPO_ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    )
    if match:
        env[SWITCH] = match.group(1).strip('"')
    return env


def _onex(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnibase_core.cli.cli_commands", *args],
        env=_image_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_runtime_image_declares_the_operator_switch_on() -> None:
    declared = _image_environment().get(SWITCH)
    assert declared == "1", (
        f"Dockerfile.runtime declares no ENV {SWITCH}=1, so the installed CLI hides settings and scm"
    )


@pytest.mark.parametrize("group", ["settings", "scm"])
def test_the_installed_cli_lists_the_operator_groups(group: str) -> None:
    result = _onex("--help")
    assert result.returncode == 0, result.stderr
    assert re.search(rf"^\s+{group}\s", result.stdout, re.MULTILINE), (
        f"{group} not listed"
    )


def test_the_installed_cli_finds_settings_and_its_verbs() -> None:
    result = _onex("settings", "--help")
    assert result.returncode == 0, result.stdout + result.stderr
    for verb in ("set", "get", "list"):
        assert re.search(rf"^\s+{verb}\s", result.stdout, re.MULTILINE), (
            f"settings {verb} missing"
        )
