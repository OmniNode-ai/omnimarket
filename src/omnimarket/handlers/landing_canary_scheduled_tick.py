# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing canary's run on the runtime's schedule (OMN-24117).

The canary waits up to an hour on a real pull request, so one tick opens it and later ticks settle it.
``open_canary`` opens the test pull request and records the pending run; ``settle_canary`` reads the pull
request's state and, once it is merged, closed, or the landing bound has run out, runs the canary handler
and returns the run's outcome. A pull request that is still open inside the bound settles to nothing, so
the next tick settles it again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_landing_canary_effect.handlers.handler_landing_canary import (
    HandlerLandingCanary,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelCanaryOpened,
    ModelCanaryOpenRequest,
    ModelCanaryState,
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_landing_canary_effect.overlay import (
    ModelLandingCanaryOverlay,
)
from omnimarket.nodes.node_landing_canary_effect.protocols import (
    CanaryRepoError,
    ProtocolCanaryRepo,
)
from omnimarket.nodes.node_landing_canary_effect.runtime_landing_canary import (
    REPO_BACKENDS,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the landing canary overlay names it")
    return value


class ModelCanaryPendingRun(NamedTuple):
    """The pull request one tick opened, so a later tick can settle it."""

    opened: ModelCanaryOpened | None
    open_error: str
    started: datetime


class ModelLandingCanaryTickOutcome(NamedTuple):
    """How one settle of the landing canary went."""

    failed: bool
    detail: str


class ResumeCanaryRepo:
    """A repository port that resumes a run: ``open`` returns the pull request the run already opened."""

    def __init__(self, real: ProtocolCanaryRepo, run: ModelCanaryPendingRun) -> None:
        self._real = real
        self._run = run

    def open(self, request: ModelCanaryOpenRequest) -> ModelCanaryOpened:
        if self._run.opened is None:
            raise CanaryRepoError(
                self._run.open_error or "the canary pull request was not opened"
            )
        return self._run.opened

    def state(self, number: int) -> ModelCanaryState:
        return self._real.state(number)


def open_canary(
    *,
    canary_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    repo: ProtocolCanaryRepo | None = None,
) -> ModelCanaryPendingRun:
    """Open the canary's test pull request and record the pending run."""
    overlay = load_structured_overlay(
        canary_overlay_id, ModelLandingCanaryOverlay, environ
    )
    started = clock().astimezone(UTC)
    if repo is None:
        repo = REPO_BACKENDS[overlay.target.backend](overlay.target, environ or {})
    nonce = hashlib.sha256(
        f"landing-canary-scheduled|{stamp_text(started)}".encode()
    ).hexdigest()[:12]
    request = ModelCanaryOpenRequest(
        repo=overlay.target.repo,
        base=overlay.target.base,
        branch=f"{overlay.target.branch_prefix}{nonce}",
        nonce=nonce,
        title=f"{overlay.target.title_prefix} {nonce}",
    )
    try:
        opened = repo.open(request)
    except CanaryRepoError as exc:
        return ModelCanaryPendingRun(None, str(exc), started)
    return ModelCanaryPendingRun(opened, "", started)


def settle_canary(
    *,
    canary_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    run: ModelCanaryPendingRun,
    repo: ProtocolCanaryRepo | None = None,
    appender: ProtocolReadingAppender | None = None,
) -> ModelLandingCanaryTickOutcome | None:
    """Settle the canary once its pull request is terminal or the landing bound has run out."""
    overlay = load_structured_overlay(
        canary_overlay_id, ModelLandingCanaryOverlay, environ
    )
    if repo is None:
        repo = REPO_BACKENDS[overlay.target.backend](overlay.target, environ or {})
    if run.opened is not None:
        try:
            state = repo.state(run.opened.number)
        except CanaryRepoError:
            state = None
        opened_at = run.opened.opened_at or run.started
        bound = timedelta(seconds=overlay.target.landing_bound_seconds)
        settled = state is not None and (state.merged or state.closed)
        if not settled and clock().astimezone(UTC) < opened_at.astimezone(UTC) + bound:
            return None
    host = _required(overlay.ledger_append.host_env, environ or {})
    if appender is None:
        appender = build_appender(
            overlay.ledger_append, overlay.publish.lane, environ or {}
        )

    def _sleep(_seconds: float) -> None:
        raise RuntimeError("the settle step must not wait")

    handler = HandlerLandingCanary(
        overlay=overlay,
        host=host,
        repo=ResumeCanaryRepo(repo, run),
        appender=appender,
        clock=clock,
        sleep=_sleep,
    )
    result = handler.handle(
        ModelLandingCanaryCommand(
            correlation_id=f"landing-canary-scheduled-{clock():%Y%m%dT%H%M%SZ}",
            write=True,
        )
    )
    if result.status == "failed":
        return ModelLandingCanaryTickOutcome(
            True, f"the landing canary run failed: {result.failure}"
        )
    if result.ledger.startswith("append-failed"):
        return ModelLandingCanaryTickOutcome(
            True, "the landing canary row was not accepted by the ledger"
        )
    if result.verdict is EnumReadingVerdict.UNKNOWN:
        return ModelLandingCanaryTickOutcome(
            True,
            "the landing canary could not read the landing: "
            + ",".join(result.reasons),
        )
    ending = result.ending.value if result.ending is not None else "none"
    return ModelLandingCanaryTickOutcome(
        False,
        f"verdict={result.verdict.value} ending={ending} elapsed_s={result.elapsed_seconds} bound_s={result.bound_seconds}",
    )


__all__: list[str] = [
    "CanaryRepoError",
    "LedgerRouteError",
    "ModelCanaryPendingRun",
    "ModelLandingCanaryTickOutcome",
    "NodeOverlayError",
    "ResumeCanaryRepo",
    "open_canary",
    "settle_canary",
]
