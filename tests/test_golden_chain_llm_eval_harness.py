# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_llm_eval_harness.

Verifies the handler can benchmark models against a task corpus, score
outputs deterministically with a FakeLlmClient, and emit completion events
via EventBusInmemory. Exercises every scoring path: code (ruff+mypy),
substring match, dry-run, and error propagation.
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_llm_eval_harness.handlers.handler_llm_eval_harness import (
    EnumLlmEvalTaskType,
    FakeLlmClient,
    LlmEvalRequest,
    ModelLlmEvalTask,
    NodeLlmEvalHarness,
    ProtocolLlmClient,
)

CMD_TOPIC = "onex.cmd.omnimarket.llm-eval-harness-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.llm-eval-harness-completed.v1"


class _RaisingClient:
    """Client that always raises — exercises error path."""

    def complete(self, model_key: str, prompt: str) -> str:
        raise RuntimeError("boom")


@pytest.mark.unit
class TestLlmEvalHarnessGoldenChain:
    """Golden chain: command -> handler -> scored samples -> completion event."""

    async def test_dry_run_shape(self, event_bus: EventBusInmemory) -> None:
        """dry_run should emit a shaped zero-value result without LLM calls."""
        handler = NodeLlmEvalHarness(client=_RaisingClient())
        request = LlmEvalRequest(models=["qwen3-coder-30b"], dry_run=True)

        result = handler.handle(request)

        assert result.dry_run is True
        assert result.samples == []
        assert result.models_benchmarked == 1
        assert result.tasks_run == 0
        assert result.status == "clean"

    async def test_substring_scoring_classification(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Classification tasks score by substring match."""
        client = FakeLlmClient(
            responses={"sentiment": "POSITIVE"},
        )
        handler = NodeLlmEvalHarness(client=client)
        corpus = (
            ModelLlmEvalTask(
                task_id="sentiment_pos",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt="Classify the sentiment of this text.",
                expected_substrings=("POSITIVE",),
            ),
        )

        result = handler.handle(
            LlmEvalRequest(models=["m1"], corpus=corpus, max_tasks_per_type=1)
        )

        assert result.tasks_run == 1
        sample = result.samples[0]
        assert sample.model_key == "m1"
        assert sample.task_type == EnumLlmEvalTaskType.CLASSIFICATION
        assert sample.score == 1.0
        assert sample.substring_hits == 1
        assert sample.error == ""

    async def test_substring_scoring_partial_miss(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Partial substring hits yield fractional scores."""
        client = FakeLlmClient(responses={"contract": "name: x\nnode_type: compute\n"})
        handler = NodeLlmEvalHarness(client=client)
        corpus = (
            ModelLlmEvalTask(
                task_id="contract_partial",
                task_type=EnumLlmEvalTaskType.CONTRACT_YAML,
                prompt="Write a contract with name node_type contract_version.",
                expected_substrings=("name:", "node_type:", "contract_version:"),
            ),
        )

        result = handler.handle(LlmEvalRequest(models=["m1"], corpus=corpus))

        sample = result.samples[0]
        assert sample.score == pytest.approx(2 / 3, abs=1e-3)
        assert sample.substring_hits == 2

    async def test_error_path_recorded(self, event_bus: EventBusInmemory) -> None:
        """When the client raises, the sample records the error and scores zero."""
        handler = NodeLlmEvalHarness(client=_RaisingClient())
        corpus = (
            ModelLlmEvalTask(
                task_id="x",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt="anything",
                expected_substrings=("YES",),
            ),
        )

        result = handler.handle(LlmEvalRequest(models=["m1"], corpus=corpus))

        sample = result.samples[0]
        assert sample.score == 0.0
        assert "RuntimeError" in sample.error
        assert sample.output_chars == 0
        assert result.status == "error"

    async def test_summary_rollup(self, event_bus: EventBusInmemory) -> None:
        """Summary aggregates mean scores by (model, task_type)."""
        client = FakeLlmClient(
            responses={"alpha": "POSITIVE", "beta": "NEGATIVE"},
        )
        handler = NodeLlmEvalHarness(client=client)
        corpus = (
            ModelLlmEvalTask(
                task_id="t1",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt="alpha",
                expected_substrings=("POSITIVE",),
            ),
            ModelLlmEvalTask(
                task_id="t2",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt="beta",
                expected_substrings=("POSITIVE",),
            ),
        )

        result = handler.handle(LlmEvalRequest(models=["m1"], corpus=corpus))
        summary = result.summary

        assert "m1" in summary
        assert summary["m1"]["classification"] == 0.5

    async def test_task_type_filter(self, event_bus: EventBusInmemory) -> None:
        """task_types filter limits which tasks from the corpus run."""
        client = FakeLlmClient(responses={"keep": "POSITIVE"})
        handler = NodeLlmEvalHarness(client=client)
        corpus = (
            ModelLlmEvalTask(
                task_id="keep",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt="keep",
                expected_substrings=("POSITIVE",),
            ),
            ModelLlmEvalTask(
                task_id="drop",
                task_type=EnumLlmEvalTaskType.CONTRACT_YAML,
                prompt="drop",
                expected_substrings=("name:",),
            ),
        )

        result = handler.handle(
            LlmEvalRequest(
                models=["m1"],
                task_types=[EnumLlmEvalTaskType.CLASSIFICATION],
                corpus=corpus,
            )
        )

        assert result.tasks_run == 1
        assert result.samples[0].task_id == "keep"

    async def test_max_tasks_per_type_cap(self, event_bus: EventBusInmemory) -> None:
        """max_tasks_per_type caps task count for each (model, task_type)."""
        client = FakeLlmClient(responses={"x": "YES"})
        handler = NodeLlmEvalHarness(client=client)
        corpus = tuple(
            ModelLlmEvalTask(
                task_id=f"t{i}",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt=f"x{i}",
                expected_substrings=("YES",),
            )
            for i in range(5)
        )

        result = handler.handle(
            LlmEvalRequest(models=["m1"], corpus=corpus, max_tasks_per_type=2)
        )

        assert result.tasks_run == 2

    async def test_multi_model_samples(self, event_bus: EventBusInmemory) -> None:
        """Running multiple models produces one sample per (model, task)."""
        client = FakeLlmClient(responses={"x": "YES"})
        handler = NodeLlmEvalHarness(client=client)
        corpus = (
            ModelLlmEvalTask(
                task_id="t1",
                task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                prompt="x",
                expected_substrings=("YES",),
            ),
        )

        result = handler.handle(
            LlmEvalRequest(models=["m1", "m2", "m3"], corpus=corpus)
        )

        assert result.models_benchmarked == 3
        assert {s.model_key for s in result.samples} == {"m1", "m2", "m3"}

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and emit a completion event."""
        client = FakeLlmClient(responses={"ping": "POSITIVE"})
        handler = NodeLlmEvalHarness(client=client)
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            corpus = (
                ModelLlmEvalTask(
                    task_id="cmd_task",
                    task_type=EnumLlmEvalTaskType.CLASSIFICATION,
                    prompt="ping",
                    expected_substrings=("POSITIVE",),
                ),
            )
            request = LlmEvalRequest(
                models=payload["models"],
                corpus=corpus,
            )
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "tasks_run": result.tasks_run,
                "summary": result.summary,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC, key=None, value=json.dumps(result_payload).encode()
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-eval"
        )

        cmd_payload = json.dumps({"models": ["qwen3-coder-30b"]}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        captured = results_captured[0]
        assert captured["status"] == "clean"
        assert captured["tasks_run"] == 1

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_protocol_client_satisfied_by_fake(self) -> None:
        """FakeLlmClient structurally satisfies ProtocolLlmClient."""
        client: ProtocolLlmClient = FakeLlmClient(responses={"x": "y"})
        assert client.complete("any-model", "x") == "y"
