# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_context_roi_runner (OMN-12798).

Exercises the full runner chain end-to-end with the bus seam injected (no live
Kafka, no LLM): for each (task x arm x trial) the handler assembles a context
pack, publishes a generation command on the contract-declared generation
command topic, consumes a terminal generation event correlated by
correlation_id, extracts typed telemetry into a ModelAttemptReductionRow, and
emits a ModelContextRoiRunResult on the contract-declared completed topic.

Chain verified:
  request -> per-cell generation command publish (bus)
          -> terminal generation event consume (bus)
          -> typed ModelAttemptReductionRow per cell
          -> ModelContextRoiRunResult emitted on completed topic

All topics are read from contract.yaml (not hardcoded). The publisher and
consumer are injected fakes that record traffic and replay a deterministic
terminal event per correlation_id, so the run is fully offline and
deterministic (RUNTIME_OBSERVED_ONLY rows, frozen for REPLAY_PROVEN scoring
downstream by node_context_roi_compute).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_attempt_reduction import (
    EnumFailureStage,
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
# Node directory / contract fixtures
# ---------------------------------------------------------------------------

_NODE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
)
_CONTRACT_PATH = _NODE_DIR / "contract.yaml"
_METADATA_PATH = _NODE_DIR / "metadata.yaml"


@pytest.fixture
def contract() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(_CONTRACT_PATH.read_text())
    return data


# ---------------------------------------------------------------------------
# Bus seam fakes (injected) — record traffic and replay a deterministic
# terminal generation event per correlation_id.
# ---------------------------------------------------------------------------


class _FakeBus:
    """Records published traffic and replays a terminal generation event.

    The replayed event echoes attempt_count / first_pass_success / token /
    model identity fields back as the live generation consumer would, so the
    handler's _extract_row path is exercised fully and deterministically.
    """

    def __init__(self, gen_terminal_topic: str) -> None:
        self.gen_terminal_topic = gen_terminal_topic
        self.published: list[tuple[str, dict[str, Any]]] = []
        # correlation_id -> command payload, captured at publish time so the
        # consumer can correlate the replayed terminal event.
        self._pending: dict[str, dict[str, Any]] = {}

    def publish(self, topic: str, payload: bytes) -> None:
        decoded = json.loads(payload.decode("utf-8"))
        self.published.append((topic, decoded))
        corr = decoded.get("correlation_id")
        if corr:
            self._pending[corr] = decoded

    def consume(
        self, topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        assert topic == self.gen_terminal_topic
        command = self._pending.get(correlation_id)
        if command is None:
            return None
        # On-arm commands carry a non-empty context_pack; first-pass success
        # for on arms, two-attempt success for the off baseline — gives the
        # downstream scorer dynamic range without any live LLM call.
        has_context = bool(command.get("context_pack"))
        return {
            "correlation_id": correlation_id,
            "attempt_count": 1 if has_context else 2,
            "contract_passed": True,
            "first_pass_success": has_context,
            "prompt_tokens": 1500 if has_context else 200,
            "completion_tokens": 120,
            "cost_inference_usd": 0.0032 if has_context else 0.0005,
            "model_id": "local-coder-v1",
            "provider": "local",
            "endpoint_class": "local-coder",
        }


def _build_request() -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="golden-chain-roi-001",
        tasks=(
            ModelContextRoiTask(
                task_id="sea_001",
                task_description="generate a minimal compute node contract",
            ),
            ModelContextRoiTask(
                task_id="sea_002",
                task_description="generate a reducer handler with typed FSM",
            ),
        ),
        arms=(
            ModelContextRoiArmSpec(label="off", factor_subset=()),
            ModelContextRoiArmSpec(
                label="golden_only", factor_subset=("golden_chain",)
            ),
        ),
        trials_per_cell=2,
        arm_order_seed=7,
        artifact_content_map={"golden_chain": "<golden chain text>"},
    )


# ---------------------------------------------------------------------------
# Contract / metadata gate
# ---------------------------------------------------------------------------


class TestContract:
    def test_contract_exists(self) -> None:
        assert _CONTRACT_PATH.exists()

    def test_metadata_exists(self) -> None:
        assert _METADATA_PATH.exists()

    def test_node_is_effect(self, contract: dict[str, Any]) -> None:
        assert contract["name"] == "node_context_roi_runner"
        assert contract["node_type"] == "effect"

    def test_topics_follow_convention(self, contract: dict[str, Any]) -> None:
        bus = contract.get("event_bus", {})
        for topic in bus.get("subscribe_topics", []):
            assert topic.startswith("onex.cmd."), topic
            assert topic.endswith(".v1"), topic
        for topic in bus.get("publish_topics", []):
            assert topic.startswith("onex.evt."), topic
            assert topic.endswith(".v1"), topic

    def test_generation_pipeline_topics_declared(
        self, contract: dict[str, Any]
    ) -> None:
        gen = contract.get("generation_pipeline", {})
        assert gen.get("command_topic", "").startswith("onex.cmd.")
        assert gen.get("terminal_event_topic", "").startswith("onex.evt.")


# ---------------------------------------------------------------------------
# Golden chain: full matrix over the injected bus
# ---------------------------------------------------------------------------


class TestGoldenChain:
    @pytest.fixture
    def bus(self, contract: dict[str, Any]) -> _FakeBus:
        terminal = contract["generation_pipeline"]["terminal_event_topic"]
        return _FakeBus(gen_terminal_topic=terminal)

    @pytest.fixture
    def result(self, bus: _FakeBus) -> ModelContextRoiRunResult:
        handler = HandlerContextRoiRunner(
            event_publisher=bus.publish,
            event_consumer=bus.consume,
        )
        return handler.handle(_build_request())

    def test_result_is_typed(self, result: ModelContextRoiRunResult) -> None:
        assert isinstance(result, ModelContextRoiRunResult)

    def test_run_id_echoed(self, result: ModelContextRoiRunResult) -> None:
        assert result.run_id == "golden-chain-roi-001"

    def test_row_count_is_tasks_x_arms_x_trials(
        self, result: ModelContextRoiRunResult
    ) -> None:
        # 2 tasks x 2 arms x 2 trials = 8 cells.
        assert len(result.rows) == 8
        assert result.total_trials == 8

    def test_every_cell_succeeded(self, result: ModelContextRoiRunResult) -> None:
        assert result.failed_trials == 0
        assert all(r.failure_stage == EnumFailureStage.NONE for r in result.rows)

    def test_generation_command_published_per_cell(
        self, bus: _FakeBus, contract: dict[str, Any], result: ModelContextRoiRunResult
    ) -> None:
        command_topic = contract["generation_pipeline"]["command_topic"]
        gen_commands = [p for p in bus.published if p[0] == command_topic]
        assert len(gen_commands) == 8

    def test_result_emitted_on_completed_topic(
        self, bus: _FakeBus, contract: dict[str, Any], result: ModelContextRoiRunResult
    ) -> None:
        completed_topic = next(
            t
            for t in contract["event_bus"]["publish_topics"]
            if "context-roi-run-completed" in t
        )
        emitted = [p for p in bus.published if p[0] == completed_topic]
        assert len(emitted) == 1

    def test_off_arm_rows_have_empty_pack_hash(
        self, result: ModelContextRoiRunResult
    ) -> None:
        off_rows = [r for r in result.rows if r.context_factor_subset == "off"]
        assert len(off_rows) == 4
        assert all(r.context_pack_hash == "" for r in off_rows)

    def test_on_arm_rows_have_pack_hash(self, result: ModelContextRoiRunResult) -> None:
        on_rows = [r for r in result.rows if r.context_factor_subset == "golden_only"]
        assert len(on_rows) == 4
        assert all(r.context_pack_hash.startswith("sha256:") for r in on_rows)

    def test_on_arm_first_pass_success(self, result: ModelContextRoiRunResult) -> None:
        on_rows = [r for r in result.rows if r.context_factor_subset == "golden_only"]
        assert all(r.first_pass_success for r in on_rows)
        assert all(r.attempt_count == 1 for r in on_rows)

    def test_off_arm_not_first_pass(self, result: ModelContextRoiRunResult) -> None:
        off_rows = [r for r in result.rows if r.context_factor_subset == "off"]
        assert all(not r.first_pass_success for r in off_rows)
        assert all(r.attempt_count == 2 for r in off_rows)

    def test_model_identity_from_event(self, result: ModelContextRoiRunResult) -> None:
        # Identity is read back from the terminal event, never hardcoded here.
        assert all(r.model_id == "local-coder-v1" for r in result.rows)
        assert all(r.provider == "local" for r in result.rows)
        assert all(r.endpoint_ref == "local-coder" for r in result.rows)

    def test_rows_are_runtime_observed(self, result: ModelContextRoiRunResult) -> None:
        assert all(
            r.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY for r in result.rows
        )

    def test_result_proof_class_runtime_observed(
        self, result: ModelContextRoiRunResult
    ) -> None:
        assert result.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY

    def test_deterministic_across_runs(self, contract: dict[str, Any]) -> None:
        terminal = contract["generation_pipeline"]["terminal_event_topic"]
        results = []
        for _ in range(2):
            bus = _FakeBus(gen_terminal_topic=terminal)
            handler = HandlerContextRoiRunner(
                event_publisher=bus.publish,
                event_consumer=bus.consume,
            )
            results.append(handler.handle(_build_request()))
        r1, r2 = results
        assert len(r1.rows) == len(r2.rows)
        assert [r.context_factor_subset for r in r1.rows] == [
            r.context_factor_subset for r in r2.rows
        ]
        assert r1.failed_trials == r2.failed_trials


# ---------------------------------------------------------------------------
# Fail-closed chain: terminal event missing attempt_count
# ---------------------------------------------------------------------------


class TestGoldenChainFailClosed:
    def test_missing_attempt_count_records_generation_failure(
        self, contract: dict[str, Any]
    ) -> None:
        terminal = contract["generation_pipeline"]["terminal_event_topic"]

        def consume_malformed(
            topic: str, correlation_id: str, timeout_seconds: float
        ) -> dict[str, Any]:
            # Terminal event without attempt_count must fail closed.
            return {"correlation_id": correlation_id, "contract_passed": True}

        bus = _FakeBus(gen_terminal_topic=terminal)
        handler = HandlerContextRoiRunner(
            event_publisher=bus.publish,
            event_consumer=consume_malformed,
        )
        result = handler.handle(_build_request())
        assert result.failed_trials == len(result.rows)
        assert all(r.failure_stage == EnumFailureStage.GENERATION for r in result.rows)

    def test_timeout_records_generation_failure(self, contract: dict[str, Any]) -> None:
        terminal = contract["generation_pipeline"]["terminal_event_topic"]

        def consume_timeout(
            topic: str, correlation_id: str, timeout_seconds: float
        ) -> None:
            return None

        bus = _FakeBus(gen_terminal_topic=terminal)
        handler = HandlerContextRoiRunner(
            event_publisher=bus.publish,
            event_consumer=consume_timeout,
        )
        result = handler.handle(_build_request())
        assert result.failed_trials == len(result.rows)
        assert all(r.failure_stage == EnumFailureStage.GENERATION for r in result.rows)
