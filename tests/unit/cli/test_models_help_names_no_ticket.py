# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a customer reads in ``onex models ... --help`` names no ticket.

A verify pass on 2026-11-26 read every ``onex models`` help in a real terminal.

Failure modes:
  H1  a help names the ticket that added it, such as "(OMN-23413)": the id
      means nothing to a customer and points at a tracker they cannot open.
  H2  a command reached only through the installed ``onex`` (a contract adds
      ``overlay``) is not read, so its help is never checked.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

#: Every ``onex models`` help as the installed CLI builds it, one subprocess.
_READ_HELPS = """
import json
import click
from omnibase_core.cli.cli_commands import cli

root = click.Context(cli, info_name="onex")
group = cli.get_command(root, "models")
ctx = click.Context(group, info_name="models", parent=root)
helps = {"": group.get_help(ctx)}
for name in group.list_commands(ctx):
    command = group.get_command(ctx, name)
    helps[name] = command.get_help(click.Context(command, info_name=name, parent=ctx))
print(json.dumps(helps))
"""

_TICKET = re.compile(r"\bOMN-\d+")


@pytest.fixture(scope="module")
def helps() -> dict[str, str]:
    shown = subprocess.run(
        [sys.executable, "-c", _READ_HELPS],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert shown.returncode == 0, shown.stderr[-2000:]
    return dict(json.loads(shown.stdout.strip().splitlines()[-1]))


def test_every_models_command_is_read(helps: dict[str, str]) -> None:
    """H2."""
    for name in ("add", "list", "overlay", "default-model", "review-gateway"):
        assert name in helps, sorted(helps)


def test_no_models_help_names_a_ticket(helps: dict[str, str]) -> None:
    """H1."""
    named = {name: _TICKET.findall(text) for name, text in helps.items()}
    assert not {name: ids for name, ids in named.items() if ids}
