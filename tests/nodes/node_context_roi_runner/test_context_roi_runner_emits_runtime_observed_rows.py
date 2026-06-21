# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 1 (OMN-12845 / M5): runner emits RUNTIME_OBSERVED_ONLY rows.

A dispatched runner run (driven over an injected test bus) emits one
``ModelAttemptReductionRow`` per (task x arm x trial) where each row:

* has ``proof_class == RUNTIME_OBSERVED_ONLY`` (captured live, not replayed);
* carries a real ``attempt_count`` extracted from the terminal event;
* carries a populated ``context_pack_hash`` for an ON arm; and
* carries a populated ``factor_subset_hash`` so the winning factor is replay
  auditable (BAC plan per-row metadata requirement, line 111).

All bus I/O is injected — no Kafka, no network, no LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.context_roi import ModelContextRoiRunResult
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)

_VALID_EVENT: dict[str, Any] = {
    "attempt_count": 1,
    "first_pass_success": True,
    "contract_passed": True,
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "cost_inference_usd": 0.01,
    "model_id": "local-coder",
    "provider": "local",
    "endpoint_class": "local-coder",
}


def _publisher(sink: list[tuple[str, bytes]]) -> Callable[[str, bytes], None]:
    def _pub(topic: str, payload: bytes) -> None:
        sink.append((topic, payload))

    return _pub


def _consumer(
    payload: dict[str, Any] | None,
) -> Callable[[str, str, float], dict[str, Any] | None]:
    def _consume(
        topic: str, correlation_id: str, timeout: float
    ) -> dict[str, Any] | None:
        return payload

    return _consume


def _request() -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-m5",
        tasks=(
            ModelContextRoiTask(
                task_id="task-1",
                task_description="emit a hello function",
            ),
        ),
        arms=(
            ModelContextRoiArmSpec(label="off", factor_subset=()),
            ModelContextRoiArmSpec(
                label="golden_exemplar", factor_subset=("golden_chain",)
            ),
        ),
        trials_per_cell=1,
        artifact_content_map={"golden_chain": "def hello(): return 'hi'"},
    )


class TestRunnerEmitsRuntimeObservedRows:
    def test_every_row_is_runtime_observed_only(self) -> None:
        handler = HandlerContextRoiRunner(
            event_publisher=_publisher([]),
            event_consumer=_consumer(_VALID_EVENT),
        )
        result = handler.handle(_request())
        assert isinstance(result, ModelContextRoiRunResult)
        assert result.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY
        assert len(result.rows) == 2  # 1 task x 2 arms x 1 trial
        for row in result.rows:
            assert row.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY

    def test_rows_carry_real_attempt_count(self) -> None:
        handler = HandlerContextRoiRunner(
            event_publisher=_publisher([]),
            event_consumer=_consumer(_VALID_EVENT),
        )
        result = handler.handle(_request())
        for row in result.rows:
            assert row.attempt_count == 1

    def test_on_arm_carries_context_pack_and_factor_subset_hash(self) -> None:
        handler = HandlerContextRoiRunner(
            event_publisher=_publisher([]),
            event_consumer=_consumer(_VALID_EVENT),
        )
        result = handler.handle(_request())
        on_rows = [
            r for r in result.rows if r.context_factor_subset == "golden_exemplar"
        ]
        assert on_rows, "expected an ON-arm row"
        for row in on_rows:
            assert row.context_pack_hash, (
                "ON arm must carry a populated context_pack_hash"
            )
            assert row.factor_subset_hash, (
                "ON arm must carry a populated factor_subset_hash so the winning "
                "factor is replay auditable"
            )
