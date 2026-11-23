# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RATCHET -- ``onex tenant`` must reach the CLI by entry point, never by hand (OMN-26475).

The same three properties ``onex cloud`` is held to (``test_cloud_cli_entry_point_registration_omn16967.py``):

1. **DECLARED** -- ``pyproject.toml`` carries ``tenant`` under ``[project.entry-points."onex.cli"]``.
2. **RESOLVED FROM THE INSTALLED DISTRIBUTION** -- the live ``importlib.metadata`` view of the ``onex.cli`` group
   contains ``tenant``, provided by exactly one distribution, ``omnimarket``.
3. **NOT HAND-WIRED** -- no module in this repo calls ``add_command`` with the tenant group.
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
_TARGET = "omnimarket.handlers.cli_tenant:tenant_group"
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

    assert not offenders, f"these modules hand-wire the tenant command: {offenders}"


def test_the_hand_wire_pattern_catches_a_hand_wired_call() -> None:
    """Positive control: the pattern above is not vacuous."""
    assert _HAND_WIRE_CALL.search("cli.add_command(tenant_group, 'tenant')")
    assert not _HAND_WIRE_CALL.search("# registered by entry point, never add_command")
