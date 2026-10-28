# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21433: a delegation routing test leaves no routing global behind.

``test_delegation_refusal_reads_overlay`` routes through ``HandlerRoutingIntent``,
which binds the handler's process-global ``_config`` (the tier ladder) from the
contract and overlay the test wrote. It cleared the bifrost endpoint cache on
teardown and nothing else, so the next test file in the same pytest process
routed on that ladder. ``test_cloud_escalation_omn13140`` then read
``cheap_cloud`` where it expected ``cheap_frontier``, and only when the CI shard
planner dealt the two files to one shard in that order (Tests (Split 18/20) on
omnimarket#4520 and #4521).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
POLLUTER = "tests/test_delegation_refusal_reads_overlay.py"
VICTIM = "tests/unit/delegation/test_cloud_escalation_omn13140.py"


def test_the_overlay_refusal_file_does_not_change_the_escalation_tier_after_it() -> (
    None
):
    """Run the two files in the order that failed; the second must still pass."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            POLLUTER,
            VICTIM,
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-cov",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
