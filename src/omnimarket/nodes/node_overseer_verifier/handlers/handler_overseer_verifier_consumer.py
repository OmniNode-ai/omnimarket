# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer wrapper for node_overseer_verifier.

Subscribes to onex.cmd.omnimarket.overseer-verify.v1, runs the
deterministic 5-check gate via HandlerOverseerVerifier, and publishes
the result to onex.evt.omnimarket.overseer-verifier-completed.v1.

This handler bridges the event-bus wire format to the pure-Python verifier.
It is the component that makes node_overseer_verifier a live Kafka gate rather
than just an in-process utility.

Wire format for the inbound command:
    {
        "correlation_id": "<uuid>",        # required — used to correlate response
        "task_id":        "<str>",          # required
        "status":         "<str>",          # required
        "domain":         "<str>",          # required
        "node_id":        "<str>",          # required
        "runner_id":      "<str|null>",     # optional
        "attempt":        <int>,            # optional, default 1
        "payload":        <dict>,           # optional, default {}
        "error":          "<str|null>",     # optional
        "confidence":     <float|null>,     # optional
        "cost_so_far":    <float|null>,     # optional
        "allowed_actions": [<str>, ...],    # optional, default []
        "declared_invariants": [<str>, ...],# optional, default []
        "schema_version": "<str>"           # optional, default "1.0"
    }

Wire format for the outbound completion event:
    {
        "correlation_id":  "<uuid>",
        "passed":          <bool>,          # True iff verdict == "PASS"
        "verdict":         "PASS|FAIL|ESCALATE",
        "failure_class":   "<str|null>",
        "summary":         "<str>",
        "checks":          [...],           # per-check detail list
        "failed_criteria": [<str>, ...]     # names of failed checks (empty on PASS)
    }

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

TOPIC_OVERSEER_VERIFIER_COMPLETED = "onex.evt.omnimarket.overseer-verifier-completed.v1"
TOPIC_OVERSEER_VERIFY = "onex.cmd.omnimarket.overseer-verify.v1"

logger = logging.getLogger(__name__)


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    """Load the node's contract.yaml."""
    _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _topic_verification_receipt_start() -> str:
    """Load the verification-receipt-start topic from contract.yaml at import time."""
    publish_topics: list[str] = (
        _load_contract().get("event_bus", {}).get("publish_topics", [])
    )
    return next((t for t in publish_topics if "verification-receipt-start" in t), "")


TOPIC_VERIFICATION_RECEIPT_START = _topic_verification_receipt_start()

# Re-export canonical topic names for callers that import from this module.
TOPIC_SUBSCRIBE = TOPIC_OVERSEER_VERIFY
TOPIC_PUBLISH = TOPIC_OVERSEER_VERIFIER_COMPLETED


class HandlerOverseerVerifierConsumer:
    """Event-bus consumer wrapper for the overseer verifier.

    Processes inbound verify commands, runs HandlerOverseerVerifier, and
    publishes the completion event. Designed to run inside the ONEX runtime
    or any event bus infrastructure.

    Also publishes verification-receipt-start.v1 when an inbound command
    carries a task_id, enabling node_verification_receipt_generator to produce
    formal evidence receipts alongside the deterministic verification.

    Usage (standalone / testing)::

        consumer = HandlerOverseerVerifierConsumer()
        result_bytes = consumer.process(raw_message_bytes)
        # result_bytes is the serialised completion event payload

    In production the runtime wires subscribe/publish automatically via
    the contract.yaml topic declarations.
    """

    def __init__(self, event_bus: ProtocolEventBusPublisher | None = None) -> None:
        self._verifier = HandlerOverseerVerifier()
        self._event_bus = event_bus

    def process(self, raw: bytes) -> bytes:
        """Process a raw verify-command message and return the completion event bytes.

        Args:
            raw: JSON-encoded verify command bytes.

        Returns:
            JSON-encoded completion event bytes ready to publish on TOPIC_PUBLISH.
        """
        try:
            data: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("[OVERSEER-CONSUMER] Failed to decode command: %s", exc)
            return self._error_response(
                correlation_id="unknown",
                summary=f"Malformed command payload: {exc}",
            )

        correlation_id = str(data.get("correlation_id", ""))
        if not correlation_id:
            logger.warning(
                "[OVERSEER-CONSUMER] Missing correlation_id — cannot correlate response"
            )

        task_id_raw = data.get("task_id")
        if task_id_raw is not None:
            self._publish_verification_receipt_start_sync(
                task_id=str(task_id_raw),
                correlation_id=correlation_id,
                claim=str(data.get("status", "")),
                repo=data.get("repo"),
                pr_number=data.get("pr_number"),
            )

        try:
            request = ModelVerifierRequest(
                task_id=str(data.get("task_id", "")),
                status=str(data.get("status", "")),
                domain=str(data.get("domain", "")),
                node_id=str(data.get("node_id", "")),
                runner_id=data.get("runner_id"),
                attempt=int(data.get("attempt", 1)),
                payload=dict(data.get("payload") or {}),
                error=data.get("error"),
                confidence=_float_or_none(data.get("confidence")),
                cost_so_far=_float_or_none(data.get("cost_so_far")),
                allowed_actions=list(data.get("allowed_actions") or []),
                declared_invariants=list(data.get("declared_invariants") or []),
                schema_version=str(data.get("schema_version", "1.0")),
            )
        except Exception as exc:
            logger.error(
                "[OVERSEER-CONSUMER] Invalid request fields (correlation_id=%s): %s",
                correlation_id,
                exc,
            )
            return self._error_response(
                correlation_id=correlation_id,
                summary=f"Request validation error: {exc}",
            )

        result = self._verifier.verify(request)

        verdict = str(result.get("verdict", "FAIL"))
        raw_checks = result.get("checks", [])
        checks: list[dict[str, Any]] = (
            list(raw_checks) if isinstance(raw_checks, list) else []
        )
        failed_criteria = [
            str(c.get("name", "")) for c in checks if not c.get("passed", True)
        ]

        response: dict[str, Any] = {
            "correlation_id": correlation_id,
            "passed": verdict == "PASS",
            "verdict": verdict,
            "failure_class": result.get("failure_class"),
            "summary": str(result.get("summary", "")),
            "checks": checks,
            "failed_criteria": failed_criteria,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

        logger.info(
            "[OVERSEER-CONSUMER] verdict=%s correlation_id=%s failed_criteria=%s",
            verdict,
            correlation_id,
            failed_criteria,
        )

        return json.dumps(response).encode()

    def _publish_verification_receipt_start_sync(
        self,
        *,
        task_id: str,
        correlation_id: str,
        claim: str,
        repo: Any,
        pr_number: Any,
    ) -> None:
        """Fire-and-forget publish of verification-receipt-start.v1.

        Must be called from within a running event loop (the ONEX runtime
        always provides one). Schedules the publish as a task so the sync
        process() method returns immediately. If no event loop is running
        the publish is silently skipped and a warning is emitted — callers
        in pure sync contexts should use the async path directly.
        """
        if self._event_bus is None:
            return
        import asyncio

        payload: dict[str, Any] = {
            "task_id": task_id,
            "claim": claim,
            "correlation_id": correlation_id,
        }
        if repo is not None:
            payload["repo"] = str(repo)
        if pr_number is not None:
            with contextlib.suppress(TypeError, ValueError):
                payload["pr_number"] = int(pr_number)

        encoded = json.dumps(payload).encode()

        async def _publish() -> None:
            await self._event_bus.publish(  # type: ignore[union-attr]
                topic=TOPIC_VERIFICATION_RECEIPT_START,
                key=None,
                value=encoded,
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "[OVERSEER-CONSUMER] No running event loop — skipping "
                "verification-receipt-start publish for task_id=%s",
                task_id,
            )
            return

        _task = loop.create_task(_publish())
        # Retain reference to prevent GC before the coroutine completes.
        _task.add_done_callback(lambda _: None)

    def _error_response(self, *, correlation_id: str, summary: str) -> bytes:
        """Return a FAIL completion event for error cases."""
        response: dict[str, Any] = {
            "correlation_id": correlation_id,
            "passed": False,
            "verdict": "FAIL",
            "failure_class": "DATA_INTEGRITY",
            "summary": summary,
            "checks": [],
            "failed_criteria": ["consumer_error"],
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        return json.dumps(response).encode()


def _float_or_none(value: Any) -> float | None:
    """Coerce value to float or return None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__: list[str] = [
    "TOPIC_PUBLISH",
    "TOPIC_SUBSCRIBE",
    "HandlerOverseerVerifierConsumer",
]
