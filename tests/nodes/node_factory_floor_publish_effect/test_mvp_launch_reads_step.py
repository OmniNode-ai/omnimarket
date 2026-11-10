# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish runs the MVP launch block's scheduled read (OMN-23205).

Golden chain: the publish contract declares the ``mvp_launch_reads`` step ahead of the simple floor, in the concurrent input group, with the
node's own flag.
Error chain (the failing control): the node refuses the step's flag with no overlay as exit 2 and names it, so a step whose flag the node
does not accept, or a read with nothing to read, fails here and not on the floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_publish_effect import __file__ as publish_file
from omnimarket.nodes.node_floor_simple_effect.runtime_floor_simple import main

pytestmark = pytest.mark.unit


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
