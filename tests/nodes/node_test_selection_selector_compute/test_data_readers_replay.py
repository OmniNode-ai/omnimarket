# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The data-file readers rule replayed over the last 60 merges of the committed baseline receipt.

The replay input and tree are the frozen ones the baseline receipt was made from. Each test replays the six
merges that changed a fixture, once with the readers rule off (the shipped setting) and once with it on, over
the same tree, so a decision that moves is the rule's doing and nothing else's.

The frozen tree holds the import graph, the spelled node names and the directory layout of each Python file,
not the text of a test, so a replay cannot tell which tests name a changed fixture; that lookup is asserted
over the live checkout in ``test_data_readers.py``. What a replay can show is which trigger a merge hits.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_tree_snapshot import (
    read_snapshot_file,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayDecision,
    ModelReplayRequest,
    ModelReplayResult,
)
from scripts.ci.detect_test_paths import REPO_ROOT

pytestmark = pytest.mark.unit

INPUT_PATH = REPO_ROOT / "config" / "test_selection_replay_input.json"
ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
FIXTURE_ROOT = "tests/fixtures/"

FOUR_FIXTURE_MERGES = ("7819ffecd0", "006a153ee0", "bd745d07d1", "b9e96a63b8")
TWO_LOCK_MERGES = ("79f31e93f5", "ae69d56977")
SIX_MERGES = (*FOUR_FIXTURE_MERGES, *TWO_LOCK_MERGES)


def _replay(*, rule_on: bool) -> dict[str, ModelReplayDecision]:
    """The decision for each of the six merges, keyed by short sha."""
    document: dict[str, Any] = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    snapshot = read_snapshot_file(
        str(REPO_ROOT), document["snapshot_path"], document["snapshot_sha256"]
    )
    if rule_on:
        shipped = load_adjacency_map(ADJACENCY)
        adjacency = snapshot.adjacency.model_copy(
            update={
                "data_file_readers_enabled": True,
                "data_reader_roots": shipped.data_reader_roots,
                "data_reader_fixture_roots": shipped.data_reader_fixture_roots,
                "data_reader_gated_infrastructure_paths": (
                    shipped.data_reader_gated_infrastructure_paths
                ),
                "test_infrastructure_paths": shipped.test_infrastructure_paths,
            }
        )
        snapshot = snapshot.model_copy(update={"adjacency": adjacency})
    request = {
        key: value
        for key, value in document.items()
        if key not in {"snapshot_path", "snapshot_sha256"}
    }
    request["snapshot"] = snapshot.model_dump(mode="json")
    request["merges"] = [
        merge for merge in document["merges"] if merge["sha"][:10] in SIX_MERGES
    ]
    result = HandlerSelectionReplay().handle(ModelReplayRequest.model_validate(request))
    assert isinstance(result, ModelReplayResult), result
    decisions = {decision.sha[:10]: decision for decision in result.decisions}
    assert set(decisions) == set(SIX_MERGES)
    return decisions


@pytest.fixture(scope="module")
def stage_zero() -> dict[str, ModelReplayDecision]:
    return _replay(rule_on=False)


@pytest.fixture(scope="module")
def data_readers_on() -> dict[str, ModelReplayDecision]:
    return _replay(rule_on=True)


def _fixture_triggers(decision: ModelReplayDecision) -> list[str]:
    return [
        trigger.evidence
        for trigger in decision.triggers
        if trigger.reason == "test_infrastructure"
        and trigger.evidence.startswith(FIXTURE_ROOT)
    ]


@pytest.mark.parametrize("sha", SIX_MERGES)
def test_data_readers_replay_rule_off_a_changed_fixture_is_test_infrastructure(
    sha: str, stage_zero: dict[str, ModelReplayDecision]
) -> None:
    # The positive control of the assertions below: the same merge, the shipped setting, hits the trigger.
    decision = stage_zero[sha]
    assert decision.kind.value == "full_suite"
    assert _fixture_triggers(decision), sha


@pytest.mark.parametrize("sha", FOUR_FIXTURE_MERGES)
def test_data_readers_replay_four_fixture_merges_no_longer_hit_the_fixture_trigger(
    sha: str,
    stage_zero: dict[str, ModelReplayDecision],
    data_readers_on: dict[str, ModelReplayDecision],
) -> None:
    decision = data_readers_on[sha]
    assert _fixture_triggers(decision) == [], sha
    assert decision.primary_reason != "test_infrastructure", sha
    # Whatever else the merge changed (a shared module, a module with no adjacency entry) is another rule's,
    # and it still stands with the rule on.
    other_before = {
        (t.reason, t.evidence)
        for t in stage_zero[sha].triggers
        if t.reason != "test_infrastructure"
    }
    other_after = {(t.reason, t.evidence) for t in decision.triggers}
    assert other_before <= other_after, sha


@pytest.mark.parametrize("sha", TWO_LOCK_MERGES)
def test_data_readers_replay_two_lock_merges_stay_full_for_the_lock_and_pyproject(
    sha: str, data_readers_on: dict[str, ModelReplayDecision]
) -> None:
    # The version-line rule for a lock diff belongs to the pyproject rules; here both merges stay a full
    # suite on the lock and the project file, and the fixtures beside them are no longer a trigger.
    decision = data_readers_on[sha]
    assert decision.kind.value == "full_suite"
    assert decision.primary_reason == "test_infrastructure"
    evidence = {
        t.evidence for t in decision.triggers if t.reason == "test_infrastructure"
    }
    assert evidence == {"pyproject.toml", "uv.lock"}, sha
    assert _fixture_triggers(decision) == [], sha
