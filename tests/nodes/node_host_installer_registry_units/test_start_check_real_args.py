# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer's start check runs the unit with its REAL rendered arguments plus its declared probe (OMN-23662).

The start check once ran ``<module> <probe_argv>`` only, while the rendered unit runs
``<module> --omni-home <TICK_OMNI_HOME> --state-dir <EVIDENCE_DIR>``. That gap hid that three landing mains rejected the
template arguments (exit 2 on every tick) while the start check, which passed only ``--probe``, stayed green. The check
now passes the tick arguments the rendered unit carries, then the declared probe; a main that rejects the template
arguments is refused at install, and a unit declaring no probe keeps the help request after the tick arguments.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from tests.nodes.node_host_installer_registry_units import (
    test_start_check_environment as sibling,
)
from tests.nodes.node_host_installer_registry_units.test_start_check_environment import (
    ROOTS,
    Proofs,
    tick_request,
)
from tests.nodes.node_host_installer_registry_units.test_start_probe import (
    probe_uv,
    units_with,
)

pytestmark = pytest.mark.unit

# The sibling module's autouse fixture, re-exported by assignment so this module collects it.
brew_python_present = sibling.brew_python_present

# A main that rejects the template arguments: it exits 2 with "unrecognized arguments" when its arguments carry
# --omni-home, so a start check that passes the rendered unit's arguments is refused.
REJECTING_UV = (
    '#!/bin/sh\nfor word in "$@"; do case "$word" in --omni-home) echo "unrecognized arguments" >&2; exit 2;; esac; done\n'
    "exit 0\n"
)


def plist_of(text: str) -> dict[str, Any]:
    return plistlib.loads(text.encode("utf-8"))


def words_between(argv: list[str], module: str) -> list[str]:
    """The words after the module name, up to the declared probe (the last word)."""
    index = argv.index("-m") + 2
    return argv[index:]


def test_golden_the_start_check_carries_the_rendered_units_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam between the render and the start check: the words the check passes after the module equal the rendered unit's."""
    probe_uv(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    proofs = Proofs()
    result = HandlerRegistryUnits(proofs).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code == 0, result.stderr
    assert len(proofs.proofs) == 1
    argv = proofs.proofs[0]
    module = "omnimarket.nodes.node_pr_landing_tick_orchestrator"
    assert argv[argv.index("-m") + 1] == module
    check_args = words_between(argv, module)
    assert check_args[-1] == "--probe"
    check_args = check_args[:-1]
    rendered = plist_of(result.rendered)["ProgramArguments"]
    rendered_args = words_between(rendered, module)
    assert check_args == rendered_args
    assert check_args[0] == "--omni-home"
    assert check_args[1] == str(tmp_path / "omni")


def test_failing_control_a_main_that_rejects_the_template_arguments_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, REJECTING_UV)
    result = HandlerRegistryUnits(Proofs()).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code != 0
    assert "does not start from" in result.stderr
    assert "unrecognized arguments" in result.stderr
    assert "--omni-home" in result.stderr


def test_control_a_passing_main_installs_and_a_unit_without_a_probe_ends_with_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    proofs = Proofs()
    result = HandlerRegistryUnits(proofs).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code == 0, result.stderr
    words = proofs.proofs[0]
    module = "omnimarket.nodes.node_pr_landing_tick_orchestrator"
    assert words[words.index("-m") + 1] == module
    assert words[-1] == "--probe"
    assert words[-5] == "--omni-home"
    assert words[-4] == str(tmp_path / "omni")

    proofs = Proofs()
    result = HandlerRegistryUnits(proofs, units=units_with()).handle(
        tick_request(
            tmp_path / "second", path_overrides={ROOTS: str(tmp_path / "clone2")}
        )
    )
    assert result.exit_code == 0, result.stderr
    assert proofs.proofs[0][-1] == "--help"
    assert proofs.proofs[0][-5] == "--omni-home"
    assert proofs.proofs[0][-4] == str(tmp_path / "second" / "omni")
