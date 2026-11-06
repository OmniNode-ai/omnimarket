# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged placement node runs without private overlays in local-only mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_packaged_runtime_empty_pool_needs_no_private_overlay(tmp_path: Path) -> None:
    root = tmp_path / "empty-overlays"
    root.mkdir()
    environ = {**os.environ, "ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"environ": {"LANDING_POOL_HOSTS": "[]"}}))
    done = subprocess.run(
        [
            str(Path(sys.executable).with_name("onex")),
            "node",
            "node_landing_placement_effect",
            "--input",
            str(request),
            "--backend",
            "event_bus=inmemory",
            "--state-root",
            str(tmp_path / "state"),
            "--output",
            "receipt",
            "--timeout",
            "30",
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    receipt = json.loads(done.stdout)
    assert receipt["status"] == "success"
    assert receipt["result_model"] == (
        "omnimarket.nodes.node_landing_placement_effect.models."
        "model_landing_placement.ModelLandingPlacementResult"
    )
    assert receipt["result"] == {
        "exit_code": 0,
        "message": "read 0 host(s) from LANDING_POOL_HOSTS",
        "pool_source": "LANDING_POOL_HOSTS",
        "hosts": [],
    }
