# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WS-5 Wave 3 — multi-parameter integration coverage for node_pr_watch_orchestrator.

This is the real backing node for the ``pr_watch`` market skill. (The scout
report named ``node_github_pr_watcher_effect``, which does not exist in this
repo; ``node_pr_watch_orchestrator`` is the resolvable backing node.)

ORCHESTRATOR (Variant B): the handler is registered against the in-memory
``integration_event_bus`` and self-publishes its terminal result via
``_publish_result``. The gh-CLI boundary is the injected
``ProtocolPrChecksClient`` collaborator (``_MockChecksClient``) — never
subprocess monkeypatch. Each parametrized case asserts BOTH the TYPED
``ModelPrWatchOrchestratorResult`` (status / conclusion / failed/pending check
names) AND the envelope published on the contract-selected terminal topic
(``get_event_history``).

Negative controls: a red check yields FAILED/RED with the failing check name;
an empty rollup yields FAILED/RED; a checks-client error yields FAILED/RED with
an error_message.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_watch_orchestrator.handlers.handler_pr_watch_orchestrator import (
    HandlerPrWatchOrchestrator,
    PrChecksClientError,
    ProtocolPrChecksClient,  # noqa: F401
)
from omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_check_status import (
    EnumPrCheckBucket,
    ModelPrCheckStatus,
)
from omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_watch_orchestrator_request import (
    ModelPrWatchOrchestratorRequest,
)
from omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_watch_orchestrator_result import (
    EnumPrWatchConclusion,
    EnumPrWatchStatus,
    ModelPrWatchOrchestratorResult,
)

REPO = "OmniNode-ai/omnimarket"


def _check(name: str, bucket: EnumPrCheckBucket) -> ModelPrCheckStatus:
    return ModelPrCheckStatus(name=name, bucket=bucket, state=bucket.value)


class _MockChecksClient:
    """Injected ProtocolPrChecksClient — returns a fixed snapshot or raises."""

    def __init__(
        self,
        snapshot: tuple[ModelPrCheckStatus, ...] = (),
        *,
        raises: bool = False,
    ) -> None:
        self._snapshot = snapshot
        self._raises = raises

    def fetch_checks(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> tuple[ModelPrCheckStatus, ...]:
        if self._raises:
            raise PrChecksClientError("gh pr checks failed: simulated transport error")
        return self._snapshot


@pytest.mark.integration
@pytest.mark.parametrize(
    ("snapshot", "raises", "timeout_seconds", "expect"),
    [
        pytest.param(
            (
                _check("pytest", EnumPrCheckBucket.PASS),
                _check("mypy", EnumPrCheckBucket.SKIPPING),
            ),
            False,
            30.0,
            {
                "status": EnumPrWatchStatus.COMPLETED,
                "conclusion": EnumPrWatchConclusion.GREEN,
                "failed": (),
                "pending": (),
                "has_error": False,
            },
            id="all-green-completed",
        ),
        # NEGATIVE CONTROL: a fail-bucket check -> FAILED / RED with its name.
        pytest.param(
            (
                _check("pytest", EnumPrCheckBucket.PASS),
                _check("deploy-gate", EnumPrCheckBucket.FAIL),
            ),
            False,
            30.0,
            {
                "status": EnumPrWatchStatus.FAILED,
                "conclusion": EnumPrWatchConclusion.RED,
                "failed": ("deploy-gate",),
                "pending": (),
                "has_error": True,
            },
            id="red-failed",
        ),
        # Pending check with a 0s budget -> TIMEOUT (no real sleep).
        pytest.param(
            (_check("slow-job", EnumPrCheckBucket.PENDING),),
            False,
            0.0,
            {
                "status": EnumPrWatchStatus.TIMEOUT,
                "conclusion": EnumPrWatchConclusion.TIMEOUT,
                "failed": (),
                "pending": ("slow-job",),
                "has_error": True,
            },
            id="pending-timeout",
        ),
        # NEGATIVE CONTROL: empty rollup classifies RED (no checks != green).
        pytest.param(
            (),
            False,
            30.0,
            {
                "status": EnumPrWatchStatus.FAILED,
                "conclusion": EnumPrWatchConclusion.RED,
                "failed": (),
                "pending": (),
                "has_error": True,
            },
            id="empty-rollup-red",
        ),
        # NEGATIVE CONTROL: checks-client error -> FAILED / RED with message.
        pytest.param(
            (),
            True,
            30.0,
            {
                "status": EnumPrWatchStatus.FAILED,
                "conclusion": EnumPrWatchConclusion.RED,
                "failed": (),
                "pending": (),
                "has_error": True,
            },
            id="client-error-failed",
        ),
    ],
)
async def test_pr_watch_orchestrator_multiparam(
    integration_event_bus,
    snapshot: tuple[ModelPrCheckStatus, ...],
    raises: bool,
    timeout_seconds: float,
    expect: dict[str, object],
) -> None:
    await integration_event_bus.start()
    try:
        correlation_id = uuid4()
        handler = HandlerPrWatchOrchestrator(
            event_bus=integration_event_bus,
            checks_client=_MockChecksClient(snapshot, raises=raises),
        )
        request = ModelPrWatchOrchestratorRequest(
            repo=REPO,
            pr_number=909,
            correlation_id=correlation_id,
            poll_interval_seconds=0.0,
            timeout_seconds=timeout_seconds,
        )

        result = await handler.handle(request)

        # Typed result assertions.
        assert isinstance(result, ModelPrWatchOrchestratorResult)
        assert result.correlation_id == correlation_id
        assert result.pr_number == 909
        assert result.status == expect["status"]
        assert result.conclusion == expect["conclusion"]
        assert result.failed_checks == expect["failed"]
        assert result.pending_checks == expect["pending"]
        assert (result.error_message != "") is expect["has_error"]
        assert result.attempts >= 1

        # Terminal event landed on the contract-selected topic.
        history = await integration_event_bus.get_event_history(
            topic=result.terminal_event
        )
        assert len(history) == 1, (
            f"expected exactly 1 terminal event on {result.terminal_event}"
        )
        envelope = json.loads(history[0].value)
        payload = envelope["payload"]
        assert payload["correlation_id"] == str(correlation_id)
        assert payload["status"] == expect["status"].value
        assert payload["conclusion"] == expect["conclusion"].value
    finally:
        await integration_event_bus.close()
