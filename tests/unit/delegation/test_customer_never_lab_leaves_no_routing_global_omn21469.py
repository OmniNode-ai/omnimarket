# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21469: the customer-never-lab routing tests leave no routing global behind.

``test_omn17082_customer_never_lab_backend`` routes through ``delta``, which binds
the handler's process-global ``_config`` (the tier ladder) from the contract the
test wrote, and caches the task-class contract. Its fixture cleared only the
bifrost endpoint cache, so ``test_delegation_tier_escalation`` run after it in
the same pytest process read a ``cheap_cloud`` ladder where it expected
``cheap_frontier`` (3 failed, 63 passed). The reverse order passed.

``unit/delegation/conftest.py`` now resets ``_config`` before each test there
(OMN-21434), which hides the leak from that directory; the state itself is still
left behind for every other file in the process, so the first test below reads the
routing globals the file leaves instead of waiting for a victim.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
POLLUTER = "tests/test_omn17082_customer_never_lab_backend.py"
VICTIM = "tests/unit/delegation/test_delegation_tier_escalation.py"


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
        [sys.executable, "-c", _LEAK_PROBE, POLLUTER],
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
    result = _run(POLLUTER, VICTIM)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]


def test_the_tier_escalation_tests_pass_before_the_customer_never_lab_file() -> None:
    """The reverse order passed on dev and must keep passing."""
    result = _run(VICTIM, POLLUTER)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
