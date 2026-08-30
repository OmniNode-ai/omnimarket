# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RATCHET — ``onex cloud`` must reach the CLI by entry point, never by hand (OMN-16967).

**The drift class this exists to stop.** The operator's diagnosis, 2026-08-29:
features get built outside entry-point registration *because nothing enforces
the binding*. The ruling is that the delegate surface is a market-package
surface which auto-registers into the ``onex`` CLI when the package is
installed — so the binding must be mechanical, not conventional.

Three properties, each of which a future hand-wiring would break:

1. **DECLARED** — ``pyproject.toml`` carries ``cloud`` under
   ``[project.entry-points."onex.cli"]``, pointing at this repo's group.
2. **RESOLVED FROM THE INSTALLED DISTRIBUTION** — the live
   ``importlib.metadata`` view of the ``onex.cli`` group contains ``cloud``,
   and the distribution providing it is ``omnimarket``. This is the
   "present when the package is installed" half; it is also the "absent when
   not" half by construction, since an entry point cannot be advertised by a
   distribution that is not installed.
3. **NOT HAND-WIRED** — no module in this repo calls ``add_command`` with the
   cloud group. A hand-wired copy would keep the command working while
   silently severing it from the install-time contract, which is exactly the
   failure that would pass CI unnoticed without this test.

It also asserts the name does not collide with an entry point another
distribution already claims. ``omnibase_core``'s loader resolves the
``onex.cli`` group in iteration order, so two distributions advertising the
same name is a nondeterministic shadow, not an override.
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
_NAME = "cloud"
_TARGET = "omnimarket.cli.cli_cloud:cloud_group"
_DISTRIBUTION = "omnimarket"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_pyproject_declares_the_cloud_command_in_the_onex_cli_group() -> None:
    pyproject = tomllib.loads(
        (_repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = pyproject["project"]["entry-points"][_GROUP]

    assert declared[_NAME] == _TARGET, (
        f"{_GROUP}.{_NAME} must point at {_TARGET}. The onex CLI discovers this "
        "command only through this declaration."
    )


def test_the_declared_target_loads_and_is_a_click_group() -> None:
    """A malformed target is skipped with a warning by older loaders — assert it here.

    ``omnibase_core``'s extension loader only adds objects that are a
    ``click.Command``/``click.Group``. A target that imports but is the wrong
    type would leave ``onex cloud`` silently missing.
    """
    module_path, _, attribute = _TARGET.partition(":")
    module = __import__(module_path, fromlist=[attribute])
    loaded = getattr(module, attribute)

    assert isinstance(loaded, click.Group)
    assert loaded.name == _NAME


def test_the_installed_distribution_advertises_the_command() -> None:
    """The install-time contract, read from live metadata rather than source."""
    matching = [ep for ep in entry_points(group=_GROUP) if ep.name == _NAME]

    assert matching, (
        f"no '{_NAME}' entry point in the '{_GROUP}' group. Either the package "
        "is not installed, or the declaration was removed — in both cases "
        "'onex cloud' does not exist for a customer."
    )
    assert matching[0].value.replace(" ", "") == _TARGET

    providers = {ep.dist.name for ep in matching if ep.dist is not None}
    assert providers == {_DISTRIBUTION}, (
        f"'{_NAME}' must be advertised by exactly one distribution "
        f"({_DISTRIBUTION}); found {sorted(providers)}. The CLI loader resolves "
        "this group in iteration order, so a duplicate name is a "
        "nondeterministic shadow, not an override."
    )


# An actual hand-wiring CALL: ``add_command(cloud_group)``,
# ``cli.add_command(cloud_group, "cloud")``, or the same split over lines.
# Matching the two bare substrings instead would flag any module that merely
# *discusses* the prohibition — which is precisely what this module's own
# docstring, and cli_cloud.py's, do.
_HAND_WIRE_CALL = re.compile(r"add_command\s*\(\s*[^)]*\bcloud_group\b", re.DOTALL)


def test_the_command_is_never_added_to_the_cli_by_hand() -> None:
    """No ``add_command(cloud_group)`` anywhere — registration is the entry point."""
    offenders: list[str] = []
    for path in (_repo_root() / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _HAND_WIRE_CALL.search(text):
            offenders.append(str(path.relative_to(_repo_root())))

    assert not offenders, (
        "these modules hand-wire the cloud command into a CLI: "
        f"{offenders}. It must be reached through the "
        f"'{_GROUP}' entry point only, so that installing the market package "
        "is what makes the command appear."
    )
