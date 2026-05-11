# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerLocalSupervisor — execute a pre-made routing decision.

Invokes the selected model endpoint, runs the verifier on the output,
and retries according to the retry budget and strategy supplied in the
request. Does NOT make routing decisions — that is
node_routing_policy_engine's responsibility.

Two-Strike protocol: attempt_count >= _TWO_STRIKE_THRESHOLD always
escalates to ESCALATE regardless of retry_strategy.

Topics are loaded from contract.yaml; none are hardcoded here.

Related:
    - OMN-8050: node_local_supervisor in omnimarket
    - OMN-8047: Overseer System Part 3 — Hierarchical Runners + Benchmarking
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_local_supervisor.models.model_local_supervisor_request import (
    EnumRetryStrategy,
    ModelLocalSupervisorRequest,
)
from omnimarket.nodes.node_local_supervisor.models.model_local_supervisor_result import (
    EnumSupervisorVerdict,
    ModelLocalSupervisorResult,
)

logger = logging.getLogger(__name__)

_TWO_STRIKE_THRESHOLD: int = 3

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_topics() -> tuple[str, str, str]:
    """Load subscribe and publish topics from contract.yaml. Fail loud if missing."""
    with open(_CONTRACT_PATH) as fh:
        data = yaml.safe_load(fh) or {}
    bus = data.get("event_bus", {}) or {}
    subscribe = (bus.get("subscribe_topics") or [])[0]
    publishes = bus.get("publish_topics") or []
    completed = next((t for t in publishes if "completed" in t), None)
    escalated = next((t for t in publishes if "escalated" in t), None)
    if not subscribe or not completed or not escalated:
        raise ValueError(f"Required topics not declared in {_CONTRACT_PATH}")
    return subscribe, completed, escalated


TOPIC_SUBSCRIBE, TOPIC_COMPLETED, TOPIC_ESCALATED = _load_topics()

ModelInvoker = Callable[[str, str, str], str]
VerifierFn = Callable[[str, str], bool]


def _default_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
    """Invoke a model endpoint via the OpenAI-compatible chat completions API.

    Extracted as a module-level function so tests can inject a replacement
    via HandlerLocalSupervisor(model_invoker=...) without patching httpx globally.
    Raises on HTTP errors or network failures.
    """
    import httpx

    payload = {
        "model": model_key,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.2,
    }
    chat_path = (
        "/chat/completions" if "/paas/v4" in endpoint_url else "/v1/chat/completions"
    )
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{endpoint_url}{chat_path}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["choices"][0]["message"]["content"])


def _default_verifier(output: str, prompt: str) -> bool:
    """Minimal structural verifier: output is non-empty and not a bare error string."""
    if not output or not output.strip():
        return False
    lowered = output.strip().lower()
    # Reject bare error responses
    if lowered.startswith("error:") or lowered.startswith("exception:"):
        return False
    return True


class HandlerLocalSupervisor:
    """Execute a pre-made routing decision with retry and verification.

    Accepts an optional model_invoker and verifier for testability — in
    production both default to the real HTTP call and structural verifier.
    """

    def __init__(
        self,
        *,
        model_invoker: ModelInvoker | None = None,
        verifier: VerifierFn | None = None,
        event_bus: Any,
    ) -> None:
        self._invoke = model_invoker if model_invoker is not None else _default_invoker
        self._verify = verifier if verifier is not None else _default_verifier
        self._event_bus = event_bus

    def handle(
        self, request: ModelLocalSupervisorRequest
    ) -> ModelLocalSupervisorResult:
        """Execute the routing decision, retrying per budget until verified or exhausted.

        Two-Strike protocol: once attempt_count reaches _TWO_STRIKE_THRESHOLD,
        the loop immediately escalates regardless of retry_strategy.
        """
        decision = request.routing_decision

        for attempt in range(1, request.retry_budget + 1):
            if attempt >= _TWO_STRIKE_THRESHOLD:
                logger.warning(
                    "Two-Strike threshold reached (attempt=%d, budget=%d, correlation_id=%s) — escalating",
                    attempt,
                    request.retry_budget,
                    request.correlation_id,
                )
                return ModelLocalSupervisorResult(
                    output="",
                    verdict=EnumSupervisorVerdict.ESCALATE,
                    attempt_count=attempt,
                    model_key=decision.model_key,
                    escalated=True,
                    correlation_id=request.correlation_id,
                )

            logger.info(
                "local_supervisor attempt %d/%d model=%s correlation_id=%s",
                attempt,
                request.retry_budget,
                decision.model_key,
                request.correlation_id,
            )

            try:
                output = self._invoke(
                    decision.endpoint_url,
                    decision.model_key,
                    request.prompt,
                )
            except Exception as exc:
                logger.warning(
                    "Model invocation failed (attempt=%d, model=%s, correlation_id=%s): %s",
                    attempt,
                    decision.model_key,
                    request.correlation_id,
                    exc,
                )
                continue

            if self._verify(output, request.prompt):
                logger.info(
                    "Verifier PASS (attempt=%d, model=%s, correlation_id=%s)",
                    attempt,
                    decision.model_key,
                    request.correlation_id,
                )
                return ModelLocalSupervisorResult(
                    output=output,
                    verdict=EnumSupervisorVerdict.PASS,
                    attempt_count=attempt,
                    model_key=decision.model_key,
                    escalated=False,
                    correlation_id=request.correlation_id,
                )

            logger.info(
                "Verifier FAIL (attempt=%d, strategy=%s, correlation_id=%s)",
                attempt,
                request.retry_strategy,
                request.correlation_id,
            )

            if request.retry_strategy == EnumRetryStrategy.TIER_ESCALATION:
                return ModelLocalSupervisorResult(
                    output="",
                    verdict=EnumSupervisorVerdict.ESCALATE,
                    attempt_count=attempt,
                    model_key=decision.model_key,
                    escalated=True,
                    correlation_id=request.correlation_id,
                )

        # Budget exhausted without a PASS
        logger.warning(
            "Retry budget exhausted (budget=%d, correlation_id=%s) — escalating",
            request.retry_budget,
            request.correlation_id,
        )
        return ModelLocalSupervisorResult(
            output="",
            verdict=EnumSupervisorVerdict.ESCALATE,
            attempt_count=request.retry_budget,
            model_key=decision.model_key,
            escalated=True,
            correlation_id=request.correlation_id,
        )


__all__: list[str] = [
    "TOPIC_COMPLETED",
    "TOPIC_ESCALATED",
    "TOPIC_SUBSCRIBE",
    "HandlerLocalSupervisor",
]
