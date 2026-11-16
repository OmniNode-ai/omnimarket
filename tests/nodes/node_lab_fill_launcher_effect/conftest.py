# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the lab-fill launcher tests: a synthetic host (home, state dir, alert library) and the overlay it reads."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import pytest

FIRE_ID = "20261113T060000Z-lab-fill"


@dataclass
class Host:
    home: Path
    state: Path
    run_log: Path
    alert_lib: Path
    alert_sent: Path
    overlay_root: Path

    def env(self, alert_rc: int = 0) -> dict[str, str]:
        self.alert_lib.write_text(
            f'alert_channel_send() {{ printf "%s\\n" "$*" >> "{self.alert_sent}"; return {alert_rc}; }}\n',
            encoding="utf-8",
        )
        return {
            "OMNI_HOME": str(self.home),
            "ONEX_SKILL_OVERLAY_ROOTS": str(self.overlay_root),
            "ONEX_ALERT_CHANNEL_LIB": str(self.alert_lib),
        }

    def write_result(self, **overrides: object) -> None:
        value: dict[str, object] = {
            "version": 1,
            "workflow": "lab-fill",
            "fire_id": FIRE_ID,
            "observed_at": "2026-11-13T06:00:00Z",
            "free_slots": 3,
            "dispatched": 2,
            "idle_alarm": False,
            "friction_recorded": False,
            "alert_text": "",
            "session_line": "lab h202:load=0.1/running=2/free=1 approved-work=0",
        }
        value.update(overrides)
        (self.state / "results").mkdir(exist_ok=True)
        (self.state / "results" / f"{FIRE_ID}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    home = tmp_path / "home"
    state = home / ".onex_state" / "morning-workflows"
    state.mkdir(parents=True)
    run_log = state / "run.log"
    run_log.write_text("", encoding="utf-8")
    overlay_root = tmp_path / "overlays"
    (overlay_root / "node_lab_fill_launcher_effect").mkdir(parents=True)
    example = files("omnimarket.nodes.node_lab_fill_launcher_effect").joinpath(
        "overlay.example.yaml"
    )
    shutil.copyfile(
        str(example), overlay_root / "node_lab_fill_launcher_effect" / "overlay.yaml"
    )
    return Host(
        home,
        state,
        run_log,
        tmp_path / "alert-channel.sh",
        tmp_path / "alert.txt",
        overlay_root,
    )
