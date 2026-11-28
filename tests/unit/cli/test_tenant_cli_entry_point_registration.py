# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RATCHET: ``onex tenant`` reaches the CLI by the ``onex.cli`` entry point, never by hand (the pattern of the cloud test).

Three properties: pyproject declares ``tenant`` in the group; the installed distribution advertises it, from exactly one
distribution; and no module hand-wires the group with ``add_command``.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import click
import pytest

pytestmark = pytest.mark.unit

_GROUP = "onex.cli"
_NAME = "tenant"
_TARGET = "omnimarket.nodes.node_tenant_cli_effect.cli:tenant_group"
_DISTRIBUTION = "omnimarket"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_pyproject_declares_the_tenant_command_in_the_onex_cli_group() -> None:
    pyproject = tomllib.loads(
        (_repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["entry-points"][_GROUP][_NAME] == _TARGET


def test_the_declared_target_loads_and_is_a_click_group() -> None:
    module_path, _, attribute = _TARGET.partition(":")
    loaded = getattr(__import__(module_path, fromlist=[attribute]), attribute)
    assert isinstance(loaded, click.Group)
    assert loaded.name == _NAME


def test_the_installed_distribution_advertises_the_command() -> None:
    matching = [ep for ep in entry_points(group=_GROUP) if ep.name == _NAME]
    assert matching, f"no '{_NAME}' entry point in the '{_GROUP}' group"
    assert matching[0].value.replace(" ", "") == _TARGET
    providers = {ep.dist.name for ep in matching if ep.dist is not None}
    assert providers == {_DISTRIBUTION}


_HAND_WIRE_CALL = re.compile(r"add_command\s*\(\s*[^)]*\btenant_group\b", re.DOTALL)


def test_the_command_is_never_added_to_the_cli_by_hand() -> None:
    offenders = [
        str(path.relative_to(_repo_root()))
        for path in (_repo_root() / "src").rglob("*.py")
        if _HAND_WIRE_CALL.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"hand-wired tenant group in {offenders}"


def test_a_hand_wire_call_is_caught_by_the_pattern() -> None:
    """Positive control: the pattern matches an actual hand-wiring call and ignores a mention."""
    assert _HAND_WIRE_CALL.search("cli.add_command(tenant_group, 'tenant')")
    assert _HAND_WIRE_CALL.search("cli.add_command(\n    tenant_group)")
    assert not _HAND_WIRE_CALL.search(
        "never add_command by hand; tenant_group is an entry point"
    )


def test_help_lists_the_commands() -> None:
    from click.testing import CliRunner

    from omnimarket.nodes.node_tenant_cli_effect.cli import tenant_group

    runner = CliRunner()
    top = runner.invoke(tenant_group, ["--help"])
    assert top.exit_code == 0
    for word in ("create", "show", "key"):
        assert word in top.output
    key = runner.invoke(tenant_group, ["key", "--help"])
    assert key.exit_code == 0
    for word in ("mint", "revoke"):
        assert word in key.output
