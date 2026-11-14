# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Queued-roll handler of node_dev_roll_decision_request_effect (OMN-24251).

One dev roll manifest in, one decision request out, or a typed skip. The
handler queues a :class:`ModelDevTipChanged` for every accepted manifest and
publishes the pending events plus the shadow last roll as one
:class:`ModelDevRollFacts` on the decision-request topic. Never raises out of
``handle``: an unusable manifest, a job already requested, or an unreadable
overlay or store all come back as a typed skip (``node_unavailable`` carries
the error message; a :class:`NodeOverlayError` is a ``ValueError``). The
handler holds no producer: the runtime publishes the returned model on the
contract's ``published_events`` topic through its event_bus. Shadow only: no
deploy-agent command is published.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from omnimarket.models.dev_roll import (
    EnumDevRollHealth,
    ModelDevRollFacts,
    ModelDevRollRecord,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.store_dev_roll_observation import (
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    EnumDevRollDecisionRequestSkipReason,
    ModelDevRollDecisionRequestPolicy,
    ModelDevRollDecisionRequestSkipped,
    ModelDevRollManifestEvent,
    ModelDevRollObservationState,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.overlay import (
    load_decision_request_policy,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.protocols import (
    ProtocolDevRollObservationStore,
)

# The shadow plan's identifier namespace (parent OMN-23532): the compute node's
# replay test derives the same event ids from the same job ids. It is an
# identifier namespace, not a bound: no overlay supplies it.
NAMESPACE = UUID("6f1c2f1e-0d1a-4c52-9a53-0a7c5d1b9d10")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

Result = ModelDevRollFacts | ModelDevRollDecisionRequestSkipped


class HandlerDevRollQueuedRoll:
    """``handle(request) -> ModelDevRollFacts | ModelDevRollDecisionRequestSkipped``.

    The runtime builds the handler with no arguments; the policy and store are
    then resolved lazily on the first request (constructor arguments, else the
    overlay) and cached on the instance. A test passes them in.
    """

    def __init__(
        self,
        store: ProtocolDevRollObservationStore | None = None,
        *,
        policy: ModelDevRollDecisionRequestPolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._environ = environ
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def _resolve_policy(self) -> ModelDevRollDecisionRequestPolicy:
        if self._policy is None:
            environ = os.environ if self._environ is None else self._environ
            self._policy = load_decision_request_policy(environ)
        return self._policy

    def _resolve_store(self) -> ProtocolDevRollObservationStore:
        if self._store is None:
            environ = os.environ if self._environ is None else self._environ
            policy = self._resolve_policy()
            self._store = store_from_policy(policy, environ)
        return self._store

    def _skip(
        self,
        job_id: str | None,
        reason: EnumDevRollDecisionRequestSkipReason,
        detail: str | None = None,
    ) -> ModelDevRollDecisionRequestSkipped:
        return ModelDevRollDecisionRequestSkipped(
            job_id=job_id, reason=reason, detail=detail
        )

    def _shadow_last_roll(
        self,
        state: ModelDevRollObservationState,
        policy: ModelDevRollDecisionRequestPolicy,
        now: datetime,
    ) -> ModelDevRollRecord | None:
        """The last observed roll, taken to run for the policy's duration."""
        started = state.last_roll_started_at
        if started is None:
            return None
        duration = timedelta(seconds=policy.shadow_roll_duration_seconds)
        if now < started + duration:
            return ModelDevRollRecord(
                job_id=state.last_roll_job_id,
                git_ref=state.last_roll_git_ref,
                runtime_started_at=started,
                finished_at=None,
                health=EnumDevRollHealth.UNKNOWN,
                healthy_since=None,
            )
        finished_at = started + duration
        return ModelDevRollRecord(
            job_id=state.last_roll_job_id,
            git_ref=state.last_roll_git_ref,
            runtime_started_at=started,
            finished_at=finished_at,
            health=EnumDevRollHealth.HEALTHY,
            healthy_since=finished_at,
        )

    def handle(self, request: ModelDevRollManifestEvent) -> Result:
        """One manifest in, one decision request (or a typed skip) out."""
        job_id = request.job_id
        if not job_id or not job_id.strip():
            return self._skip(
                job_id,
                EnumDevRollDecisionRequestSkipReason.JOB_ID_MISSING,
                "job_id is missing or blank",
            )
        try:
            policy = self._resolve_policy()
        except (ValueError, OSError) as exc:
            return self._skip(
                job_id,
                EnumDevRollDecisionRequestSkipReason.NODE_UNAVAILABLE,
                str(exc),
            )
        issuer = request.issuer
        match = re.fullmatch(policy.issuer_repository_pattern, issuer or "")
        if match is None:
            return self._skip(
                job_id,
                EnumDevRollDecisionRequestSkipReason.ISSUER_NOT_A_CI_REQUEST,
                f"issuer {issuer!r} does not match the CI issuer pattern",
            )
        git_ref = request.git_ref
        if not git_ref or not _SHA_RE.fullmatch(git_ref):
            return self._skip(
                job_id,
                EnumDevRollDecisionRequestSkipReason.GIT_REF_NOT_A_SHA,
                f"git_ref {git_ref!r} is not 40 lowercase hex characters",
            )
        merged_at = (
            request.requested_at
            if request.requested_at is not None
            else request.accepted_at
        )
        if merged_at is None or merged_at.utcoffset() is None:
            return self._skip(
                job_id,
                EnumDevRollDecisionRequestSkipReason.TIME_MISSING,
                "neither requested_at nor accepted_at is an instant with a UTC offset",
            )
        try:
            store = self._resolve_store()
            state = store.load()
            if job_id in state.seen_job_ids:
                return self._skip(
                    job_id,
                    EnumDevRollDecisionRequestSkipReason.ALREADY_REQUESTED,
                    f"job_id {job_id!r} already yielded a decision request",
                )
            now = self._clock()
            pending = (
                *state.pending_events,
                ModelDevTipChanged(
                    event_id=uuid5(NAMESPACE, job_id),
                    repository=match.group("repository"),
                    sha=git_ref,
                    merged_at=merged_at,
                ),
            )
            seen = (*state.seen_job_ids, job_id)[-policy.seen_job_ids_kept :]
            facts = ModelDevRollFacts(
                correlation_id=uuid5(NAMESPACE, f"request/{job_id}"),
                observed_at=now,
                pending_events=pending,
                last_roll=self._shadow_last_roll(state, policy, now),
            )
            store.save(
                ModelDevRollObservationState(
                    pending_events=pending,
                    seen_job_ids=seen,
                    last_roll_job_id=state.last_roll_job_id,
                    last_roll_git_ref=state.last_roll_git_ref,
                    last_roll_started_at=state.last_roll_started_at,
                )
            )
        except (ValueError, OSError) as exc:
            return self._skip(
                job_id,
                EnumDevRollDecisionRequestSkipReason.NODE_UNAVAILABLE,
                str(exc),
            )
        return facts
