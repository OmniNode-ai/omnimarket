# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each nightly full-suite shard fits its job timeout: a shard that runs out of time is cancelled, so the night is red.

Every ``Nightly Full Suite`` run from 2026-11-10 to 2026-11-23 concluded cancelled. In the run of 2026-11-23
(45100973978) nine of the ten shards were cancelled in their pytest step 30 minutes after the job started, which is
the shard's ``timeout-minutes``, and the one shard that finished took 21 minutes. The recorded suite is 11,429 test
seconds, 1,143 per shard at ten shards before the coverage collection and the setup the shard also pays; at the
PR full-suite split of twenty it is 571. This test holds the share one shard carries, with that headroom, inside
the job timeout, so the shard count follows the recorded suite and is not typed from memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly-full-suite.yml"
DURATIONS = REPO_ROOT / "config" / "test_file_durations.json"

#: The shard pays for coverage collection and its setup on top of the recorded test seconds: run 45100973978
#: spent more than 30 minutes on a recorded share of 19 minutes, so a shard must carry at most this share of its
#: timeout in recorded seconds.
MAX_RECORDED_SHARE_OF_TIMEOUT = 0.5


def _workflow() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    return loaded


def _recorded_suite_seconds() -> float:
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    return float(sum(v for v in durations.values() if isinstance(v, (int, float))))


def test_nightly_shard_recorded_share_fits_its_job_timeout() -> None:
    workflow = _workflow()
    split_count = int(workflow["env"]["NIGHTLY_SPLIT_COUNT"])
    timeout_seconds = workflow["jobs"]["shard"]["timeout-minutes"] * 60
    per_shard = _recorded_suite_seconds() / split_count
    assert per_shard <= MAX_RECORDED_SHARE_OF_TIMEOUT * timeout_seconds, (
        f"{per_shard:.0f} recorded seconds per shard at {split_count} shards, "
        f"over {MAX_RECORDED_SHARE_OF_TIMEOUT:.0%} of the {timeout_seconds} s job timeout"
    )


def test_nightly_shard_count_is_at_least_the_pull_request_full_suite_split() -> None:
    workflow = _workflow()
    assert int(workflow["env"]["NIGHTLY_SPLIT_COUNT"]) >= load_policy().full_split_count


def test_nightly_shard_job_keeps_its_timeout_and_the_matrix_stays_in_step() -> None:
    """The fix adds shards; it never lengthens the timeout or drops a shard from the matrix."""
    workflow = _workflow()
    shard = workflow["jobs"]["shard"]
    assert shard["timeout-minutes"] == 30
    split_count = int(workflow["env"]["NIGHTLY_SPLIT_COUNT"])
    assert shard["strategy"]["matrix"]["split"] == list(range(1, split_count + 1))
