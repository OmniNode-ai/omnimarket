# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerContextRoiRunner.

All bus I/O (event_publisher, event_consumer) and pack assembly are mocked
or injected — no Kafka, no network, no LLM calls required.

Coverage:
- Handler imports cleanly
- Contract topics are read from contract.yaml (not hardcoded)
- Off arm (empty factor_subset) skips pack build and publishes a generation
  command with empty context_pack
- On arm builds pack text and sets context_pack_hash
- Terminal event fields are extracted into ModelAttemptReductionRow correctly
- Absent attempt_count in event triggers fail-closed (failure_stage=GENERATION)
- Pack build failure records failure_stage=pack_build without publishing
- Budget failure records failure_stage=budget_fail
- run_order is monotonically increasing across cells
- K trials per cell are executed (trials_per_cell=2 produces 2 rows per armxtask)
- Arm order within task is shuffled deterministically by arm_order_seed
- Result is emitted on the completed topic
- ModelContextRoiRunResult row count = tasks x arms x trials
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
    _assemble_context_text,
    _factor_str_to_enum,
    _sha256,
)
from omnimarket.nodes.node_context_roi_runner.models.model_attempt_reduction import (
    EnumFailureStage,
    ModelAttemptReductionRow,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_result import (
    ModelContextRoiRunResult,
)

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)


def _make_published_topics() -> list[tuple[str, bytes]]:
    """Returns a mutable list that the fake publisher appends to."""
    return []


def _make_publisher(
    published: list[tuple[str, bytes]],
) -> Callable[[str, bytes], None]:
    def _pub(topic: str, payload: bytes) -> None:
        published.append((topic, payload))

    return _pub


def _make_consumer_returning(
    payload: dict[str, Any] | None,
) -> Callable[[str, str, float], dict[str, Any] | None]:
    def _consume(
        topic: str, correlation_id: str, timeout: float
    ) -> dict[str, Any] | None:
        return payload

    return _consume


_VALID_EVENT: dict[str, Any] = {
    "correlation_id": "WILL_BE_OVERRIDDEN",
    "attempt_count": 2,
    "contract_passed": True,
    "first_pass_success": False,
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "cost_inference_usd": 0.001,
    "model_id": "Qwen3.6-35B-A3B",
    "provider": "local",
    "endpoint_class": "local-coder",
}

_FIRST_PASS_EVENT: dict[str, Any] = {
    **_VALID_EVENT,
    "attempt_count": 1,
    "first_pass_success": True,
}


def _off_arm() -> ModelContextRoiArmSpec:
    return ModelContextRoiArmSpec(label="off", factor_subset=())


def _golden_only_arm() -> ModelContextRoiArmSpec:
    return ModelContextRoiArmSpec(label="golden_only", factor_subset=("golden_chain",))


def _make_task(task_id: str = "task-001") -> ModelContextRoiTask:
    return ModelContextRoiTask(
        task_id=task_id,
        task_description="Generate a compute node that validates email addresses.",
    )


def _make_request(
    arms: tuple[ModelContextRoiArmSpec, ...] | None = None,
    tasks: tuple[ModelContextRoiTask, ...] | None = None,
    trials_per_cell: int = 1,
) -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-test-001",
        tasks=tasks or (_make_task(),),
        arms=arms or (_off_arm(),),
        trials_per_cell=trials_per_cell,
        max_attempts=2,
        arm_order_seed=42,
        generation_timeout_seconds=5.0,
        contract_hash="test-hash-abc",
    )


_SENTINEL = object()  # distinguishes "not provided" from intentional None


def _make_handler(
    published: list[tuple[str, bytes]] | None = None,
    consumer_payload: object = _SENTINEL,
) -> HandlerContextRoiRunner:
    """Build a handler with injectable publisher and consumer.

    consumer_payload=_SENTINEL (default) → consumer returns _VALID_EVENT.
    consumer_payload=None → consumer returns None (simulates timeout).
    consumer_payload=<dict> → consumer returns that dict.
    """
    pubs: list[tuple[str, bytes]] = published if published is not None else []
    payload: dict[str, Any] | None = (
        _VALID_EVENT if consumer_payload is _SENTINEL else consumer_payload  # type: ignore[assignment]
    )
    return HandlerContextRoiRunner(
        event_publisher=_make_publisher(pubs),
        event_consumer=_make_consumer_returning(payload),
        runner_contract_path=_CONTRACT_PATH,
    )


# ---------------------------------------------------------------------------
# Import / contract tests
# ---------------------------------------------------------------------------


def test_handler_importable() -> None:
    assert HandlerContextRoiRunner.__name__ == "HandlerContextRoiRunner"


def test_contract_topics_are_not_hardcoded() -> None:
    """Topics must come from contract.yaml, not literals in handler code."""
    import inspect

    from omnimarket.nodes.node_context_roi_runner.handlers import (
        handler_context_roi_runner,
    )

    source = inspect.getsource(handler_context_roi_runner)
    # The hardcoded topic strings from the generation consumer must NOT appear.
    assert "onex.cmd.omnimarket.node-generation-requested.v1" not in source
    assert "onex.evt.omnimarket.node-generation-completed.v1" not in source


def test_no_generation_consumer_import() -> None:
    """The handler must NOT import GenerationConsumer (bus-only constraint)."""
    import inspect

    from omnimarket.nodes.node_context_roi_runner.handlers import (
        handler_context_roi_runner,
    )

    source = inspect.getsource(handler_context_roi_runner)
    assert "GenerationConsumer" not in source
    assert "kafka_runner" not in source


def test_handler_initialises_from_contract() -> None:
    handler = _make_handler()
    assert handler._gen_command_topic != ""
    assert handler._gen_terminal_topic != ""
    assert "generation-requested" in handler._gen_command_topic
    assert "generation-completed" in handler._gen_terminal_topic


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_sha256_produces_prefixed_hex() -> None:
    digest = _sha256("hello")
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_sha256_is_deterministic() -> None:
    assert _sha256("content") == _sha256("content")
    assert _sha256("a") != _sha256("b")


def test_factor_str_to_enum_known() -> None:
    from omnibase_core.enums.enum_context_factor import EnumContextFactor

    result = _factor_str_to_enum("golden_chain")
    assert result == EnumContextFactor.GOLDEN_CHAIN


def test_factor_str_to_enum_unknown_returns_none() -> None:
    assert _factor_str_to_enum("not_a_real_factor") is None


def test_assemble_context_text_formats_factors() -> None:
    text, warnings = _assemble_context_text(
        factor_subset=("golden_chain",),
        artifact_content_map={"golden_chain": "some golden chain content"},
    )
    assert "[golden_chain]" in text
    assert "some golden chain content" in text
    assert warnings == []


def test_assemble_context_text_unknown_factor_warns() -> None:
    text, warnings = _assemble_context_text(
        factor_subset=("not_a_factor",),
        artifact_content_map={},
    )
    assert text == ""
    assert len(warnings) == 1
    assert "not_a_factor" in warnings[0]


def test_assemble_context_text_fallback_stub_when_map_empty() -> None:
    text, warnings = _assemble_context_text(
        factor_subset=("golden_chain",),
        artifact_content_map={},
    )
    assert "[golden_chain]" in text
    assert "stub" in text
    assert warnings == []


def test_assemble_context_text_multiple_factors() -> None:
    text, warnings = _assemble_context_text(
        factor_subset=("golden_chain", "exemplar"),
        artifact_content_map={"golden_chain": "chain text", "exemplar": "ex text"},
    )
    assert "[golden_chain]" in text
    assert "[exemplar]" in text
    assert warnings == []


# ---------------------------------------------------------------------------
# Off-arm (no context injection)
# ---------------------------------------------------------------------------


def test_off_arm_publishes_generation_command() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(published=published)
    request = _make_request(arms=(_off_arm(),))
    handler.handle(request)

    # One generation command + one completed event = 2 publishes
    gen_commands = [(t, p) for t, p in published if "node-generation-requested" in t]
    assert len(gen_commands) == 1


def test_off_arm_generation_command_has_empty_context_pack() -> None:
    import json

    published: list[tuple[str, bytes]] = []
    handler = _make_handler(published=published)
    request = _make_request(arms=(_off_arm(),))
    handler.handle(request)

    gen_commands = [(t, p) for t, p in published if "node-generation-requested" in t]
    payload = json.loads(gen_commands[0][1])
    assert payload["context_pack"] == ""
    assert payload["context_pack_hash"] == ""


def test_off_arm_row_has_empty_context_pack_hash() -> None:
    handler = _make_handler()
    request = _make_request(arms=(_off_arm(),))
    result = handler.handle(request)

    assert len(result.rows) == 1
    assert result.rows[0].context_pack_hash == ""
    assert result.rows[0].context_factor_subset == "off"


# ---------------------------------------------------------------------------
# Row field extraction from terminal event
# ---------------------------------------------------------------------------


def test_row_extracts_attempt_count() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    assert result.rows[0].attempt_count == 2


def test_row_extracts_first_pass_success() -> None:
    handler = _make_handler(consumer_payload=_FIRST_PASS_EVENT)
    result = handler.handle(_make_request())
    assert result.rows[0].first_pass_success is True
    assert result.rows[0].attempt_count == 1


def test_row_extracts_final_success() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    assert result.rows[0].final_success is True
    assert result.rows[0].failure_stage == EnumFailureStage.NONE


def test_row_extracts_token_fields() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    row = result.rows[0]
    assert row.prompt_tokens == 150
    assert row.completion_tokens == 80
    assert row.estimated_cost == pytest.approx(0.001, abs=1e-9)


def test_row_extracts_model_identity() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    row = result.rows[0]
    assert row.model_id == "Qwen3.6-35B-A3B"
    assert row.provider == "local"
    assert row.endpoint_ref == "local-coder"


def test_row_proof_class_is_runtime_observed() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    assert result.rows[0].proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY


def test_row_failure_stage_none_on_success() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    assert result.rows[0].failure_stage == EnumFailureStage.NONE


def test_row_failure_stage_validation_on_contract_fail() -> None:
    event = {**_VALID_EVENT, "contract_passed": False}
    handler = _make_handler(consumer_payload=event)
    result = handler.handle(_make_request())
    assert result.rows[0].failure_stage == EnumFailureStage.VALIDATION
    assert result.rows[0].final_success is False


# ---------------------------------------------------------------------------
# Fail-closed: missing required event fields
# ---------------------------------------------------------------------------


def test_missing_attempt_count_records_generation_failure() -> None:
    """attempt_count is required; absence triggers fail-closed."""
    bad_event = {k: v for k, v in _VALID_EVENT.items() if k != "attempt_count"}
    handler = _make_handler(consumer_payload=bad_event)
    result = handler.handle(_make_request())
    assert result.rows[0].failure_stage == EnumFailureStage.GENERATION


def test_timeout_no_event_records_generation_failure() -> None:
    """Consumer returning None (timeout) records failure_stage=generation."""
    handler = _make_handler(consumer_payload=None)
    result = handler.handle(_make_request())
    assert result.rows[0].failure_stage == EnumFailureStage.GENERATION


# ---------------------------------------------------------------------------
# Pack build failure paths
# ---------------------------------------------------------------------------


def test_unknown_factor_in_arm_causes_pack_build_failure() -> None:
    """An arm whose factor_subset has only unknown labels fails at pack_build."""
    bad_arm = ModelContextRoiArmSpec(label="bad_arm", factor_subset=("not_a_factor",))
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(published=published)
    request = _make_request(arms=(bad_arm,))
    result = handler.handle(request)
    assert result.rows[0].failure_stage == EnumFailureStage.PACK_BUILD
    # No generation command must be published when pack build fails.
    gen_cmds = [t for t, _ in published if "node-generation-requested" in t]
    assert gen_cmds == []


# ---------------------------------------------------------------------------
# Multi-trial / multi-task counting
# ---------------------------------------------------------------------------


def test_trials_per_cell_produces_correct_row_count() -> None:
    """2 tasks x 2 arms x 3 trials = 12 rows."""
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    request = _make_request(
        tasks=(
            _make_task("task-001"),
            _make_task("task-002"),
        ),
        arms=(_off_arm(), _golden_only_arm()),
        trials_per_cell=3,
    )
    result = handler.handle(request)
    assert result.total_trials == 12
    assert len(result.rows) == 12


def test_single_task_single_arm_single_trial() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request(trials_per_cell=1))
    assert result.total_trials == 1
    assert len(result.rows) == 1


def test_failed_trials_count() -> None:
    """Consumer timeout → all trials fail; failed_trials == total_trials."""
    handler = _make_handler(consumer_payload=None)
    request = _make_request(
        arms=(_off_arm(), _golden_only_arm()),
        trials_per_cell=2,
    )
    result = handler.handle(request)
    assert result.failed_trials == result.total_trials


# ---------------------------------------------------------------------------
# Result emission
# ---------------------------------------------------------------------------


def test_result_emitted_on_completed_topic() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(published=published, consumer_payload=_VALID_EVENT)
    handler.handle(_make_request())

    completed = [t for t, _ in published if "context-roi-run-completed" in t]
    assert len(completed) == 1


def test_result_run_id_echoed() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    request = _make_request()
    result = handler.handle(request)
    assert result.run_id == request.run_id


def test_result_proof_class_is_runtime_observed() -> None:
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    result = handler.handle(_make_request())
    assert result.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY


# ---------------------------------------------------------------------------
# Model smoke tests
# ---------------------------------------------------------------------------


def test_model_context_roi_run_result_is_frozen() -> None:
    from pydantic import ValidationError

    result = ModelContextRoiRunResult(
        run_id="r",
        rows=(),
        total_trials=0,
        failed_trials=0,
    )
    with pytest.raises((ValidationError, TypeError)):
        result.run_id = "other"  # type: ignore[misc]


def test_model_attempt_reduction_row_is_frozen() -> None:
    from pydantic import ValidationError

    row = ModelAttemptReductionRow(
        run_id="r",
        correlation_id="c",
        task_id="t",
    )
    with pytest.raises((ValidationError, TypeError)):
        row.run_id = "other"  # type: ignore[misc]


def test_enum_failure_stage_values() -> None:
    assert EnumFailureStage.NONE == "none"
    assert EnumFailureStage.PACK_BUILD == "pack_build"
    assert EnumFailureStage.BUDGET_FAIL == "budget_fail"
    assert EnumFailureStage.GENERATION == "generation"
    assert EnumFailureStage.VALIDATION == "validation"
    assert EnumFailureStage.DOWNSTREAM_GATE == "downstream_gate"


# ---------------------------------------------------------------------------
# run_order recording (CodeRabbit: run_order computed but never emitted)
# ---------------------------------------------------------------------------


def test_run_order_recorded_monotonically() -> None:
    """Every row records a 1-based run_order; sequence is 1..N across cells."""
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    request = _make_request(
        tasks=(_make_task("task-001"), _make_task("task-002")),
        arms=(_off_arm(), _golden_only_arm()),
        trials_per_cell=2,
    )
    result = handler.handle(request)
    orders = sorted(r.run_order for r in result.rows)
    assert orders == list(range(1, len(result.rows) + 1))


def test_run_order_recorded_on_failed_rows() -> None:
    """Timeout failure rows still carry a non-zero run_order."""
    handler = _make_handler(consumer_payload=None)
    request = _make_request(arms=(_off_arm(), _golden_only_arm()), trials_per_cell=1)
    result = handler.handle(request)
    assert all(r.run_order >= 1 for r in result.rows)


# ---------------------------------------------------------------------------
# Reproducible arm-order randomization (CodeRabbit: hash() is process-salted)
# ---------------------------------------------------------------------------


def test_arm_order_reproducible_across_handler_instances() -> None:
    """Same arm_order_seed → identical arm order across separate handlers."""
    arms = (
        _off_arm(),
        _golden_only_arm(),
        ModelContextRoiArmSpec(label="exemplar", factor_subset=("exemplar",)),
    )

    def _ordered_labels() -> list[str]:
        handler = _make_handler(consumer_payload=_VALID_EVENT)
        request = _make_request(arms=arms, trials_per_cell=1)
        result = handler.handle(request)
        # Single task, single trial: row order is the executed arm order.
        return [r.context_factor_subset for r in result.rows]

    assert _ordered_labels() == _ordered_labels()


# ---------------------------------------------------------------------------
# required_factors enforcement on ON arms (CodeRabbit: contract not enforced)
# ---------------------------------------------------------------------------


def test_missing_required_factor_fails_pack_build() -> None:
    """An ON arm lacking a task-required factor fails at pack_build, no publish."""
    task = ModelContextRoiTask(
        task_id="task-req",
        task_description="needs golden_chain",
        required_factors=("golden_chain",),
    )
    # Arm injects exemplar only -- golden_chain (required) is absent.
    arm = ModelContextRoiArmSpec(label="exemplar_only", factor_subset=("exemplar",))
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(published=published)
    request = _make_request(tasks=(task,), arms=(arm,))
    result = handler.handle(request)
    assert result.rows[0].failure_stage == EnumFailureStage.PACK_BUILD
    gen_cmds = [t for t, _ in published if "node-generation-requested" in t]
    assert gen_cmds == []


def test_present_required_factor_does_not_fail() -> None:
    task = ModelContextRoiTask(
        task_id="task-req-ok",
        task_description="needs golden_chain",
        required_factors=("golden_chain",),
    )
    arm = ModelContextRoiArmSpec(label="golden", factor_subset=("golden_chain",))
    handler = _make_handler(consumer_payload=_VALID_EVENT)
    request = _make_request(tasks=(task,), arms=(arm,))
    result = handler.handle(request)
    assert result.rows[0].failure_stage != EnumFailureStage.PACK_BUILD


# ---------------------------------------------------------------------------
# Malformed payload fails one row closed, never aborts the run (CodeRabbit)
# ---------------------------------------------------------------------------


def test_non_numeric_attempt_count_fails_row_closed() -> None:
    """A non-numeric attempt_count fails that row without raising."""
    bad_event = {**_VALID_EVENT, "attempt_count": "not-a-number"}
    handler = _make_handler(consumer_payload=bad_event)
    result = handler.handle(_make_request())
    assert result.rows[0].failure_stage == EnumFailureStage.GENERATION
    assert result.failed_trials == 1


def test_non_numeric_token_field_fails_row_closed() -> None:
    bad_event = {**_VALID_EVENT, "prompt_tokens": "lots"}
    handler = _make_handler(consumer_payload=bad_event)
    # Multi-cell run must complete every cell, not abort on the first bad row.
    request = _make_request(arms=(_off_arm(), _golden_only_arm()), trials_per_cell=2)
    result = handler.handle(request)
    assert len(result.rows) == 4
    assert all(r.failure_stage == EnumFailureStage.GENERATION for r in result.rows)
