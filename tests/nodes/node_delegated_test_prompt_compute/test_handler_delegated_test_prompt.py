# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19361 — the write and repair prompt compute."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegated_test_prompt_compute.handlers.handler_delegated_test_prompt import (
    DelegatedTestPromptRefusedError,
    HandlerDelegatedTestPrompt,
    build_prompt_bundle,
)
from omnimarket.nodes.node_delegated_test_prompt_compute.models.model_delegated_test_prompt import (
    MAX_EXCERPT_CHARS,
    ModelDelegatedTestPromptRequest,
    ModelFailureContext,
)

pytestmark = pytest.mark.unit

CRITERION = (
    "`KafkaSnapshotDeltaPublisher.publish` called inside a running loop refuses with a "
    "typed error naming the seam's precondition, and leaks no coroutine."
)
TARGET = "src/omnimarket/projection/snapshot_publisher.py"
TEST_PATH = "tests/unit/projection/test_dtl_generated_0f1e2d3c.py"
EXCERPT = (
    "class KafkaSnapshotDeltaPublisher:\n    def publish(self, message):\n        ...\n"
)
HIDDEN = (
    "TestThePublisherRefusesFromARunningLoop",
    "test_a_running_loop_is_refused_typed_and_leaks_no_coroutine",
)


def _request(**overrides: object) -> ModelDelegatedTestPromptRequest:
    values: dict[str, object] = {
        "mode": "write",
        "criterion": CRITERION,
        "target_path": TARGET,
        "target_excerpt": EXCERPT,
        "test_path": TEST_PATH,
        "forbidden_fragments": HIDDEN,
    }
    values.update(overrides)
    return ModelDelegatedTestPromptRequest(**values)  # type: ignore[arg-type]


def test_the_write_prompt_carries_the_criterion_target_and_test_path() -> None:
    bundle = build_prompt_bundle(_request())
    assert CRITERION in bundle.prompt
    assert EXCERPT.strip() in bundle.prompt
    assert TEST_PATH in bundle.prompt
    assert "omnimarket.projection.snapshot_publisher" in bundle.prompt
    assert bundle.test_path == TEST_PATH


def test_the_response_contract_asks_for_exactly_path_and_source() -> None:
    contract = build_prompt_bundle(_request()).response_contract
    assert contract["type"] == "object"
    assert contract["required"] == ["test_path", "test_source"]
    assert set(contract["properties"]) == {"test_path", "test_source"}
    json.dumps(contract)  # serialisable, so it can go to --response-contract


def test_the_repair_prompt_carries_the_previous_test_and_the_digest() -> None:
    failure = ModelFailureContext(
        outcome="failed_call",
        exception_type="AssertionError",
        message="assert 2 == 3",
        frames="tests/x.py:6: AssertionError",
        failing_node_id="tests.x::test_y",
    )
    bundle = build_prompt_bundle(
        _request(
            mode="repair",
            previous_test="def test_y():\n    assert 2 == 3\n",
            failure=failure,
        )
    )
    assert "def test_y()" in bundle.prompt
    assert "assert 2 == 3" in bundle.prompt
    assert "failed_call" in bundle.prompt


def test_repair_without_a_previous_test_or_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="repair"):
        _request(mode="repair")


@pytest.mark.parametrize(
    "field",
    ["criterion", "target_excerpt", "previous_test"],
)
def test_hidden_function_name_in_bundle_is_refused(field: str) -> None:
    overrides: dict[str, object] = {field: f"prefix {HIDDEN[1]} suffix"}
    if field == "previous_test":
        overrides.update(
            mode="repair",
            failure=ModelFailureContext(outcome="failed_call", message="x"),
        )
    with pytest.raises(DelegatedTestPromptRefusedError, match="forbidden"):
        build_prompt_bundle(_request(**overrides))


def test_a_forbidden_fragment_in_the_digest_is_refused_too() -> None:
    failure = ModelFailureContext(outcome="failed_call", message=f"see {HIDDEN[0]}")
    with pytest.raises(DelegatedTestPromptRefusedError):
        build_prompt_bundle(
            _request(mode="repair", previous_test="x = 1\n", failure=failure)
        )


def test_the_target_excerpt_is_capped() -> None:
    bundle = build_prompt_bundle(_request(target_excerpt="x = 1\n" * 10_000))
    assert len(bundle.prompt) < MAX_EXCERPT_CHARS + 6000
    assert "truncated" in bundle.prompt


def test_the_handler_is_the_same_pure_function() -> None:
    request = _request()
    assert HandlerDelegatedTestPrompt().handle(request) == build_prompt_bundle(request)


@pytest.mark.parametrize(
    "node",
    ["node_delegated_test_prompt_compute", "node_delegated_test_control_compute"],
)
def test_neither_handler_imports_an_envelope_type(node: str) -> None:
    node_dir = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes" / node
    sources = [p.read_text() for p in node_dir.rglob("*.py")]
    assert sources
    assert not [s for s in sources if "ModelEventEnvelope" in s]


def test_the_bundle_says_whether_the_excerpt_was_truncated() -> None:
    assert build_prompt_bundle(_request()).excerpt_truncated is False
    long_request = _request(target_excerpt="x = 1\n" * 10_000)
    assert build_prompt_bundle(long_request).excerpt_truncated is True
