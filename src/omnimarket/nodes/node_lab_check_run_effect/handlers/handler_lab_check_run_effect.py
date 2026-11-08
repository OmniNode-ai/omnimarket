# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabCheckRunEffect: post a lab-bound job's required check on the exact head, carrying its attempt identity.

Canonical shape: ``handle(ModelLabCheckRunRequest) -> ModelLabCheckRunResult``. Every setting comes from the overlay;
the check-run API, the emission sink and the clock are injected (in-memory by default, GitHub and a journal as the real
backend).

POST, for attempt A when the order's current attempt is C:

* the repository is not one the overlay names as lab-bound: refused before any call (no public repository reaches a lab
  runner);
* A is not C: the attempt is superseded. Nothing is posted (a pass from it would satisfy the required check on the
  head), and any run of A on the head that still counts is set to the overlay's superseded conclusion;
* A is C: runs of other attempts on the exact head that still count (running, or holding a passing conclusion) are
  invalidated, then A's run is created or updated, and the head is read back. A post the read-back cannot see is a
  ``failed_readback_missing`` result, never a posted one;
* a post made after the wait bound from the dispatch is posted and reported ``posted_late`` with a failure on the chain.

READ: what the head's runs read as for the current attempt (``read_head``); a missing check reads missing.

Every call is one chain under the floor's emission contract: a start, then a success or a typed failure.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.nodes.node_lab_check_run_effect.adapters.github_check_run_api import (
    GithubCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.adapters.inmemory_check_run_api import (
    InMemoryCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.head_reading import (
    COMPLETED,
    SUCCESS,
    read_head,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.http_transport import (
    UrllibHttpJson,
)
from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    EnumLabCheckRunConclusion,
    EnumLabCheckRunOperation,
    EnumLabCheckRunOutcome,
    ModelCheckRunRecord,
    ModelLabCheckRunRequest,
    ModelLabCheckRunResult,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay import (
    ModelInMemorySource,
    ModelJournalEmission,
    ModelLabCheckRunOverlay,
    load_overlay,
)
from omnimarket.nodes.node_lab_check_run_effect.protocols.protocol_check_run_api import (
    CheckRunApiError,
    ProtocolCheckRunApi,
    ProtocolHttpJson,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

#: The typed failure code of each outcome that is not a success on the chain.
FAILURE_CODE: dict[EnumLabCheckRunOutcome, str] = {
    EnumLabCheckRunOutcome.POSTED_LATE: "lab_check_posted_late",
    EnumLabCheckRunOutcome.REFUSED_SUPERSEDED_ATTEMPT: "lab_check_attempt_superseded",
    EnumLabCheckRunOutcome.REFUSED_REPOSITORY_NOT_LAB_BOUND: "lab_check_repository_not_lab_bound",
    EnumLabCheckRunOutcome.FAILED_READBACK_MISSING: "lab_check_readback_missing",
    EnumLabCheckRunOutcome.FAILED_INVALIDATION_NOT_READ: "lab_check_invalidation_not_read",
    EnumLabCheckRunOutcome.FAILED_API: "lab_check_api_failed",
}

_STATUS = {
    EnumLabCheckRunConclusion.PENDING: ("in_progress", None),
    EnumLabCheckRunConclusion.SUCCESS: (COMPLETED, SUCCESS),
    EnumLabCheckRunConclusion.FAILURE: (COMPLETED, "failure"),
}


def load_topics() -> dict[str, str]:
    """The topics by role, read from the contract (the handler holds no topic string)."""
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return dict(loaded["topics"])


class HandlerLabCheckRunEffect:
    def __init__(
        self,
        overlay: ModelLabCheckRunOverlay | None = None,
        api: ProtocolCheckRunApi | None = None,
        sink: ProtocolEmissionSink | None = None,
        http: ProtocolHttpJson | None = None,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._api = api
        self._sink = sink
        self._http = http
        self._clock = clock or (lambda: datetime.now(UTC))
        self._environ = environ

    # ------------------------------------------------------------------------------------------ wiring

    def _api_for(self, overlay: ModelLabCheckRunOverlay) -> ProtocolCheckRunApi:
        if self._api is not None:
            return self._api
        source = overlay.source
        if isinstance(source, ModelInMemorySource):
            self._api = InMemoryCheckRunApi(source.fixture)
        else:
            self._api = GithubCheckRunApi(
                source,
                self._http or UrllibHttpJson(),
                os.environ if self._environ is None else self._environ,
            )
        return self._api

    @property
    def sink(self) -> ProtocolEmissionSink:
        """The sink events go to; built from the overlay on the first handle when none was injected."""
        if self._sink is None:
            raise RuntimeError("the sink is built on the first handle")
        return self._sink

    def _sink_for(self, overlay: ModelLabCheckRunOverlay) -> ProtocolEmissionSink:
        if self._sink is None:
            emission = overlay.emission
            self._sink = (
                JsonlEmissionSink(emission.journal())
                if isinstance(emission, ModelJournalEmission)
                else InMemoryEmissionSink()
            )
        return self._sink

    # ------------------------------------------------------------------------------------------ handle

    def handle(self, request: ModelLabCheckRunRequest) -> ModelLabCheckRunResult:
        overlay = self._overlay or load_overlay(self._environ)
        api = self._api_for(overlay)
        chain = ChainEmitter(
            self._sink_for(overlay),
            node=overlay.emission.node,
            piece=overlay.emission.piece,
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend="in_memory"
            if isinstance(overlay.source, ModelInMemorySource)
            else "real",
        )
        chain.start()
        try:
            if request.operation is EnumLabCheckRunOperation.POST:
                result = self._post(request, overlay, api)
            else:
                result = self._read(request, overlay, api)
        except CheckRunApiError as exc:
            result = self._result(
                request,
                overlay,
                api,
                EnumLabCheckRunOutcome.FAILED_API,
                detail=str(exc),
            )
        code = FAILURE_CODE.get(result.outcome)
        if code is None:
            chain.success(
                {
                    "check_runs_posted": 1 if result.posted is not None else 0,
                    "check_runs_invalidated": len(result.invalidated),
                }
            )
        else:
            chain.failure(code, result.detail)
        return result

    @staticmethod
    def event_for(result: ModelLabCheckRunResult) -> tuple[str, dict[str, Any]]:
        """The terminal event of a run: the failure topic for every outcome that failed the chain."""
        topics = load_topics()
        failed = result.outcome in FAILURE_CODE
        topic = topics["check_run_failed"] if failed else topics["check_run_completed"]
        return topic, result.model_dump(mode="json")

    # ------------------------------------------------------------------------------------------ internals

    @staticmethod
    def _within_bound(
        request: ModelLabCheckRunRequest, overlay: ModelLabCheckRunOverlay
    ) -> bool:
        bound = timedelta(seconds=overlay.settings.wait_bound_seconds)
        return request.now - request.dispatched_at <= bound

    def _result(
        self,
        request: ModelLabCheckRunRequest,
        overlay: ModelLabCheckRunOverlay,
        api: ProtocolCheckRunApi,
        outcome: EnumLabCheckRunOutcome,
        *,
        posted: ModelCheckRunRecord | None = None,
        invalidated: tuple[int, ...] = (),
        verdict: EnumLabCheckVerdict | None = None,
        detail: str = "",
    ) -> ModelLabCheckRunResult:
        return ModelLabCheckRunResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            outcome=outcome,
            backend=overlay.source.backend,
            repository=request.repository,
            head_sha=request.head_sha,
            check_name=overlay.settings.check_name,
            posted=posted,
            invalidated=invalidated,
            verdict=verdict,
            within_wait_bound=self._within_bound(request, overlay),
            calls=api.calls,
            detail=detail,
        )

    def _on_head(
        self,
        request: ModelLabCheckRunRequest,
        overlay: ModelLabCheckRunOverlay,
        api: ProtocolCheckRunApi,
    ) -> tuple[ModelCheckRunRecord, ...]:
        """The head's runs of the required check; a run of another head or name never counts."""
        runs = api.list_runs(
            repository=request.repository,
            head_sha=request.head_sha,
            check_name=overlay.settings.check_name,
        )
        return tuple(
            r
            for r in runs
            if r.head_sha == request.head_sha and r.name == overlay.settings.check_name
        )

    def _counts(
        self, run: ModelCheckRunRecord, overlay: ModelLabCheckRunOverlay
    ) -> bool:
        """A run still counts toward the required check: running, or holding a passing conclusion."""
        return run.status != COMPLETED or (
            run.conclusion in overlay.settings.passing_conclusions
        )

    def _counting(
        self,
        runs: tuple[ModelCheckRunRecord, ...],
        overlay: ModelLabCheckRunOverlay,
        *,
        attempt: str | None = None,
        other_than: str | None = None,
    ) -> list[ModelCheckRunRecord]:
        """Runs that still count, of one attempt or of every attempt but one."""
        return [
            r
            for r in runs
            if self._counts(r, overlay)
            and (attempt is None or r.attempt_id == attempt)
            and (other_than is None or r.attempt_id != other_than)
        ]

    def _invalidate(
        self,
        runs: tuple[ModelCheckRunRecord, ...],
        *,
        keep_attempt: str | None,
        remove_attempt: str | None,
        request: ModelLabCheckRunRequest,
        overlay: ModelLabCheckRunOverlay,
        api: ProtocolCheckRunApi,
    ) -> tuple[int, ...]:
        """Set every counting run of the named attempts to the superseded conclusion; return their ids."""
        settings = overlay.settings
        done: list[int] = []
        for run in runs:
            if not self._counts(run, overlay):
                continue
            if remove_attempt is not None and run.attempt_id != remove_attempt:
                continue
            if keep_attempt is not None and run.attempt_id == keep_attempt:
                continue
            api.update_run(
                repository=request.repository,
                check_run_id=run.check_run_id,
                status=COMPLETED,
                conclusion=settings.superseded_conclusion,
                title=settings.title_superseded.format(
                    attempt=run.attempt_id or "unidentified"
                ),
                summary=f"Current attempt: {request.current_attempt_id}. {request.detail}".strip(),
            )
            done.append(run.check_run_id)
        return tuple(done)

    def _post(
        self,
        request: ModelLabCheckRunRequest,
        overlay: ModelLabCheckRunOverlay,
        api: ProtocolCheckRunApi,
    ) -> ModelLabCheckRunResult:
        settings = overlay.settings
        if request.repository not in settings.lab_repositories:
            return self._result(
                request,
                overlay,
                api,
                EnumLabCheckRunOutcome.REFUSED_REPOSITORY_NOT_LAB_BOUND,
                detail=f"{request.repository} is not a lab-bound repository",
            )
        attempt = request.attempt_id or ""
        runs = self._on_head(request, overlay, api)
        if attempt != request.current_attempt_id:
            invalidated = self._invalidate(
                runs,
                keep_attempt=None,
                remove_attempt=attempt,
                request=request,
                overlay=overlay,
                api=api,
            )
            still = self._counting(
                self._on_head(request, overlay, api), overlay, attempt=attempt
            )
            if still:
                return self._result(
                    request,
                    overlay,
                    api,
                    EnumLabCheckRunOutcome.FAILED_INVALIDATION_NOT_READ,
                    invalidated=invalidated,
                    detail=f"attempt {attempt} still has {len(still)} counting run(s) on the head after invalidation",
                )
            return self._result(
                request,
                overlay,
                api,
                EnumLabCheckRunOutcome.REFUSED_SUPERSEDED_ATTEMPT,
                invalidated=invalidated,
                detail=f"attempt {attempt} is not the current attempt {request.current_attempt_id}; nothing posted",
            )
        invalidated = self._invalidate(
            runs,
            keep_attempt=attempt,
            remove_attempt=None,
            request=request,
            overlay=overlay,
            api=api,
        )
        assert request.conclusion is not None
        status, conclusion = _STATUS[request.conclusion]
        title = {
            EnumLabCheckRunConclusion.PENDING: settings.title_pending,
            EnumLabCheckRunConclusion.SUCCESS: settings.title_success,
            EnumLabCheckRunConclusion.FAILURE: settings.title_failure,
        }[request.conclusion].format(attempt=attempt)
        own = [r for r in runs if r.attempt_id == attempt]
        if own:
            target = max(own, key=lambda r: r.check_run_id)
            api.update_run(
                repository=request.repository,
                check_run_id=target.check_run_id,
                status=status,
                conclusion=conclusion,
                title=title,
                summary=request.detail,
            )
            run_id = target.check_run_id
        else:
            run_id = api.create_run(
                repository=request.repository,
                head_sha=request.head_sha,
                name=settings.check_name,
                attempt_id=attempt,
                status=status,
                conclusion=conclusion,
                title=title,
                summary=request.detail,
            ).check_run_id
        after = self._on_head(request, overlay, api)
        seen = next(
            (
                r
                for r in after
                if r.check_run_id == run_id
                and r.attempt_id == attempt
                and r.status == status
                and r.conclusion == conclusion
            ),
            None,
        )
        verdict, reading = read_head(
            after,
            check_name=settings.check_name,
            head_sha=request.head_sha,
            current_attempt_id=request.current_attempt_id,
            dispatched_at=request.dispatched_at,
            now=request.now,
            wait_bound_seconds=settings.wait_bound_seconds,
        )
        left = self._counting(after, overlay, other_than=attempt)
        if left:
            return self._result(
                request,
                overlay,
                api,
                EnumLabCheckRunOutcome.FAILED_INVALIDATION_NOT_READ,
                posted=seen,
                invalidated=invalidated,
                verdict=verdict,
                detail=f"{len(left)} counting run(s) of another attempt remain on the head after invalidation",
            )
        if seen is None:
            return self._result(
                request,
                overlay,
                api,
                EnumLabCheckRunOutcome.FAILED_READBACK_MISSING,
                invalidated=invalidated,
                verdict=verdict,
                detail=f"the post of attempt {attempt} was not read back on the head: {reading}",
            )
        late = not self._within_bound(request, overlay)
        return self._result(
            request,
            overlay,
            api,
            EnumLabCheckRunOutcome.POSTED_LATE
            if late
            else EnumLabCheckRunOutcome.POSTED,
            posted=seen,
            invalidated=invalidated,
            verdict=verdict,
            detail=(
                f"posted after the {settings.wait_bound_seconds} s wait bound: {reading}"
                if late
                else reading
            ),
        )

    def _read(
        self,
        request: ModelLabCheckRunRequest,
        overlay: ModelLabCheckRunOverlay,
        api: ProtocolCheckRunApi,
    ) -> ModelLabCheckRunResult:
        settings = overlay.settings
        runs = self._on_head(request, overlay, api)
        verdict, reading = read_head(
            runs,
            check_name=settings.check_name,
            head_sha=request.head_sha,
            current_attempt_id=request.current_attempt_id,
            dispatched_at=request.dispatched_at,
            now=request.now,
            wait_bound_seconds=settings.wait_bound_seconds,
        )
        return self._result(
            request,
            overlay,
            api,
            EnumLabCheckRunOutcome.READ,
            verdict=verdict,
            detail=reading,
        )


__all__ = ["FAILURE_CODE", "HandlerLabCheckRunEffect", "load_topics"]
