# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rebuilt nine-task set is declared in the committed suite: six kept rows on their real fix commits, three tasks recorded as counted exclusions."""

from __future__ import annotations

import pytest

from omnimarket.models.eval_suite_census.committed_suite import load_committed_suite
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_suite_census_compute.handlers.handler_code_eval_suite_census import (
    HandlerCodeEvalSuiteCensus,
)
from tests.nodes.node_code_eval_suite_census_compute.support import overlay

pytestmark = pytest.mark.unit

# (repository, contract id): (merge commit, base commit = the merge's first parent)
KEPT = {
    ("omnibase_infra", "OMN-24854"): (
        "a679b44e2cfe2d6148a201273415ba96401f29ed",
        "eff82e2ec9966db0c3a1a9e3f2998053d4f0d8eb",
    ),
    ("omnibase_infra", "OMN-24899"): (
        "eb8fdbf7e01dc1240bfc65e041cba9fd812060d3",
        "1034acc9b1e2df6c276afea5485d50a9be8e5589",
    ),
    ("omnibase_infra", "OMN-24956"): (
        "711f1ebd2fab832272394c420a28c606b6122a8c",
        "235df4a3ef3698981924e7070afcf819b8a0e406",
    ),
    ("omnimarket", "OMN-26079"): (
        "2b0f4e88f30fb61c4ae77a026f958e72252c7e44",
        "d4954eaed26bf579d72358733a46e9c0d27fc038",
    ),
    ("omnimarket", "OMN-24307"): (
        "ae69d5697728071abc46509da777ffebc69582e3",
        "5bf30a3f5258f3e797bd86d05a5847dca9efcc82",
    ),
    ("omnimarket", "OMN-24859"): (
        "69d026f1c63955bcbb42de2c6711dac3e79b2ec9",
        "3e2cc67654cc0f8d1e29bc356f959a87c36bbeeb",
    ),
}
DROPPED_REASON = "omnimarket:taskset_1540_no_merged_fix_commit"
DROPPED_COUNT = 2
UNNAMEABLE_REASON = "private_repository:path_fails_the_public_repo_hygiene_gate"


def test_the_six_kept_tasks_sit_on_their_fix_commit_and_its_parent() -> None:
    by_key = {(t.repository, t.contract_id): t for t in load_committed_suite().tasks}
    for key, (merge_commit, base_commit) in KEPT.items():
        task = by_key[key]
        assert task.merge_commit == merge_commit
        assert task.base_commit == base_commit
        assert task.control is EnumCodeScoreVerdict.PASS
        assert task.checks
        assert task.check_files
        assert task.held_paths


def test_the_two_dropped_tasks_are_one_counted_exclusion() -> None:
    request = load_committed_suite()
    reasons = {e.reason: e.count for e in request.pre_control_exclusions}
    assert reasons[DROPPED_REASON] == DROPPED_COUNT
    assert reasons[UNNAMEABLE_REASON] == 1
    answer = HandlerCodeEvalSuiteCensus(overlay=overlay()).handle(request)
    assert answer.failure is None
    excluded = {row.reason: row.count for row in answer.excluded}
    assert excluded[f"pre_control:{DROPPED_REASON}"] == DROPPED_COUNT
    candidates = {(c.repository, c.contract_id) for c in answer.candidates}
    assert set(KEPT) <= candidates


def test_the_suite_carries_the_rebuilt_set_not_the_earlier_one() -> None:
    request = load_committed_suite()
    assert request.suite_id.endswith("2026-11-21")
    assert len(request.tasks) == 316
