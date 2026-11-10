# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of node_test_selection_selector_compute (OMN-23296)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    EnumSelectorOutcome,
    ModelSelectorDecision,
    ModelSelectorPolicy,
    ModelSelectorRequest,
)

pytestmark = pytest.mark.unit

POLICY = ModelSelectorPolicy(
    target_shard_seconds=100,
    max_narrowed_shards=5,
    durations_path="durations.json",
    path_count_shards=((2, 1), (5, 2)),
    path_count_max_shards=3,
)
DURATIONS = {
    "tests/a/test_x.py": 250.0,
    "tests/a/test_y.py": 50.0,
    "tests/b/test_z.py": 1000.0,
    "tests/c/test_w.py": 10.0,
}


def _request(*paths: str) -> ModelSelectorRequest:
    return ModelSelectorRequest(selected_paths=paths, repo_root="/unused")


def _handler(
    durations: dict[str, float] | None = DURATIONS,
) -> HandlerTestSelectionSelector:
    return HandlerTestSelectionSelector(SourceInMemory(durations), POLICY)


def test_selection_selector_shard_count_follows_estimated_seconds() -> None:
    decision = _handler().handle(_request("tests/a/"))
    assert decision.outcome is EnumSelectorOutcome.NARROWED
    assert decision.estimated_seconds == 300.0
    assert decision.split_count == 3
    assert decision.shard_seconds == 100.0


def test_selection_selector_a_file_path_counts_only_that_file() -> None:
    decision = _handler().handle(_request("tests/a/test_y.py"))
    assert decision.estimated_seconds == 50.0
    assert decision.split_count == 1


def test_selection_selector_path_count_rule_is_a_floor() -> None:
    decision = _handler().handle(
        _request("tests/c/", "tests/none1/", "tests/none2/", "tests/none3/")
    )
    assert decision.estimated_seconds == 10.0
    assert decision.split_count == 2


def test_selection_selector_path_count_rule_tops_out_at_its_own_maximum() -> None:
    paths = [f"tests/none{i}/" for i in range(9)]
    assert _handler().handle(_request(*paths)).split_count == 3


def test_selection_selector_too_heavy_takes_the_full_suite() -> None:
    decision = _handler().handle(_request("tests/a/", "tests/b/"))
    assert decision.outcome is EnumSelectorOutcome.FULL_SUITE_TOO_HEAVY
    assert decision.escalates_to_full_suite is True
    assert decision.split_count is None
    assert decision.estimated_seconds == 1300.0


def test_selection_selector_exactly_the_bound_stays_narrowed() -> None:
    durations = {"tests/a/test_x.py": 500.0}
    decision = _handler(durations).handle(_request("tests/a/"))
    assert decision.outcome is EnumSelectorOutcome.NARROWED
    assert decision.split_count == 5


def test_selection_selector_unread_durations_fail_closed() -> None:
    decision = _handler(None).handle(_request("tests/a/"))
    assert decision.outcome is EnumSelectorOutcome.FULL_SUITE_DURATIONS_UNREAD
    assert decision.split_count is None
    assert decision.estimated_seconds is None


def test_selection_selector_decision_refuses_a_split_count_with_an_escalation() -> None:
    with pytest.raises(ValidationError):
        ModelSelectorDecision(
            outcome=EnumSelectorOutcome.FULL_SUITE_TOO_HEAVY, split_count=2
        )
    with pytest.raises(ValidationError):
        ModelSelectorDecision(outcome=EnumSelectorOutcome.NARROWED)


def test_selection_selector_policy_refuses_a_ladder_over_the_narrowed_maximum() -> None:
    with pytest.raises(ValidationError):
        ModelSelectorPolicy(
            target_shard_seconds=100,
            max_narrowed_shards=2,
            durations_path="d.json",
            path_count_shards=((2, 1), (5, 3)),
            path_count_max_shards=2,
        )


def test_selection_selector_shipped_policy_loads() -> None:
    policy = load_policy()
    assert policy.target_shard_seconds * policy.max_narrowed_shards == 1800


def test_selection_selector_durations_file_source_reads_and_fails_closed(
    tmp_path: Path,
) -> None:
    (tmp_path / "d.json").write_text(json.dumps({"tests/a/test_x.py": 5, "bad": "x"}))
    source = SourceDurationsFile()
    assert source.read(str(tmp_path), "d.json") == {"tests/a/test_x.py": 5.0}
    assert source.read(str(tmp_path), "missing.json") is None
    (tmp_path / "list.json").write_text("[1, 2]")
    assert source.read(str(tmp_path), "list.json") is None
    (tmp_path / "bad.json").write_text("not json")
    assert source.read(str(tmp_path), "bad.json") is None
