from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

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

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_GH_JSON_FIELDS = "name,state,bucket,workflow,link,startedAt,completedAt"
_PENDING_BUCKETS = frozenset({EnumPrCheckBucket.PENDING, EnumPrCheckBucket.UNKNOWN})
_GREEN_BUCKETS = frozenset({EnumPrCheckBucket.PASS, EnumPrCheckBucket.SKIPPING})
_RED_BUCKETS = frozenset({EnumPrCheckBucket.FAIL, EnumPrCheckBucket.CANCEL})


class PrChecksClientError(RuntimeError):
    """Raised when the gh checks effect boundary cannot return a valid snapshot."""


class ProtocolPrChecksClient(Protocol):
    """Typed effect boundary for reading GitHub PR check state."""

    def fetch_checks(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> tuple[ModelPrCheckStatus, ...]: ...


class GitHubCliPrChecksClient:
    """Read PR checks through the GitHub CLI."""

    def fetch_checks(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> tuple[ModelPrCheckStatus, ...]:
        command = [
            "gh",
            "pr",
            "checks",
            str(request.pr_number),
            "--repo",
            request.repo,
            "--json",
            _GH_JSON_FIELDS,
        ]
        if request.required_only:
            command.append("--required")

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise PrChecksClientError("gh CLI is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise PrChecksClientError("gh pr checks command timed out") from exc

        stdout = completed.stdout.strip()
        if completed.returncode not in {0, 8}:
            stderr = completed.stderr.strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise PrChecksClientError(f"gh pr checks failed: {detail}")
        if not stdout:
            raise PrChecksClientError("gh pr checks returned no JSON output")

        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise PrChecksClientError("gh pr checks returned invalid JSON") from exc
        if not isinstance(raw, list):
            raise PrChecksClientError("gh pr checks JSON must be a list")
        return tuple(_parse_check(row) for row in raw)


class HandlerPrWatchOrchestrator:
    """Orchestrator that polls GitHub PR checks until a terminal outcome."""

    def __init__(
        self,
        event_bus: Any | None = None,
        checks_client: ProtocolPrChecksClient | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._checks_client = checks_client or GitHubCliPrChecksClient()
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._contract = _load_contract()
        self._completed_topic = _topic_containing(self._contract, "completed")
        self._failed_topic = _topic_containing(self._contract, "failed")
        descriptor = cast(dict[str, Any], self._contract.get("descriptor") or {})
        self._contract_timeout_seconds = (
            float(descriptor.get("timeout_ms") or 0) / 1000.0
        )

    async def handle(
        self, request: ModelPrWatchOrchestratorRequest | dict[str, object]
    ) -> ModelPrWatchOrchestratorResult:
        if isinstance(request, dict):
            request = ModelPrWatchOrchestratorRequest.model_validate(request)

        result = await self._watch(request)
        await self._publish_result(result)
        return result

    async def _watch(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> ModelPrWatchOrchestratorResult:
        timeout_seconds = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self._contract_timeout_seconds
        )
        if timeout_seconds <= 0:
            timeout_seconds = 0.0

        started = self._monotonic()
        deadline = started + timeout_seconds
        attempts = 0
        last_checks: tuple[ModelPrCheckStatus, ...] = ()

        while True:
            attempts += 1
            try:
                last_checks = self._checks_client.fetch_checks(request)
            except PrChecksClientError as exc:
                return self._result(
                    request,
                    status=EnumPrWatchStatus.FAILED,
                    conclusion=EnumPrWatchConclusion.RED,
                    terminal_event=self._failed_topic,
                    checks=last_checks,
                    attempts=attempts,
                    started=started,
                    error_message=str(exc),
                )

            conclusion = _classify_checks(last_checks)
            if conclusion is EnumPrWatchConclusion.GREEN:
                return self._result(
                    request,
                    status=EnumPrWatchStatus.COMPLETED,
                    conclusion=conclusion,
                    terminal_event=self._completed_topic,
                    checks=last_checks,
                    attempts=attempts,
                    started=started,
                )
            if conclusion is EnumPrWatchConclusion.RED:
                failed = _check_names(last_checks, _RED_BUCKETS)
                return self._result(
                    request,
                    status=EnumPrWatchStatus.FAILED,
                    conclusion=conclusion,
                    terminal_event=self._failed_topic,
                    checks=last_checks,
                    attempts=attempts,
                    started=started,
                    failed_checks=failed,
                    error_message="PR checks failed: " + ", ".join(failed),
                )

            now = self._monotonic()
            if now >= deadline:
                pending = _check_names(last_checks, _PENDING_BUCKETS)
                return self._result(
                    request,
                    status=EnumPrWatchStatus.TIMEOUT,
                    conclusion=EnumPrWatchConclusion.TIMEOUT,
                    terminal_event=self._failed_topic,
                    checks=last_checks,
                    attempts=attempts,
                    started=started,
                    pending_checks=pending,
                    error_message="PR checks timed out with pending checks: "
                    + ", ".join(pending),
                )

            sleep_for = min(request.poll_interval_seconds, max(deadline - now, 0.0))
            await self._sleep(sleep_for)

    def _result(
        self,
        request: ModelPrWatchOrchestratorRequest,
        *,
        status: EnumPrWatchStatus,
        conclusion: EnumPrWatchConclusion,
        terminal_event: str,
        checks: tuple[ModelPrCheckStatus, ...],
        attempts: int,
        started: float,
        failed_checks: tuple[str, ...] = (),
        pending_checks: tuple[str, ...] = (),
        error_message: str = "",
    ) -> ModelPrWatchOrchestratorResult:
        return ModelPrWatchOrchestratorResult(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            status=status,
            conclusion=conclusion,
            terminal_event=terminal_event,
            checks=checks,
            attempts=attempts,
            elapsed_seconds=max(self._monotonic() - started, 0.0),
            failed_checks=failed_checks,
            pending_checks=pending_checks,
            error_message=error_message,
        )

    async def _publish_result(self, result: ModelPrWatchOrchestratorResult) -> None:
        if self._event_bus is None:
            return
        envelope = ModelEventEnvelope[ModelPrWatchOrchestratorResult](
            payload=result,
            correlation_id=result.correlation_id,
            event_type=result.terminal_event,
            source_tool="node_pr_watch_orchestrator",
        )
        publish_envelope = getattr(self._event_bus, "publish_envelope", None)
        try:
            if callable(publish_envelope):
                maybe_awaitable = publish_envelope(
                    envelope=envelope,
                    topic=result.terminal_event,
                )
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
                return

            publish = getattr(self._event_bus, "publish", None)
            if callable(publish):
                maybe_awaitable = publish(
                    result.terminal_event,
                    None,
                    envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                    None,
                )
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
        except Exception:
            logger.exception(
                "Failed to publish PR watch result to %s", result.terminal_event
            )


def _parse_check(row: object) -> ModelPrCheckStatus:
    if not isinstance(row, dict):
        raise PrChecksClientError("gh pr checks entries must be objects")
    bucket = str(row.get("bucket") or "unknown").lower()
    if bucket not in {item.value for item in EnumPrCheckBucket}:
        bucket = EnumPrCheckBucket.UNKNOWN.value
    return ModelPrCheckStatus(
        name=str(row.get("name") or row.get("workflow") or "unnamed-check"),
        state=str(row.get("state") or ""),
        bucket=EnumPrCheckBucket(bucket),
        workflow=str(row.get("workflow") or ""),
        link=str(row.get("link") or ""),
        started_at=str(row.get("startedAt") or ""),
        completed_at=str(row.get("completedAt") or ""),
    )


def _classify_checks(
    checks: tuple[ModelPrCheckStatus, ...],
) -> EnumPrWatchConclusion | None:
    if not checks:
        return EnumPrWatchConclusion.RED
    buckets = {check.bucket for check in checks}
    if buckets & _RED_BUCKETS:
        return EnumPrWatchConclusion.RED
    if buckets.issubset(_GREEN_BUCKETS):
        return EnumPrWatchConclusion.GREEN
    return None


def _check_names(
    checks: tuple[ModelPrCheckStatus, ...],
    buckets: frozenset[EnumPrCheckBucket],
) -> tuple[str, ...]:
    return tuple(check.name for check in checks if check.bucket in buckets)


def _load_contract() -> dict[str, Any]:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"PR watch contract must be a mapping: {_CONTRACT_PATH}")
    return cast(dict[str, Any], raw)


def _topic_containing(contract: dict[str, Any], fragment: str) -> str:
    event_bus = cast(dict[str, Any], contract.get("event_bus") or {})
    topics = event_bus.get("publish_topics")
    if not isinstance(topics, list):
        raise ValueError("PR watch contract must declare event_bus.publish_topics")
    topic = next(
        (
            str(candidate)
            for candidate in topics
            if isinstance(candidate, str) and fragment in candidate
        ),
        "",
    )
    if not topic:
        raise ValueError(f"PR watch contract lacks {fragment!r} publish topic")
    return topic


__all__ = [
    "GitHubCliPrChecksClient",
    "HandlerPrWatchOrchestrator",
    "PrChecksClientError",
    "ProtocolPrChecksClient",
]
