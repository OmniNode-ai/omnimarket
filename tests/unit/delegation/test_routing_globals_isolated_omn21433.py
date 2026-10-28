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


# OMN-21469: the same class of leak from the customer-never-lab file. It routes
# through ``delta``, which binds ``_config`` (the tier ladder with the placements of
# the contract that file wrote), the spread groups derived from it and the
# task-class contract cache, and its fixture cleared only the endpoint cache. The
# tier escalation tests then read ``cheap_cloud`` where they expect
# ``cheap_frontier`` when the shard planner deals the two files to one process in
# that order (3 failed, 63 passed on omnimarket#4524). ``unit/delegation/conftest.py``
# resets ``_config`` before each test there (OMN-21434), which hides the leak from
# that directory; the state is still left bound for every other file, so the first
# test below reads the globals the file leaves instead of waiting for a victim.
POLLUTER_OMN21469 = "tests/test_omn17082_customer_never_lab_backend.py"
VICTIM_OMN21469 = "tests/unit/delegation/test_delegation_tier_escalation.py"

_LEAK_PROBE = """
import sys
import pytest
code = pytest.main(sys.argv[1:] + ["-q", "-p", "no:cacheprovider", "--no-cov"])
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
leaked = {
    "_config": routing._config is not None,
    "_config_spread_peers": routing._config_spread_peers is not None,
    "_config_spread_weights": routing._config_spread_weights is not None,
    "_get_task_class_contract": routing._get_task_class_contract.cache_info().currsize,
    "_load_bifrost_endpoints": routing._load_bifrost_endpoints.cache_info().currsize,
}
print("LEAKED", {k: v for k, v in leaked.items() if v})
sys.exit(int(code) or (3 if any(leaked.values()) else 0))
"""


def _run(*files: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *files,
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--no-cov",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=480,
        check=False,
    )


def test_the_customer_never_lab_file_leaves_no_routing_global_set() -> None:
    """After the file's last test no routing global is bound and no cache is filled."""
    result = subprocess.run(
        [sys.executable, "-c", _LEAK_PROBE, POLLUTER_OMN21469],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=480,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]


def test_the_customer_never_lab_file_does_not_change_the_tier_escalation_tests_after_it() -> (
    None
):
    """Run the two files in the order that failed; the second must still pass."""
    result = _run(POLLUTER_OMN21469, VICTIM_OMN21469)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]


def test_the_tier_escalation_tests_pass_before_the_customer_never_lab_file() -> None:
    """The reverse order passed on dev and must keep passing."""
    result = _run(VICTIM_OMN21469, POLLUTER_OMN21469)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
