# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publisher unit carries the MVP launch block's command route, and the publish runs its read (OMN-23205).

Golden chain: the rendered unit holds the command the simple floor's source port runs, under the registry root, and still parses as a
plist; the publish contract declares the ``mvp_launch_reads`` step ahead of the simple floor, in the concurrent input group, with the
node's own flag.
Error chain (the failing control): the command names the same node and flag the step runs, so a unit whose command went missing, or a
step whose flag the node does not accept, fails here and not on the floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_publish_effect import __file__ as publish_file
from omnimarket.nodes.node_floor_simple_effect.runtime_floor_simple import main
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from tests.nodes.node_host_installer_registry_units.test_publisher_ledger_environment import (  # noqa: F401
    brew_python,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    make_checkout,
    plist_of,
    real_run,
    request,
)

pytestmark = pytest.mark.unit


def publisher_env(tmp_path: Path) -> dict[str, str]:
    make_checkout(tmp_path / "omni" / "omnimarket")
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, ledger_host_name="host-under-test")
    )
    assert result.status == "success", result.stderr
    env: dict[str, str] = plist_of(result.rendered)["EnvironmentVariables"]
    return env


def test_golden_the_rendered_unit_holds_the_mvp_launch_command_under_the_registry_root(
    tmp_path: Path,
) -> None:
    root = str(tmp_path / "omni")
    assert publisher_env(tmp_path)["ONEX_MVP_LAUNCH_READ_COMMAND"] == (
        f"{root}/omnimarket/.venv/bin/python -m omnimarket.nodes.node_floor_simple_effect "
        f"--home {root} --mvp-answer"
    )


def test_golden_the_publish_runs_the_read_with_the_input_steps_before_the_simple_floor() -> (
    None
):
    contract = yaml.safe_load(
        (Path(publish_file).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    steps = [s for s in contract["runtime"]["steps"] if isinstance(s, dict)]
    names = [s["name"] for s in steps]
    step = steps[names.index("mvp_launch_reads")]
    assert names.index("mvp_launch_reads") < names.index("floor_simple")
    assert step["concurrent"] is True
    assert step["required"] is False
    assert step["module"] == "omnimarket.nodes.node_floor_simple_effect"
    assert "--mvp-reads" in step["args"]


def test_error_the_node_refuses_a_read_with_no_overlay_as_exit_2_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_MVP_LAUNCH_OVERLAY", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert main(["--home", str(tmp_path), "--mvp-reads"]) == 2
    assert "REFUSED mvp-reads" in capsys.readouterr().err
