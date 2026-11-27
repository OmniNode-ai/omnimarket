# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help lines this package declares for ``onex --help`` match its commands (OMN-26615).

``onex --help`` reads ``[project.entry-points."onex.cli.help"]`` instead of importing
each command, and prints a stand-in ``click.Command(name, help=<declared text>)``.
So the declared text must print exactly what the real command prints.

Failure modes covered:
- declared line drifts from the command's own help
- entry kept for a hidden, deprecated or explicit-short_help command (metadata cannot
  carry those flags, so ``--help`` would show it wrongly)
- entry for a command that does not exist
- missing entry for a visible command
- truncation differs at some terminal width
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import click
import pytest

pytestmark = pytest.mark.unit

ROOT_DEPTH = 3
LIMITS = range(10, 140)


def _tables() -> tuple[dict[str, str], dict[str, str]]:
    root = Path(__file__).resolve().parents[ROOT_DEPTH]
    with (root / "pyproject.toml").open("rb") as handle:
        entry_points = tomllib.load(handle)["project"]["entry-points"]
    return entry_points.get("onex.cli", {}), entry_points.get("onex.cli.help", {})


CLI, HELP = _tables()


def _command(target: str) -> click.Command:
    module_name, attr = target.split(":", 1)
    command = getattr(importlib.import_module(module_name), attr)
    assert isinstance(command, click.Command), f"{target} is not a click command"
    return command


def _carried_by_metadata(command: click.Command) -> bool:
    """False for a command whose flags or text a plain help line cannot carry."""
    return not (
        command.hidden
        or command.deprecated
        or command.short_help
        or not (command.help or "").strip()
    )


def test_a_help_table_exists_when_there_are_commands() -> None:
    assert CLI
    assert HELP, "onex.cli.help is empty although onex.cli declares commands"


def test_every_help_entry_names_a_command_of_this_package() -> None:
    assert set(HELP) <= set(CLI)


@pytest.mark.parametrize("name", sorted(CLI))
def test_declared_help_prints_exactly_what_the_command_prints(name: str) -> None:
    command = _command(CLI[name])
    if not _carried_by_metadata(command):
        assert name not in HELP, (
            f"{name!r} is hidden, deprecated, has a short_help or has no help text, "
            "so it must not have an onex.cli.help entry"
        )
        return
    assert name in HELP, f"visible command {name!r} has no onex.cli.help entry"
    stand_in = click.Command(name, help=HELP[name])
    for limit in LIMITS:
        assert stand_in.get_short_help_str(limit) == command.get_short_help_str(
            limit
        ), (
            f"{name!r} at limit {limit}: declared {stand_in.get_short_help_str(limit)!r}, "
            f"command {command.get_short_help_str(limit)!r}"
        )
