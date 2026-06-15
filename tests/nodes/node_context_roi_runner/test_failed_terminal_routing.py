# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13038: runner must receive FAILED generation terminals, not just completed.

FIFTH defect on ``node_context_roi_runner`` (sibling OMN-13003/13005/13010/13012).

Root cause: ``node_generation_consumer`` routes a benchmark whose final contract
validation failed (``contract_passed=False``) to
``onex.evt.omnimarket.node-generation-failed.v1`` (``_emit_benchmark``), while the
runner's contract declares only the COMPLETED topic as
``generation_pipeline.terminal_event_topic`` and ``_run_trial`` opens exactly one
terminal session on it. A failed terminal — emitted with full telemetry
(attempt_count, tokens, model/provider, contract_passed=False) — is therefore
never delivered to the runner, which blocks the full per-arm timeout and records
``failure_stage=GENERATION, attempt_count=0``: indistinguishable from
"generation never ran". OFF-arm failures are the experiment's signal, so every
such row is corrupted.

Fix under test: the runner contract declares BOTH terminal topics
(``generation_pipeline.terminal_event_topic`` +
``generation_pipeline.terminal_failed_event_topic``); ``_run_trial`` opens one
two-phase session per terminal topic BEFORE publishing (subscribe-before-publish,
OMN-13012) and races the correlated waits across both topics within the single
per-arm timeout. ``_extract_row`` already distinguishes outcomes from the payload
(``contract_passed=False`` -> ``failure_stage=VALIDATION`` with the real
``attempt_count``); the emitter is untouched.

These tests drive the public entry points the runtime uses (``handle`` /
``handle_async``) with per-topic fakes — no private-method shortcuts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.context_roi import EnumFailureStage
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)

_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)

_GENERATION_CONSUMER_CONTRACT_PATH = (
    _CONTRACT_PATH.parent.parent / "node_generation_consumer" / "contract.yaml"
)

# Terminal payload as node_generation_consumer emits it on the FAILED topic:
# real telemetry, contract_passed=False (see _emit_benchmark — same
# ModelGenerationBenchmark serialization on both topics).
_FAILED_EVENT: dict[str, Any] = {
    "attempt_count": 2,
    "contract_passed": False,
    "first_pass_success": False,
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "cost_inference_usd": 0.001,
    "model_id": "Qwen3.6-35B-A3B",
    "provider": "local",
    "endpoint_class": "local-coder",
}

_COMPLETED_EVENT: dict[str, Any] = {
    **_FAILED_EVENT,
    "attempt_count": 1,
    "contract_passed": True,
    "first_pass_success": True,
}


def _make_request(timeout_seconds: float = 5.0) -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-omn13038",
        tasks=(
            ModelContextRoiTask(
                task_id="invoice_reconcile",
                task_description="Generate a reconciliation compute node.",
            ),
        ),
        arms=(ModelContextRoiArmSpec(label="off", factor_subset=()),),
        trials_per_cell=1,
        max_attempts=2,
        arm_order_seed=42,
        generation_timeout_seconds=timeout_seconds,
    )


class _FakeTerminalSession:
    """Per-topic two-phase session fake.

    ``payload=None`` models a topic on which no correlated terminal arrives:
    ``wait`` blocks until ``close()`` (or the caller's timeout fake-out via the
    short ``block_max_seconds``) and returns None — mirroring the real session's
    blocking correlate.
    """

    def __init__(
        self, payload: dict[str, Any] | None, block_max_seconds: float = 2.0
    ) -> None:
        self._payload = payload
        self._block_max_seconds = block_max_seconds
        self._closed_event = threading.Event()
        self.calls: list[str] = []
        self.closed = False

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        self.calls.append("wait")
        if self._payload is None:
            # No terminal on this topic: block until close() or a bounded stand-in
            # for the caller's timeout, then report a genuine timeout (None).
            self._closed_event.wait(min(timeout_seconds, self._block_max_seconds))
            return None
        return {**self._payload, "correlation_id": correlation_id}

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        self._closed_event.set()


class _FakePerTopicTwoPhaseConsumer:
    """Two-phase consumer fake delivering a configured payload per topic."""

    def __init__(self, payload_by_topic_substr: dict[str, dict[str, Any] | None]):
        self._payload_by_topic_substr = payload_by_topic_substr
        self.sessions: dict[str, _FakeTerminalSession] = {}
        self.event_log: list[tuple[str, str]] = []  # ("open"|..., topic)

    def _payload_for(self, topic: str) -> dict[str, Any] | None:
        for substr, payload in self._payload_by_topic_substr.items():
            if substr in topic:
                return payload
        raise AssertionError(f"unexpected terminal topic opened: {topic}")

    def open(self, terminal_topic: str) -> _FakeTerminalSession:
        self.event_log.append(("open", terminal_topic))
        session = _FakeTerminalSession(self._payload_for(terminal_topic))
        self.sessions[terminal_topic] = session
        return session

    def __call__(
        self, terminal_topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        raise AssertionError("two-phase consumer must be used via open().wait()")


def _make_handler(
    consumer: object,
    event_log: list[tuple[str, str]] | None = None,
) -> HandlerContextRoiRunner:
    log = event_log if event_log is not None else []

    def _publisher(topic: str, payload: bytes) -> None:
        log.append(("publish", topic))

    return HandlerContextRoiRunner(
        event_publisher=_publisher,
        event_consumer=consumer,  # type: ignore[arg-type]
        runner_contract_path=_CONTRACT_PATH,
    )


# ---------------------------------------------------------------------------
# Contract declarations
# ---------------------------------------------------------------------------


def test_contract_declares_failed_terminal_topic() -> None:
    """The runner contract must declare the failed terminal topic so the handler
    never hardcodes it."""
    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)
    gen_pipeline = contract["generation_pipeline"]
    failed_topic = gen_pipeline.get("terminal_failed_event_topic", "")
    assert "generation-failed" in failed_topic, (
        "generation_pipeline.terminal_failed_event_topic missing from the runner "
        "contract — failed generation terminals are unreachable (OMN-13038)"
    )


def test_failed_terminal_topic_matches_emitter_publish_side() -> None:
    """Both terminal topics must be contract-declared on the emitter's publish
    side (node_generation_consumer) — no orphan subscription."""
    with open(_CONTRACT_PATH) as f:
        runner_contract = yaml.safe_load(f)
    with open(_GENERATION_CONSUMER_CONTRACT_PATH) as f:
        emitter_contract = yaml.safe_load(f)
    emitter_topics = emitter_contract["event_bus"]["publish_topics"]
    gen_pipeline = runner_contract["generation_pipeline"]
    assert gen_pipeline["terminal_event_topic"] in emitter_topics
    assert gen_pipeline["terminal_failed_event_topic"] in emitter_topics


def test_handler_initialises_failed_topic_from_contract() -> None:
    handler = _make_handler(_FakePerTopicTwoPhaseConsumer({"": None}))
    assert "generation-failed" in handler._gen_terminal_failed_topic


def test_handler_fails_fast_when_failed_topic_missing(tmp_path: Path) -> None:
    """A contract without the failed terminal topic must be rejected at
    construction — no silent single-topic fallback."""
    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)
    del contract["generation_pipeline"]["terminal_failed_event_topic"]
    stripped = tmp_path / "contract.yaml"
    stripped.write_text(yaml.safe_dump(contract))
    with pytest.raises(ValueError, match="terminal_failed_event_topic"):
        HandlerContextRoiRunner(runner_contract_path=stripped)


def test_no_hardcoded_failed_topic_literal_in_handler() -> None:
    import inspect

    from omnimarket.nodes.node_context_roi_runner.handlers import (
        handler_context_roi_runner,
    )

    source = inspect.getsource(handler_context_roi_runner)
    assert "onex.evt.omnimarket.node-generation-failed.v1" not in source


# ---------------------------------------------------------------------------
# THE defect: failed terminal must be consumed, not timed out
# ---------------------------------------------------------------------------


def test_failed_terminal_recorded_as_validation_failure_not_timeout() -> None:
    """A contract_passed=False terminal delivered on the FAILED topic must
    produce failure_stage=VALIDATION with the real attempt_count — NOT the
    failure_stage=GENERATION / attempt_count=0 timeout signature (OMN-13038)."""
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": None,  # nothing arrives on completed
            "generation-failed": _FAILED_EVENT,
        }
    )
    handler = _make_handler(consumer)

    result = handler.handle(_make_request())

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.VALIDATION, (
        f"failed terminal was not consumed: failure_stage={row.failure_stage} "
        "(GENERATION here means the runner timed out instead of reading the "
        "failed.v1 terminal)"
    )
    assert row.attempt_count == 2
    assert row.final_success is False
    assert row.first_pass_success is False
    assert row.model_id == "Qwen3.6-35B-A3B"
    assert row.provider == "local"


def test_completed_terminal_still_consumed() -> None:
    """Regression: the completed path must keep working when the failed topic
    stays silent."""
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": _COMPLETED_EVENT,
            "generation-failed": None,
        }
    )
    handler = _make_handler(consumer)

    result = handler.handle(_make_request())

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.NONE
    assert row.attempt_count == 1
    assert row.final_success is True


def test_no_terminal_on_either_topic_records_generation_failure() -> None:
    """Genuine timeout (silence on BOTH topics) keeps the GENERATION signature."""
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": None,
            "generation-failed": None,
        }
    )
    handler = _make_handler(consumer)

    result = handler.handle(_make_request(timeout_seconds=1.0))

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.GENERATION
    assert row.attempt_count == 0


def test_both_terminal_sessions_open_before_publish() -> None:
    """Subscribe-before-publish (OMN-13012) must hold for BOTH terminal topics:
    each session is positioned before the generation command goes out."""
    event_log: list[tuple[str, str]] = []
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": _COMPLETED_EVENT,
            "generation-failed": None,
        }
    )
    # Share one event log between opens and publishes.
    consumer.event_log = event_log
    handler = _make_handler(consumer, event_log=event_log)

    handler.handle(_make_request())

    open_topics = [topic for kind, topic in event_log if kind == "open"]
    publish_index = next(
        i for i, (kind, _) in enumerate(event_log) if kind == "publish"
    )
    open_indices = [i for i, (kind, _) in enumerate(event_log) if kind == "open"]
    assert any("generation-completed" in t for t in open_topics)
    assert any("generation-failed" in t for t in open_topics), (
        "failed-terminal session was never opened — failed terminals are "
        "unreachable (OMN-13038)"
    )
    assert all(i < publish_index for i in open_indices), (
        "a terminal session was opened AFTER the command publish — "
        "subscribe-after-publish race (OMN-13012) reintroduced"
    )


def test_all_terminal_sessions_closed_after_trial() -> None:
    """Winner AND loser sessions must both be closed after the trial — no
    leaked ephemeral consumers."""
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": None,
            "generation-failed": _FAILED_EVENT,
        }
    )
    handler = _make_handler(consumer)

    handler.handle(_make_request())

    assert len(consumer.sessions) == 2
    for topic, session in consumer.sessions.items():
        assert session.closed is True, f"session for {topic} leaked (never closed)"


def test_publish_failure_closes_all_opened_sessions() -> None:
    """If the command publish raises, every pre-opened session must be closed."""
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": None,
            "generation-failed": None,
        }
    )

    def _exploding_publisher(topic: str, payload: bytes) -> None:
        raise RuntimeError("broker down")

    handler = HandlerContextRoiRunner(
        event_publisher=_exploding_publisher,
        event_consumer=consumer,  # type: ignore[arg-type]
        runner_contract_path=_CONTRACT_PATH,
    )

    result = handler.handle(_make_request())

    assert result.rows[0].failure_stage == EnumFailureStage.GENERATION
    for topic, session in consumer.sessions.items():
        assert session.closed is True, (
            f"session for {topic} leaked after publish failure"
        )


# ---------------------------------------------------------------------------
# Legacy single-call consumer fallback
# ---------------------------------------------------------------------------


def test_legacy_single_call_consumer_polls_failed_topic_too() -> None:
    """A legacy consumer (no .open()) must also be raced across both terminal
    topics so failed terminals stay reachable on the fallback path."""
    seen_topics: list[str] = []

    def _legacy_consumer(
        topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        seen_topics.append(topic)
        if "generation-failed" in topic:
            return {**_FAILED_EVENT, "correlation_id": correlation_id}
        return None

    handler = _make_handler(_legacy_consumer)

    result = handler.handle(_make_request(timeout_seconds=1.0))

    row = result.rows[0]
    assert any("generation-failed" in t for t in seen_topics), (
        "legacy fallback never polled the failed terminal topic (OMN-13038)"
    )
    assert row.failure_stage == EnumFailureStage.VALIDATION
    assert row.attempt_count == 2


# ---------------------------------------------------------------------------
# Real dispatch entry point (handle_async — what the runtime invokes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_consumes_failed_terminal() -> None:
    """The runtime dispatch entry point (handle_async) must surface failed
    terminals identically to handle()."""
    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": None,
            "generation-failed": _FAILED_EVENT,
        }
    )
    handler = _make_handler(consumer)

    result = await handler.handle_async(_make_request())

    row = result.rows[0]
    assert row.failure_stage == EnumFailureStage.VALIDATION
    assert row.attempt_count == 2
    assert row.final_success is False


# ---------------------------------------------------------------------------
# Command payload unchanged (emitter contract untouched)
# ---------------------------------------------------------------------------


def test_generation_command_payload_shape_unchanged() -> None:
    """The fix is consume-side only: the published generation command keeps its
    existing shape (the emitter and its contract are untouched)."""
    published: list[tuple[str, bytes]] = []

    def _publisher(topic: str, payload: bytes) -> None:
        published.append((topic, payload))

    consumer = _FakePerTopicTwoPhaseConsumer(
        {
            "generation-completed": _COMPLETED_EVENT,
            "generation-failed": None,
        }
    )
    handler = HandlerContextRoiRunner(
        event_publisher=_publisher,
        event_consumer=consumer,  # type: ignore[arg-type]
        runner_contract_path=_CONTRACT_PATH,
    )

    handler.handle(_make_request())

    commands = [
        (topic, raw) for topic, raw in published if "node-generation-requested" in topic
    ]
    assert len(commands) == 1
    _topic, raw = commands[0]
    payload = json.loads(raw.decode("utf-8"))
    assert set(payload.keys()) == {
        "task_description",
        "correlation_id",
        "max_attempts",
        "context_pack",
        "context_artifacts",
        "context_pack_hash",
    }
