# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the overlays repository's ci step and this node's check entry (OMN-23433).

The overlays ci step "No overlay merges ahead of the code that models it" runs ``python -m <MODULE>`` with the changed
overlay ids on standard input and three variables in its environment, and reads the exit code. The overlays repository
holds the mirror of the constants below in ``tests/test_overlay_merge_order_ci.py``; each side spells them in its own
repository, so a rename on either side turns one of the two tests red and no test reads across repositories.

Each test runs the entry as the ci step runs it: a subprocess, the ci step's variable names, the ci overlay layout.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_overlay_merge_order_compute.overlay import (
    ModelOverlayMergeOrderOverlay,
)
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    EXAMPLE_OVERLAY,
)

pytestmark = pytest.mark.unit

MODULE = "omnimarket.nodes.node_overlay_merge_order_compute.runtime_overlay_merge_order_check"
CI_ENV = {
    "home_env": "ONEX_OVERLAY_HOME",
    "merged_ref_env": "ONEX_MERGE_ORDER_MERGED_REF",
    "roots_env": "ONEX_SKILL_OVERLAY_ROOTS",
}
CI_OVERLAY_KEYS = {
    "ahead_verdicts",
    "max_named",
    "injected_fault",
    "home",
    "merged_ref_env",
}
CI_HOME = {
    "root_env": "ONEX_OVERLAY_HOME",
    "overlays_subdir": "overlays",
    "overlay_file": "overlay.yaml",
    "pins_file": ".omnimarket-node-pins",
}
EXIT = {"ORDER_OK": 0, "AHEAD_OF_CODE": 1, "REFUSED": 2, "INDETERMINATE": 3}
DECISION_LINE = re.compile(
    r"^DECISION verdict=(ORDER_OK|AHEAD_OF_CODE) merged_ref=(\S+) changed=(\d+) accepted=(\d+) ahead=(\d+)$"
)
AHEAD_LINE = re.compile(r"^AHEAD overlay=(\S+) verdict=(\S+) fields=(\S+)$")

SRC = Path(__file__).resolve().parents[4] / "src"
SOUND = "node_overlay_drift_effect"
SOUND_MODEL = (
    "omnimarket.nodes.node_overlay_drift_effect.overlay:ModelOverlayDriftOverlay"
)
OTHER = "node_reading_writer_effect"
OTHER_MODEL = "omnimarket.nodes.node_reading_writer_effect.overlay:ModelReadingOverlay"
PLANTED = "rung_order"
MERGED_REF = "0123456789abcdef0123456789abcdef01234567"
NODES = EXAMPLE_OVERLAY.parent.parent


def ci_values() -> dict[str, object]:
    """The node's overlay as the overlays repository holds it: the example's keys with the ci home and variable."""
    values: dict[str, object] = yaml.safe_load(
        EXAMPLE_OVERLAY.read_text(encoding="utf-8")
    )
    values["home"] = dict(CI_HOME)
    values["merged_ref_env"] = CI_ENV["merged_ref_env"]
    return values


def build_ci_home(tmp_path: Path, *, plant_in: str | None) -> dict[str, str]:
    """An overlay home laid out as the overlays repository lays it out, and the environment the ci step sets."""
    home = tmp_path / "home"
    for overlay_id in (SOUND, OTHER):
        text = (NODES / overlay_id / "overlay.example.yaml").read_text(encoding="utf-8")
        if overlay_id == plant_in:
            text += f"{PLANTED}:\n- claude\n- opus\n"
        target = home / CI_HOME["overlays_subdir"] / overlay_id
        target.mkdir(parents=True)
        (target / CI_HOME["overlay_file"]).write_text(text, encoding="utf-8")
    pins = [
        f"{SOUND} 0123456789abcdef {SOUND_MODEL}",
        f"{OTHER} 0123456789abcdef {OTHER_MODEL}",
    ]
    (home / CI_HOME["pins_file"]).write_text("\n".join(pins) + "\n", encoding="utf-8")
    own = tmp_path / "roots" / "node_overlay_merge_order_compute"
    own.mkdir(parents=True)
    (own / "overlay.yaml").write_text(yaml.safe_dump(ci_values()), encoding="utf-8")
    return {
        CI_ENV["roots_env"]: str(tmp_path / "roots"),
        CI_ENV["home_env"]: str(home),
        CI_ENV["merged_ref_env"]: MERGED_REF,
        "PYTHONPATH": str(SRC),
    }


def run_as_ci(environ: dict[str, str], stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", MODULE],
        input=stdin,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_module_the_ci_step_runs_exists_and_is_runnable() -> None:
    spec = importlib.util.find_spec(MODULE)
    assert spec is not None
    module = importlib.import_module(MODULE)
    assert callable(module.main)


def test_the_nodes_example_overlay_has_exactly_the_keys_the_ci_overlay_holds() -> None:
    values = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    assert set(values) == CI_OVERLAY_KEYS


def test_the_ci_overlay_loads_into_the_nodes_model() -> None:
    model = ModelOverlayMergeOrderOverlay.model_validate(ci_values())
    assert set(model.model_dump()) == CI_OVERLAY_KEYS
    assert model.home.root_env == CI_ENV["home_env"]
    assert model.merged_ref_env == CI_ENV["merged_ref_env"]


def test_a_ci_shaped_run_reads_a_planted_key_as_ahead_of_code(tmp_path: Path) -> None:
    """The failing control of the seam: a check that judged nothing would exit 0 here."""
    done = run_as_ci(build_ci_home(tmp_path, plant_in=OTHER), f"{OTHER}\n")
    lines = done.stdout.splitlines()
    assert done.returncode == EXIT["AHEAD_OF_CODE"], done.stderr
    first = DECISION_LINE.match(lines[0])
    assert first is not None
    assert first.group(1) == "AHEAD_OF_CODE"
    assert first.group(2) == MERGED_REF
    assert first.group(5) == "1"
    named = [m for m in map(AHEAD_LINE.match, lines[1:]) if m is not None]
    assert [(m.group(1), PLANTED in m.group(3)) for m in named] == [(OTHER, True)]
    assert "claude" not in done.stdout + done.stderr
    assert "opus" not in done.stdout + done.stderr


def test_a_ci_shaped_run_reads_a_clean_touched_overlay_as_order_ok(
    tmp_path: Path,
) -> None:
    done = run_as_ci(build_ci_home(tmp_path, plant_in=None), f"{SOUND}\n")
    first = DECISION_LINE.match(done.stdout.splitlines()[0])
    assert done.returncode == EXIT["ORDER_OK"], done.stderr
    assert first is not None
    assert first.group(1) == "ORDER_OK"
    assert first.group(5) == "0"


def test_a_ci_shaped_run_without_the_home_variable_is_indeterminate(
    tmp_path: Path,
) -> None:
    environ = build_ci_home(tmp_path, plant_in=None)
    del environ[CI_ENV["home_env"]]
    done = run_as_ci(environ, f"{SOUND}\n")
    assert done.returncode == EXIT["INDETERMINATE"]
    assert CI_ENV["home_env"] in done.stderr


def test_failing_control_a_renamed_decision_field_does_not_match_the_ci_reading() -> (
    None
):
    line = "DECISION verdict=ORDER_OK merged_ref=x changed=1 accepted=1 ahead=0"
    assert DECISION_LINE.match(line) is not None
    assert DECISION_LINE.match(line.replace("ahead=", "behind=")) is None
