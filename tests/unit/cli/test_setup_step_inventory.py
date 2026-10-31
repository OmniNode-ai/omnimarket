# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21311 AC1: every step of omnibase ``install.sh`` has an owner.

The inventory ``setup_step_inventory.yaml`` of node_onex_setup_effect lists every step and every
command-line flag of ``install.sh`` against the command that owns it now, and
marks each one kept (stays in ``install.sh``), moved (now owned by an ``onex``
command) or dropped, with a reason. This test parses ``install.sh`` itself and
fails when a step or flag in it has no entry, or when an entry names a step
that is not there.

The parsed copy is the snapshot under ``tests/fixtures/onex_setup/`` taken from
OmniNode-ai/omnibase at the commit in its file name. Set
``ONEX_SETUP_INSTALL_SH`` to a live ``install.sh`` to check that copy as well.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import click
import pytest
import yaml
from omnibase_core.cli.cli_commands import cli

import omnimarket.nodes.node_onex_setup_effect as setup_node

_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "onex_setup"
    / "omnibase_install_sh_7e5af9e6.txt"
)
_INVENTORY = Path(setup_node.__file__).parent / "setup_step_inventory.yaml"
_RULE = re.compile(r"^# -{10,}$")
_HEADING = re.compile(r"^# (\S.*)$")
_ARM = re.compile(r"^\s+((?:-[-a-z=*]+)(?:\|-[-a-z=*]+)*)\)")
_DISPOSITIONS = {"kept", "moved", "dropped"}


def install_sh_sections(text: str) -> list[str]:
    """Return the heading of every ``# ----`` framed section, in order."""
    lines = text.splitlines()
    found: list[str] = []
    for i in range(len(lines) - 2):
        heading = _HEADING.match(lines[i + 1])
        if _RULE.match(lines[i]) and heading and _RULE.match(lines[i + 2]):
            found.append(heading.group(1).strip())
    return found


def install_sh_flags(text: str) -> list[str]:
    """Return every long option an argument ``case`` arm of ``install.sh`` accepts."""
    flags: list[str] = []
    for line in text.splitlines():
        arm = _ARM.match(line)
        if not arm:
            continue
        for option in arm.group(1).split("|"):
            name = option.split("=")[0]
            if name.startswith("--") and name not in flags:
                flags.append(name)
    return flags


def _sources() -> list[Path]:
    sources = [_FIXTURE]
    live = os.environ.get("ONEX_SETUP_INSTALL_SH")
    if live:
        sources.append(Path(live))
    return sources


def _inventory() -> list[dict[str, str]]:
    data = yaml.safe_load(_INVENTORY.read_text())
    assert isinstance(data, dict), "inventory must be a mapping"
    entries = data["entries"]
    assert isinstance(entries, list)
    return entries


@pytest.mark.unit
class TestSetupStepInventory:
    def test_parser_finds_the_known_sections(self) -> None:
        """Positive control: the parser sees the numbered steps of the snapshot."""
        sections = install_sh_sections(_FIXTURE.read_text())
        assert "1. Check prerequisites" in sections
        assert "7. Write the runtime configuration for the chosen install path" in (
            sections
        )
        assert len(sections) >= 12
        flags = install_sh_flags(_FIXTURE.read_text())
        assert {"--path", "--switch-path", "--clone-only", "--help"} <= set(flags)

    def test_parser_reports_a_missing_entry(self) -> None:
        """Negative control: a new section in install.sh has no entry."""
        planted = (
            _FIXTURE.read_text()
            + "\n# ------------------------------------------------------------------"
            + "\n# 8. A step nobody owns"
            + "\n# ------------------------------------------------------------------\n"
        )
        owned = {e["install_sh"] for e in _inventory()}
        assert "8. A step nobody owns" not in owned
        assert "8. A step nobody owns" in install_sh_sections(planted)

    @pytest.mark.parametrize("source", _sources(), ids=lambda p: p.name)
    def test_every_install_sh_step_has_an_entry(self, source: Path) -> None:
        text = source.read_text()
        owned = {e["install_sh"] for e in _inventory()}
        missing = [
            step
            for step in install_sh_sections(text) + install_sh_flags(text)
            if step not in owned
        ]
        assert not missing, f"install.sh steps with no inventory entry: {missing}"

    def test_every_entry_names_a_real_step(self) -> None:
        text = _FIXTURE.read_text()
        real = set(install_sh_sections(text)) | set(install_sh_flags(text))
        stale = [e["install_sh"] for e in _inventory() if e["install_sh"] not in real]
        assert not stale, f"inventory entries naming no install.sh step: {stale}"

    def test_every_entry_is_marked_with_an_owner_and_a_reason(self) -> None:
        commands = set(cli.list_commands(click.Context(cli)))
        for entry in _inventory():
            step = entry["install_sh"]
            assert entry["disposition"] in _DISPOSITIONS, step
            assert len(str(entry.get("reason", "")).strip()) >= 20, step
            owner = entry["owner"]
            if entry["disposition"] == "moved":
                assert owner.startswith("onex "), step
                assert owner.split()[1] in commands, (step, owner)
            elif entry["disposition"] == "kept":
                assert owner == "install.sh", step
            else:
                assert owner == "none", step
