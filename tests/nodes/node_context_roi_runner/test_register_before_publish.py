# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13118: the runner must register the correlation_id BEFORE publishing.

The injected long-lived correlator (omnibase_infra ``TerminalEventConsumer``) runs
an always-on poll loop; its per-correlation future must exist BEFORE the
generation command is published, or the loop reads the correlated terminal and
drops it UNMATCHED (``pending_keys=[]``) ~28s before the post-publish wait would
register the cid. That is the strike-six wedge traced live in
``docs/evidence/2026-06-12-weekend-pass/experiments/probe4-stability/diagnostic-rebuild8/FINDINGS.md``:
the COMPLETED terminal IS read by ``getone`` and is dropped solely because the cid
is not yet registered. ``open()`` (Step 2a) and ``wait()`` (Step 3) were wired but
``register()`` was folded into ``wait()`` (post-publish); the fix hoists
``register()`` to before publish, per the two-phase
``open -> register -> publish -> wait`` contract.

Acceptance for the consume-leg itself is the LIVE K>=10 stability-lane reprobe,
NOT this unit test (six prior fixes passed unit repros and failed live). This test
guards only the register-vs-publish ordering invariant the live trace pinned down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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

_COMPLETED_EVENT: dict[str, Any] = {
    "attempt_count": 1,
    "contract_passed": True,
    "first_pass_success": True,
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "cost_inference_usd": 0.001,
    "model_id": "Qwen3.6-35B-A3B",
    "provider": "local",
    "endpoint_class": "local-coder",
}


def _make_request(timeout_seconds: float = 5.0) -> ModelContextRoiRunRequest:
    return ModelContextRoiRunRequest(
        run_id="run-omn13118",
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


class _RecordingSession:
    """Two-phase session fake recording register/wait/close to a shared log.

    Unlike the legacy ``_FakeTerminalSession`` (no ``register``), this fake models
    the real runtime ``TerminalConsumerSession`` which exposes ``register`` — so
    the runner's duck-typed pre-publish ``register`` is exercised here.
    """

    def __init__(
        self, topic: str, payload: dict[str, Any] | None, log: list[tuple[str, str]]
    ) -> None:
        self._topic = topic
        self._payload = payload
        self._log = log
        self.closed = False

    def register(self, correlation_id: str) -> None:
        self._log.append(("register", self._topic))

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        self._log.append(("wait", self._topic))
        if self._payload is None:
            return None
        return {**self._payload, "correlation_id": correlation_id}

    def close(self) -> None:
        self.closed = True


class _RecordingConsumer:
    """Two-phase consumer fake logging opens and delivering a payload per topic."""

    def __init__(
        self,
        payload_by_topic_substr: dict[str, dict[str, Any] | None],
        log: list[tuple[str, str]],
    ) -> None:
        self._payloads = payload_by_topic_substr
        self._log = log
        self.sessions: dict[str, _RecordingSession] = {}

    def _payload_for(self, topic: str) -> dict[str, Any] | None:
        for substr, payload in self._payloads.items():
            if substr in topic:
                return payload
        raise AssertionError(f"unexpected terminal topic: {topic}")

    def open(self, terminal_topic: str) -> _RecordingSession:
        self._log.append(("open", terminal_topic))
        session = _RecordingSession(
            terminal_topic, self._payload_for(terminal_topic), self._log
        )
        self.sessions[terminal_topic] = session
        return session

    def __call__(
        self, terminal_topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        raise AssertionError("two-phase consumer must be used via open()")


def test_register_called_before_publish_on_both_terminal_sessions() -> None:
    """register(cid) must run on BOTH terminal sessions before the command publish.

    This is the ordering invariant the live diagnostic pinned: the always-on
    correlator poll loop consumes the terminal the instant generation completes,
    so the cid must be in the pending registry before publish or the terminal is
    UNMATCHED-dropped.
    """
    log: list[tuple[str, str]] = []
    consumer = _RecordingConsumer(
        {"generation-completed": _COMPLETED_EVENT, "generation-failed": None}, log
    )

    def _publisher(topic: str, payload: bytes) -> None:
        log.append(("publish", topic))

    handler = HandlerContextRoiRunner(
        event_publisher=_publisher,
        event_consumer=consumer,  # type: ignore[arg-type]
        runner_contract_path=_CONTRACT_PATH,
    )

    result = handler.handle(_make_request())

    publish_index = next(i for i, (kind, _) in enumerate(log) if kind == "publish")
    register_indices = [i for i, (kind, _) in enumerate(log) if kind == "register"]
    registered_topics = [topic for kind, topic in log if kind == "register"]

    assert register_indices, (
        "register() was never called — the cid is not in the correlator registry "
        "before publish, so the terminal is UNMATCHED-dropped (OMN-13118 wedge)"
    )
    assert all(i < publish_index for i in register_indices), (
        "register() ran AFTER the command publish — the always-on correlator poll "
        "loop reads and UNMATCHED-drops the terminal before the cid is registered "
        "(the strike-six register-after-publish race)"
    )
    assert any("generation-completed" in t for t in registered_topics)
    assert any("generation-failed" in t for t in registered_topics), (
        "the FAILED terminal session was not pre-registered — an OFF-arm failure "
        "terminal would be dropped the same way"
    )
    # Happy path intact: pre-registering does not break terminal delivery.
    assert result.rows[0].failure_stage == EnumFailureStage.NONE
    assert result.rows[0].final_success is True
