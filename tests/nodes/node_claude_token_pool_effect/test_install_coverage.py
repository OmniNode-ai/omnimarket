# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared install covers every host the pool overlay lists (OMN-23887).

Measured 2026-11-11: the rotation overlay listed five hosts, yet tick, schedule install and assign worked on one.
The install is declared by the overlay's host rows (scheduler, venv, login file), so a dry-run install must render a
unit that runs ``tick --host <name>`` for each row, pin the overlay root the unit reads its overlay through (the root
a host without it answers NodeOverlayNotFoundError for), and a host the overlay does not list is refused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_claude_token_pool_effect.handlers.schedule import (
    CONTRACT_PATH,
    OVERLAY_ROOTS_ENV,
    RotationScheduler,
    load_schedule,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

NODE = CONTRACT_PATH.parent


def _overlay() -> ModelRotationOverlay:
    return ModelRotationOverlay.model_validate(
        yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def _nothing_runs(command):  # type: ignore[no-untyped-def]
    return subprocess.CompletedProcess(list(command), 0, "", "")


def _scheduler(tmp: Path, overlay: ModelRotationOverlay) -> RotationScheduler:
    return RotationScheduler(
        schedule=load_schedule(),
        overlay=overlay,
        home=tmp / "home",
        run=_nothing_runs,
        uid=501,
    )


def test_every_listed_host_renders_a_tick_unit_that_pins_the_overlay_root(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    sched = _scheduler(tmp_path, overlay)
    assert len(overlay.hosts) >= 2
    for row in overlay.hosts:
        done = sched.install(
            row.name,
            omni_root=tmp_path / "omni",
            overlay_root=tmp_path / "overlays",
            dry_run=True,
        )
        assert done.exit_code == 0, (row.name, done.stderr)
        assert row.name in done.stdout
        assert str(tmp_path / "overlays") in done.stdout, row.name
        if row.scheduler == "systemd-user":
            assert f"tick --host {row.name}" in done.stdout
            assert f"{OVERLAY_ROOTS_ENV}=" in done.stdout
        else:
            assert "<string>tick</string>" in done.stdout


def test_failing_control_a_host_the_overlay_does_not_list_is_refused(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    sched = _scheduler(tmp_path, overlay)
    unlisted = "h999-not-listed"
    assert overlay.host(unlisted) is None
    done = sched.install(
        unlisted,
        omni_root=tmp_path / "omni",
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert done.exit_code == 1
    assert "not in the" in done.stderr
