# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both pages retain dead-worker evidence without drawing occupied worker slots."""

from __future__ import annotations

import copy
import shutil

import pytest

from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    SHELL_FILES,
    SHELL_RECORD,
    _run_floor,
    _run_shell,
    _shell_out,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("page", ["dashboard", "floor"])
def test_dead_workers_are_named_separately_from_live_quiet_workers(page: str) -> None:
    assert shutil.which("node"), "the page test requires the JavaScript runtime"
    record = copy.deepcopy(SHELL_RECORD)
    record["floor"]["inactive_lanes"] = [
        {
            "lane": "landing-L25183",
            "liveness": "dead",
            "liveness_detail": "runner process exited",
        },
        {
            "lane": "cx-5614-0410",
            "liveness": "finished",
            "liveness_detail": "runner receipt: landing-failed",
        },
    ]
    if page == "dashboard":
        drawn = _shell_out(_run_shell({**SHELL_FILES, "factory-floor.json": record}))[
            "dash"
        ]
    else:
        drawn = _run_floor({"factory-floor.json": record})["bayGrid"]
    assert "Workers no longer occupying slots" in drawn
    assert "landing-L25183" in drawn
    assert "dead" in drawn
    assert "cx-5614-0410" in drawn
    assert "finished" in drawn
    # The existing unplaced worker is still quiet; inactive rows did not replace it.
    assert "lane-y" in drawn


@pytest.mark.parametrize("page", ["dashboard", "floor"])
def test_inactive_worker_evidence_is_escaped(page: str) -> None:
    assert shutil.which("node"), "the page test requires the JavaScript runtime"
    record = copy.deepcopy(SHELL_RECORD)
    record["floor"]["inactive_lanes"] = [
        {
            "lane": "<img src=x>",
            "liveness": "dead",
            "liveness_detail": "<script>bad</script>",
        },
    ]
    if page == "dashboard":
        drawn = _shell_out(_run_shell({**SHELL_FILES, "factory-floor.json": record}))[
            "dash"
        ]
    else:
        drawn = _run_floor({"factory-floor.json": record})["bayGrid"]
    assert "<img src=x>" not in drawn
    assert "<script>bad</script>" not in drawn
    assert "&lt;img src=x&gt;" in drawn
