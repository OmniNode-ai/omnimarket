# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: `onex` opens without loading commands the TUI cannot run.

Operator 2026-11-26, with a picture of a blank terminal after typing onex:
"when type onex and opening onex cli/tui takes more time why?". Measured on
the operator's Mac: 4.3-4.7 s from launch to the first frame, of which the
TUI's command list loaded every onex extension (46 of them; dashboard alone
2.3 s) although the prompt runs only the declared customer commands, and
dashboard is replaced by the TUI's own tab.

Failure modes, each with a test:
  S1 the TUI loads a command it can never run (a replaced one, or one off the
     customer list), which is what made the start slow.
  S2 the list is cut too far: a customer command no longer resolves.
  S3 a customer command's own subcommands are cut with the rest.
"""

from __future__ import annotations

import click

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui, tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner


class _Recording(click.Group):
    """A root that records which commands were loaded, as onex's lazy root would."""

    def __init__(self) -> None:
        super().__init__("onex")
        self.loaded: list[str] = []

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        self.loaded.append(cmd_name)
        return super().get_command(ctx, cmd_name)


def _root() -> _Recording:
    root = _Recording()

    @root.command("doctor")
    def _doctor() -> None:
        """Check the install."""

    @root.group("metering")
    def _metering() -> None:
        """Spend."""

    @_metering.command("report")
    def _report() -> None:
        """Spend report."""

    @root.command("dashboard")
    def _dashboard() -> None:
        """Replaced by the TUI's own tab."""

    @root.command("setup")
    def _setup() -> None:
        """Not a customer command."""

    return root


def test_the_tui_loads_only_the_commands_it_can_run() -> None:
    """S1."""
    design = tui_design()
    assert (
        "dashboard",
    ) in design.customer_commands  # the control: listed, yet replaced
    assert design.replaces(("dashboard",))
    assert ("setup",) not in design.customer_commands
    root = _root()
    OnexTui(root=root, runner=_Runner([]))
    assert "dashboard" not in root.loaded, root.loaded
    assert "setup" not in root.loaded, root.loaded


def test_a_customer_command_still_resolves() -> None:
    """S2."""
    app = OnexTui(root=_root(), runner=_Runner([]))
    entry = app._resolve(["doctor"])
    assert entry is not None
    assert entry.path == ("doctor",)


def test_a_customer_commands_subcommands_are_kept() -> None:
    """S3."""
    app = OnexTui(root=_root(), runner=_Runner([]))
    entry = app._resolve(["metering", "report"])
    assert entry is not None
    assert entry.path == ("metering", "report")
