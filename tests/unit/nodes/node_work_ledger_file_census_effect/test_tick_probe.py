# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census tick answers the installer's start check.

The host installer runs ``<module> --omni-home <root> --state-dir <dir> --probe`` before it loads the unit (the unit's
tick arguments followed by its declared ``probe_argv``). The tick honours ``--probe`` only as the sole argument, so the
start check ended in an argparse error and ``onex-ledger-file-census`` was never loaded on any host.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units import handlers as _handlers
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect import (
    runtime_file_census_tick as tick,
)

pytestmark = pytest.mark.unit

_UNIT = "ledger-file-census-systemd"


def _declared_unit() -> dict[str, object]:
    contract = Path(_handlers.__file__).parent.parent / "contract.yaml"
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    unit: dict[str, object] = document["units"][_UNIT]
    return unit


def _installer_start_check_argv(tmp_path: Path) -> list[str]:
    unit = _declared_unit()
    request = type(
        "Request",
        (),
        {"registry_root": str(tmp_path), "home": str(tmp_path / "home")},
    )()
    return [
        *HandlerRegistryUnits.tick_arguments(HandlerRegistryUnits(), request, unit),
        *HandlerRegistryUnits.probe_argv(unit),
    ]


def test_start_check_argv_the_installer_runs_is_answered(tmp_path: Path) -> None:
    argv = _installer_start_check_argv(tmp_path)
    assert argv[-1] == "--probe"
    assert "--omni-home" in argv
    out = io.StringIO()
    code = tick.main(argv, environ={}, stdout=out)
    assert code == 0
    assert json.loads(out.getvalue()) == {"status": tick.PROBE_STATUS}


def test_probe_with_the_tick_arguments_writes_no_evidence(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(state_dir), "--probe"],
        environ={},
        stdout=io.StringIO(),
    )
    assert code == 0
    assert not (state_dir / tick.EVIDENCE_FILE).exists()


def test_lone_probe_is_still_answered() -> None:
    out = io.StringIO()
    assert tick.main(["--probe"], environ={}, stdout=out) == 0
    assert json.loads(out.getvalue()) == {"status": tick.PROBE_STATUS}


def test_a_pass_without_its_arguments_is_still_refused() -> None:
    with pytest.raises(SystemExit) as refused:
        tick.main([], environ={}, stdout=io.StringIO())
    assert refused.value.code == 2
