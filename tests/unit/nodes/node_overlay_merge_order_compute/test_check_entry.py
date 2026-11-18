# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check entry (OMN-22809): the overlay home's ci judges a change against the installed models, end to end.

Each test builds a real overlay home on disk (overlays, a pins file) and a real overlay for the node, then runs the
entry the way ci runs it: the changed ids on standard input, the home and the merged ref named by the node's overlay.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_overlay_merge_order_compute import (
    runtime_overlay_merge_order_check as check_entry,
)
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    EXAMPLE_OVERLAY,
    MERGED_REF,
    NODE_DIR,
)

pytestmark = pytest.mark.unit

NODES = NODE_DIR.parent
SOUND = "node_overlay_drift_effect"
SOUND_MODEL = (
    "omnimarket.nodes.node_overlay_drift_effect.overlay:ModelOverlayDriftOverlay"
)
OTHER = "node_reading_writer_effect"
OTHER_MODEL = "omnimarket.nodes.node_reading_writer_effect.overlay:ModelReadingOverlay"
UNPINNED = "node_unpinned_effect"
PLANTED = "rung_order"


def build_home(
    tmp_path: Path, *, plant_in: str | None = None, pin_other: bool = True
) -> dict[str, str]:
    """A home of two real overlays (their packaged examples), the node's own overlay and the environment naming them."""
    home = tmp_path / "home"
    for overlay_id in (SOUND, OTHER):
        text = (NODES / overlay_id / "overlay.example.yaml").read_text(encoding="utf-8")
        if overlay_id == plant_in:
            text += f"{PLANTED}:\n- claude\n- opus\n"
        target = home / "overlays" / overlay_id
        target.mkdir(parents=True)
        (target / "overlay.yaml").write_text(text, encoding="utf-8")
    unpinned = home / "overlays" / UNPINNED
    unpinned.mkdir(parents=True)
    (unpinned / "overlay.yaml").write_text("key: value\n", encoding="utf-8")
    pins = [f"{SOUND} 0123456789abcdef {SOUND_MODEL}"]
    if pin_other:
        pins.append(f"{OTHER} 0123456789abcdef {OTHER_MODEL}")
    (home / ".test-node-pins").write_text("\n".join(pins) + "\n", encoding="utf-8")

    roots = tmp_path / "roots"
    own = roots / "node_overlay_merge_order_compute"
    own.mkdir(parents=True)
    values = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    values["home"] = {
        "root_env": "TEST_OVERLAY_HOME",
        "overlays_subdir": "overlays",
        "overlay_file": "overlay.yaml",
        "pins_file": ".test-node-pins",
    }
    values["merged_ref_env"] = "TEST_MERGED_REF"
    (own / "overlay.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(roots),
        "TEST_OVERLAY_HOME": str(home),
        "TEST_MERGED_REF": MERGED_REF,
    }


def run(environ: dict[str, str], changed: str) -> int:
    return check_entry.main(stdin=io.StringIO(changed), environ=environ)


def test_a_touched_overlay_the_installed_model_accepts_is_order_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = build_home(tmp_path)
    code = run(environ, f"{SOUND}\n")
    assert code == 0
    assert "verdict=ORDER_OK" in capsys.readouterr().out


def test_a_touched_overlay_carrying_a_key_the_model_refuses_is_ahead_of_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failing control: the rung_order case. Judging nothing would read ORDER_OK here."""
    environ = build_home(tmp_path, plant_in=OTHER)
    code = run(environ, f"{OTHER}\n")
    shown = capsys.readouterr().out
    assert code == 1
    assert "verdict=AHEAD_OF_CODE" in shown
    assert f"overlay={OTHER}" in shown
    assert PLANTED in shown
    assert "claude" not in shown
    assert "opus" not in shown


def test_an_untouched_failing_overlay_is_not_counted_against_the_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = build_home(tmp_path, plant_in=OTHER)
    code = run(environ, f"{SOUND}\n")
    assert code == 0
    assert "verdict=ORDER_OK" in capsys.readouterr().out


def test_a_touched_overlay_no_pin_judges_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = build_home(tmp_path)
    code = run(environ, f"{UNPINNED}\n")
    assert code == 2
    assert "changed_overlay_not_judged" in capsys.readouterr().err


def test_a_change_touching_no_overlay_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = build_home(tmp_path)
    code = run(environ, "\n\n")
    assert code == 2
    assert "no_changed_overlays" in capsys.readouterr().err


def test_an_absent_home_is_indeterminate_with_no_default_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = build_home(tmp_path)
    del environ["TEST_OVERLAY_HOME"]
    code = run(environ, f"{SOUND}\n")
    assert code == 3
    assert "TEST_OVERLAY_HOME" in capsys.readouterr().err


def test_a_missing_overlay_for_the_node_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = build_home(tmp_path)
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path / "empty")
    code = run(environ, f"{SOUND}\n")
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err
