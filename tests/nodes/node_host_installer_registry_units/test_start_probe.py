# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer's start check runs the unit's declared probe, and the landing mains honour it (OMN-23604).

The start check ran ``<module> --help``. For the landing snapshot, watcher-facts and tick orchestrator mains a help request
ran a real pass (read files, reached the bus, wrote its facts). A unit now declares ``probe_argv`` in the contract and the
installer runs exactly that argv; a unit declaring none keeps the help request. The seam test runs every declared probe
against the real module from a bare shell, so a contract that names a probe its main does not honour is red.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_start_check_environment as sibling,
)
from tests.nodes.node_host_installer_registry_units.test_start_check_environment import (
    ROOTS,
    Proofs,
    tick_request,
)

pytestmark = pytest.mark.unit

# The sibling module's autouse fixture, re-exported by assignment so this module collects it.
brew_python_present = sibling.brew_python_present

LANDING_UNITS = (
    "landing-snapshot",
    "landing-watcher-facts",
    "landing-snapshot-cutover",
    "landing-tick-orchestrator",
)
FAILING_PROBE_UV = (
    '#!/bin/sh\nfor word in "$@"; do last="$word"; done\n'
    'if [ "$last" = "--probe" ]; then echo "probe refused" >&2; exit 4; fi\nexit 0\n'
)


def probe_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "uv"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:" + os.environ["PATH"])


def units_with(**probe: Any) -> dict[str, dict[str, Any]]:
    units = load_units()
    unit = units["landing-tick-orchestrator"]
    unit.pop("probe_argv", None)
    unit.update(probe)
    return units


@pytest.mark.parametrize("name", LANDING_UNITS)
def test_the_landing_units_declare_a_probe(name: str) -> None:
    assert load_units()[name]["probe_argv"] == ["--probe"]


def test_golden_the_start_check_runs_the_declared_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    proofs = Proofs()
    result = HandlerRegistryUnits(proofs).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code == 0, result.stderr
    assert len(proofs.proofs) == 1
    words = proofs.proofs[0]
    assert words[-1] == "--probe"
    assert words[-7:] == [
        "-m",
        "omnimarket.nodes.node_pr_landing_tick_orchestrator",
        "--omni-home",
        str(tmp_path / "omni"),
        "--state-dir",
        words[-2],
        "--probe",
    ]
    assert words[-2].endswith("landing-tick-orchestrator")
    assert "--help" not in words


def test_golden_a_unit_declaring_no_probe_is_checked_with_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    proofs = Proofs()
    result = HandlerRegistryUnits(proofs, units=units_with()).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code == 0, result.stderr
    words = proofs.proofs[0]
    assert words[-1] == "--help"
    assert words[-5] == "--omni-home"
    assert words[-4] == str(tmp_path / "omni")
    assert words[-3] == "--state-dir"


def test_golden_a_declared_probe_may_carry_several_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    proofs = Proofs()
    result = HandlerRegistryUnits(
        proofs, units=units_with(probe_argv=["--probe", "--quiet"])
    ).handle(tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")}))
    assert result.exit_code == 0, result.stderr
    words = proofs.proofs[0]
    assert words[-2:] == ["--probe", "--quiet"]
    assert words[-6] == "--omni-home"
    assert words[-5] == str(tmp_path / "omni")
    assert words[-4] == "--state-dir"


def test_error_a_failing_probe_is_refused_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, FAILING_PROBE_UV)
    result = HandlerRegistryUnits(Proofs()).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code != 0
    assert "does not start from" in result.stderr
    assert "start check: --omni-home" in result.stderr
    assert "--probe" in result.stderr
    assert "probe refused" in result.stderr


@pytest.mark.parametrize("declared", ["--probe", [], [""], ["--probe", 3], None])
def test_error_a_malformed_probe_declaration_is_refused_before_any_check(
    declared: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_uv(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    proofs = Proofs()
    result = HandlerRegistryUnits(proofs, units=units_with(probe_argv=declared)).handle(
        tick_request(tmp_path, path_overrides={ROOTS: str(tmp_path / "clone")})
    )
    assert result.exit_code != 0
    assert "probe_argv" in result.stderr
    assert proofs.proofs == []


@pytest.mark.parametrize(
    "module",
    sorted({load_units()[name]["module"] for name in LANDING_UNITS}),
)
def test_seam_every_declared_probe_is_honoured_by_its_main_from_a_bare_shell(
    module: str, tmp_path: Path
) -> None:
    """The contract's probe and the main's flag are two sides of one seam: run each declared argv for real."""
    declared = {
        tuple(load_units()[name]["probe_argv"])
        for name in LANDING_UNITS
        if load_units()[name]["module"] == module
    }
    assert len(declared) == 1
    (argv,) = declared
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            "--omni-home",
            str(tmp_path / "root"),
            "--state-dir",
            str(tmp_path / "state"),
            *argv,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == '{"status": "probe_ok"}'
    assert list(tmp_path.rglob("*")) == []
