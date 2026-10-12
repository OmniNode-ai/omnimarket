from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.github_api import GitHubApiError, rest_json
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref
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
_SECRET_NAME = "GITHUB_TOKEN"
_PAGE_SIZE = 100
_PENDING_BUCKETS = frozenset({EnumPrCheckBucket.PENDING, EnumPrCheckBucket.UNKNOWN})
_GREEN_BUCKETS = frozenset({EnumPrCheckBucket.PASS, EnumPrCheckBucket.SKIPPING})
_RED_BUCKETS = frozenset({EnumPrCheckBucket.FAIL, EnumPrCheckBucket.CANCEL})


class PrChecksClientError(RuntimeError):
    """Raised when the gh checks effect boundary cannot return a valid snapshot."""


class PrWatchPollRefusedError(RuntimeError):
    """Raised when a read of the PR is attempted inside the poll interval."""


class PrWatchPollSchedule:
    """At most one read per interval: the floor on the spacing of two reads."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last_poll: float | None = None

    @property
    def next_due(self) -> float:
        """When the next read may happen (``-inf`` before the first)."""
        if self._last_poll is None:
            return float("-inf")
        return self._last_poll + self._interval

    def wait_needed(self, now: float) -> float:
        return max(self.next_due - now, 0.0)

    def claim(self, now: float) -> None:
        wait = self.wait_needed(now)
        if wait > 0:
            raise PrWatchPollRefusedError(
                f"poll refused: {wait:g} s of the {self._interval:g} s interval remain"
            )
        self._last_poll = now


class ProtocolPrChecksClient(Protocol):
    """Typed effect boundary for reading GitHub PR check state."""

    def fetch_checks(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> tuple[ModelPrCheckStatus, ...]: ...


class HandlerPrChecksGithubRest:
    """Read PR checks over the GitHub REST API through the shared GitHub helper.

    The deployed effects runtime carries a gh whose ``pr checks`` has no
    ``--json`` flag, so the read does not go through the CLI. Three endpoints
    answer it: the PR (for its head sha), the head's check runs (paged) and its
    commit statuses. Every call is a GET.
    """

    def __init__(
        self,
        rest_get: Callable[[str, str], dict[str, Any]] | None = None,
        token_source: Callable[[], str] | None = None,
    ) -> None:
        self._rest_get = rest_get or _rest_get
        self._token_source = token_source or _resolve_github_token

    def fetch_checks(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> tuple[ModelPrCheckStatus, ...]:
        if request.required_only:
            raise PrChecksClientError(
                "required_only is not served: the REST read cannot tell which "
                "checks the base branch requires"
            )
        token = self._token_source()
        try:
            pull = self._rest_get(
                f"/repos/{request.repo}/pulls/{request.pr_number}", token
            )
            head = pull.get("head")
            sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
            if not sha:
                raise PrChecksClientError("PR response carried no head sha")
            runs = self._check_runs(request.repo, sha, token)
            status = self._rest_get(
                f"/repos/{request.repo}/commits/{sha}/status", token
            )
        except GitHubApiError as exc:
            code = f" (HTTP {exc.status_code})" if exc.status_code else ""
            raise PrChecksClientError(f"GitHub read failed{code}: {exc}") from exc
        statuses = status.get("statuses")
        return tuple(_parse_check_run(row) for row in runs) + tuple(
            _parse_commit_status(row)
            for row in (statuses if isinstance(statuses, list) else [])
        )

    def _check_runs(self, repo: str, sha: str, token: str) -> list[object]:
        runs: list[object] = []
        page = 1
        while True:
            body = self._rest_get(
                f"/repos/{repo}/commits/{sha}/check-runs?per_page={_PAGE_SIZE}&page={page}",
                token,
            )
            batch = body.get("check_runs")
            if not isinstance(batch, list):
                raise PrChecksClientError("check-runs response must carry a list")
            runs.extend(batch)
            if not batch or len(runs) >= int(body.get("total_count") or 0):
                return runs
            page += 1


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
        self._checks_client = checks_client or HandlerPrChecksGithubRest()
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
        self, request: ModelPrWatchOrchestratorRequest
    ) -> ModelPrWatchOrchestratorResult:
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
        schedule = PrWatchPollSchedule(request.poll_interval_seconds)
        attempts = 0
        last_checks: tuple[ModelPrCheckStatus, ...] = ()

        while True:
            wait = schedule.wait_needed(self._monotonic())
            while wait > 0:
                await self._sleep(wait)
                wait = schedule.wait_needed(self._monotonic())
            schedule.claim(self._monotonic())
            attempts += 1
            try:
                last_checks = await asyncio.to_thread(
                    self._checks_client.fetch_checks, request
                )
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
            if now >= deadline or schedule.next_due > deadline:
                # No further read fits before the deadline without landing
                # inside the interval, so the watch ends at the deadline.
                if deadline > now:
                    await self._sleep(deadline - now)
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


_RUN_CONCLUSION_BUCKETS: dict[str, EnumPrCheckBucket] = {
    "success": EnumPrCheckBucket.PASS,
    "skipped": EnumPrCheckBucket.SKIPPING,
    "neutral": EnumPrCheckBucket.SKIPPING,
    "failure": EnumPrCheckBucket.FAIL,
    "timed_out": EnumPrCheckBucket.FAIL,
    "action_required": EnumPrCheckBucket.FAIL,
    "startup_failure": EnumPrCheckBucket.FAIL,
    "stale": EnumPrCheckBucket.FAIL,
    "cancelled": EnumPrCheckBucket.CANCEL,
}
_RUN_PENDING_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)
_STATUS_STATE_BUCKETS: dict[str, EnumPrCheckBucket] = {
    "success": EnumPrCheckBucket.PASS,
    "pending": EnumPrCheckBucket.PENDING,
    "failure": EnumPrCheckBucket.FAIL,
    "error": EnumPrCheckBucket.FAIL,
}


def _parse_check_run(row: object) -> ModelPrCheckStatus:
    if not isinstance(row, dict):
        raise PrChecksClientError("check-run entries must be objects")
    status = str(row.get("status") or "").lower()
    conclusion = str(row.get("conclusion") or "").lower()
    if status in _RUN_PENDING_STATUSES:
        bucket = EnumPrCheckBucket.PENDING
    elif status == "completed":
        bucket = _RUN_CONCLUSION_BUCKETS.get(conclusion, EnumPrCheckBucket.UNKNOWN)
    else:
        bucket = EnumPrCheckBucket.UNKNOWN
    app = row.get("app")
    return ModelPrCheckStatus(
        name=str(row.get("name") or "unnamed-check"),
        state=conclusion or status,
        bucket=bucket,
        workflow=str(app.get("slug") or "") if isinstance(app, dict) else "",
        link=str(row.get("html_url") or ""),
        started_at=str(row.get("started_at") or ""),
        completed_at=str(row.get("completed_at") or ""),
    )


def _parse_commit_status(row: object) -> ModelPrCheckStatus:
    if not isinstance(row, dict):
        raise PrChecksClientError("commit-status entries must be objects")
    state = str(row.get("state") or "").lower()
    return ModelPrCheckStatus(
        name=str(row.get("context") or "unnamed-status"),
        state=state,
        bucket=_STATUS_STATE_BUCKETS.get(state, EnumPrCheckBucket.UNKNOWN),
        link=str(row.get("target_url") or ""),
        started_at=str(row.get("created_at") or ""),
        completed_at=str(row.get("updated_at") or "") if state != "pending" else "",
    )


def _rest_get(path: str, token: str) -> dict[str, Any]:
    return rest_json("GET", path, token=token)


def _resolve_github_token() -> str:
    ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
    secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
    if secret is None:
        raise PrChecksClientError(f"{ref} is not set in the secret store")
    return secret.get_secret_value()


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
    "HandlerPrChecksGithubRest",
    "HandlerPrWatchOrchestrator",
    "PrChecksClientError",
    "PrWatchPollRefusedError",
    "PrWatchPollSchedule",
    "ProtocolPrChecksClient",
]
