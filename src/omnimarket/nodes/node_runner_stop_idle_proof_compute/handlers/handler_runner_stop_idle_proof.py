# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRunnerStopIdleProof: whether the proof groups are idle and no queued job names a stop-set label.

COMPUTE handler: no clock read (the request carries the time), no state, no I/O. The proof judges the
newest ``required_reads`` reads. A busy runner in a proof group, or a queued job naming a stop-set
label, in any judged read reads NOT WORKING. Too few reads, reads closer together than the gap, a stale
or future newest read, a failed read and a read that did not cover a proof group read UNKNOWN and never
WORKING.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_stop_idle_proof_compute.models import (
    ModelIdleProofSubject,
    ModelProofRead,
    ModelRunnerStopIdleProofRequest,
    ModelRunnerStopIdleProofResult,
)

SUBJECT_READ_COUNT = "read_count"
SUBJECT_READ_SPACING = "read_spacing"
SUBJECT_READ_AGE = "read_age"
SUBJECT_READ_FAILURES = "read_failures"
SUBJECT_QUEUED_STOP_SET_LABEL = "queued_stop_set_label_jobs"
SUBJECT_QUEUE_POOL_PROVEN = "queue_pool_proven_before_stop"
SUBJECT_QUEUE_POOL_HOSTS = "queue_pool_hosts_after_stop"

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


def group_read_subject(group_id: int) -> str:
    return f"group_read:{group_id}"


def group_idle_subject(group_id: int) -> str:
    return f"group_idle:{group_id}"


class HandlerRunnerStopIdleProof:
    """handle(request) -> proof; subjects are ordered read count, spacing, age, failures, then per group, then the queue."""

    def handle(
        self, request: ModelRunnerStopIdleProofRequest
    ) -> ModelRunnerStopIdleProofResult:
        settings = request.settings
        window = tuple(sorted(request.reads, key=lambda r: r.read_at))[
            -settings.required_reads :
        ]
        subjects: list[ModelIdleProofSubject] = [
            self._read_count(window, settings.required_reads),
            self._read_spacing(window, settings.min_gap_seconds),
            self._read_age(window, request),
            self._read_failures(window),
        ]
        groups = tuple(sorted(set(settings.proof_group_ids)))
        busy_names: list[str] = []
        for gid in groups:
            subjects.append(self._group_read(window, gid))
            idle, names = self._group_idle(window, gid)
            subjects.append(idle)
            busy_names.extend(names)
        queued, job_ids = self._queued_labels(window, settings.stop_set_labels)
        subjects.append(queued)
        refused: list[str] = []
        if settings.queue_pool_stop_name_prefixes:
            pool, refused = self._queue_pool_proven(request)
            subjects.append(pool)
        if settings.queue_pool_hosts:
            hosts, spread_refused = self._queue_pool_hosts_after_stop(request)
            subjects.append(hosts)
            refused = sorted({*refused, *spread_refused})
        verdicts = {s.verdict for s in subjects}
        verdict = _N if _N in verdicts else _U if _U in verdicts else _W
        return ModelRunnerStopIdleProofResult(
            correlation_id=request.correlation_id,
            verdict=verdict,
            subjects=tuple(subjects),
            reads_judged=len(window),
            busy_runners=tuple(sorted(set(busy_names))),
            blocking_job_ids=tuple(sorted(set(job_ids))),
            refused_stop_names=tuple(refused),
        )

    @staticmethod
    def _queue_pool_proven(
        request: ModelRunnerStopIdleProofRequest,
    ) -> tuple[ModelIdleProofSubject, list[str]]:
        """OMN-23156 AC3: a queue runner is on the stop request only after the replacement pool's proof reads WORKING."""
        prefixes = tuple(
            p.casefold() for p in request.settings.queue_pool_stop_name_prefixes
        )
        queued = sorted(
            name
            for name in set(request.stop_request_names)
            if name.casefold().startswith(prefixes)
        )
        if not queued:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_PROVEN,
                    verdict=_W,
                    count=0,
                    reason="no queue runner is on the stop request",
                ),
                [],
            )
        proof = request.queue_pool_proof
        if proof is None:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_PROVEN,
                    verdict=_U,
                    count=len(queued),
                    reason=f"stop refused: {len(queued)} queue runner(s) requested and the replacement pool's proof was not read",
                ),
                queued,
            )
        age = request.now - proof.read_at
        limit = request.settings.queue_pool_proof_max_age_seconds
        if age < timedelta(0) or age > timedelta(seconds=limit):
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_PROVEN,
                    verdict=_U,
                    count=len(queued),
                    reason=f"stop refused: the replacement pool's proof is dated outside its {limit} s bound",
                ),
                queued,
            )
        if proof.verdict is not _W or proof.proven_run_id is None:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_PROVEN,
                    verdict=_N,
                    count=len(queued),
                    reason=(
                        f"stop refused: {len(queued)} queue runner(s) requested and the replacement pool reads "
                        f"{proof.verdict.value} with no proven merge-group run"
                        if proof.proven_run_id is None
                        else f"stop refused: {len(queued)} queue runner(s) requested and the replacement pool reads {proof.verdict.value}"
                    ),
                ),
                queued,
            )
        return (
            ModelIdleProofSubject(
                name=SUBJECT_QUEUE_POOL_PROVEN,
                verdict=_W,
                count=len(queued),
                reason=f"the replacement pool is proven by merge-group run {proof.proven_run_id}",
            ),
            [],
        )

    @staticmethod
    def _queue_pool_hosts_after_stop(
        request: ModelRunnerStopIdleProofRequest,
    ) -> tuple[ModelIdleProofSubject, list[str]]:
        """OMN-23387: a stop that names queue runners is refused when the queue runners left online sit on too few hosts."""
        settings = request.settings
        host_of = {
            p.runner_name_prefix.casefold() + "-": p.host
            for p in settings.queue_pool_hosts
        }

        def host(name: str) -> str | None:
            lowered = name.casefold()
            return next(
                (h for prefix, h in host_of.items() if lowered.startswith(prefix)), None
            )

        stopping = {
            n.casefold() for n in request.stop_request_names if host(n) is not None
        }
        if not stopping:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_HOSTS,
                    verdict=_W,
                    count=0,
                    reason="no queue runner is on the stop request",
                ),
                [],
            )
        names = sorted(
            n for n in request.stop_request_names if n.casefold() in stopping
        )
        if request.queue_runners_online is None:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_HOSTS,
                    verdict=_U,
                    count=len(names),
                    reason=f"stop refused: {len(names)} queue runner(s) requested and the online queue runners were not read",
                ),
                names,
            )
        left = {
            host(n)
            for n in request.queue_runners_online
            if n.casefold() not in stopping and host(n) is not None
        }
        floor = settings.queue_pool_min_hosts
        if len(left) < floor:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUE_POOL_HOSTS,
                    verdict=_N,
                    count=len(left),
                    reason=(
                        f"stop refused: {len(names)} queue runner(s) requested and the queue runners left online "
                        f"would sit on {len(left)} host(s), fewer than the {floor} the overlay requires"
                    ),
                ),
                names,
            )
        return (
            ModelIdleProofSubject(
                name=SUBJECT_QUEUE_POOL_HOSTS,
                verdict=_W,
                count=len(left),
                reason=f"the queue runners left online sit on {len(left)} host(s), at least the {floor} the overlay requires",
            ),
            [],
        )

    @staticmethod
    def _read_count(
        window: tuple[ModelProofRead, ...], required: int
    ) -> ModelIdleProofSubject:
        if len(window) < required:
            return ModelIdleProofSubject(
                name=SUBJECT_READ_COUNT,
                verdict=_U,
                count=len(window),
                reason=f"{len(window)} read(s) given; the proof needs {required}",
            )
        return ModelIdleProofSubject(
            name=SUBJECT_READ_COUNT,
            verdict=_W,
            count=len(window),
            reason=f"{len(window)} read(s) judged",
        )

    @staticmethod
    def _read_spacing(
        window: tuple[ModelProofRead, ...], min_gap_seconds: int
    ) -> ModelIdleProofSubject:
        gaps = [(b.read_at - a.read_at) for a, b in pairwise(window)]
        minimum = timedelta(seconds=min_gap_seconds)
        short = [g for g in gaps if g < minimum]
        if not gaps or short:
            reason = (
                "fewer than two reads to space"
                if not gaps
                else f"{len(short)} gap(s) shorter than {min_gap_seconds} s"
            )
            return ModelIdleProofSubject(
                name=SUBJECT_READ_SPACING, verdict=_U, count=len(short), reason=reason
            )
        return ModelIdleProofSubject(
            name=SUBJECT_READ_SPACING,
            verdict=_W,
            count=0,
            reason=f"every gap is at least {min_gap_seconds} s",
        )

    @staticmethod
    def _read_age(
        window: tuple[ModelProofRead, ...], request: ModelRunnerStopIdleProofRequest
    ) -> ModelIdleProofSubject:
        if not window:
            return ModelIdleProofSubject(
                name=SUBJECT_READ_AGE, verdict=_U, reason="no read to age"
            )
        age = request.now - window[-1].read_at
        if age < timedelta(0):
            return ModelIdleProofSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                reason="the newest read is dated after the time of judgment",
            )
        if age > timedelta(seconds=request.settings.max_age_seconds):
            return ModelIdleProofSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                count=int(age.total_seconds()),
                reason=(
                    f"the newest read is {int(age.total_seconds())} s old; "
                    f"the bound is {request.settings.max_age_seconds} s"
                ),
            )
        return ModelIdleProofSubject(
            name=SUBJECT_READ_AGE,
            verdict=_W,
            count=int(age.total_seconds()),
            reason="the newest read is within its max age",
        )

    @staticmethod
    def _read_failures(window: tuple[ModelProofRead, ...]) -> ModelIdleProofSubject:
        failed = [r for r in window if r.read_failure is not None]
        if failed:
            why = ",".join(
                sorted({r.read_failure.value for r in failed if r.read_failure})
            )
            return ModelIdleProofSubject(
                name=SUBJECT_READ_FAILURES,
                verdict=_U,
                count=len(failed),
                reason=f"{len(failed)} read(s) failed: {why}",
            )
        return ModelIdleProofSubject(
            name=SUBJECT_READ_FAILURES,
            verdict=_W,
            count=0,
            reason="every read succeeded",
        )

    @staticmethod
    def _group_read(
        window: tuple[ModelProofRead, ...], group_id: int
    ) -> ModelIdleProofSubject:
        missing = [r for r in window if group_id not in r.groups_read]
        if not window or missing:
            return ModelIdleProofSubject(
                name=group_read_subject(group_id),
                verdict=_U,
                count=len(missing) if window else 0,
                reason=f"group {group_id} was not read in every judged read",
            )
        return ModelIdleProofSubject(
            name=group_read_subject(group_id),
            verdict=_W,
            count=0,
            reason=f"group {group_id} was read in every judged read",
        )

    @staticmethod
    def _group_idle(
        window: tuple[ModelProofRead, ...], group_id: int
    ) -> tuple[ModelIdleProofSubject, list[str]]:
        names = [
            runner.name
            for read in window
            for runner in read.runners
            if runner.group_id == group_id and runner.busy
        ]
        if names:
            return (
                ModelIdleProofSubject(
                    name=group_idle_subject(group_id),
                    verdict=_N,
                    count=len(names),
                    reason=f"{len(names)} busy runner reading(s) in group {group_id}",
                ),
                names,
            )
        return (
            ModelIdleProofSubject(
                name=group_idle_subject(group_id),
                verdict=_W,
                count=0,
                reason=f"no runner of group {group_id} busy in any judged read",
            ),
            [],
        )

    @staticmethod
    def _queued_labels(
        window: tuple[ModelProofRead, ...], stop_set_labels: tuple[str, ...]
    ) -> tuple[ModelIdleProofSubject, list[int]]:
        stop = {label.casefold() for label in stop_set_labels}
        ids = [
            job.job_id
            for read in window
            for job in read.queued_jobs
            if any(label.casefold() in stop for label in job.labels)
        ]
        if ids:
            return (
                ModelIdleProofSubject(
                    name=SUBJECT_QUEUED_STOP_SET_LABEL,
                    verdict=_N,
                    count=len(ids),
                    reason=f"{len(ids)} queued job reading(s) name a stop-set label",
                ),
                ids,
            )
        return (
            ModelIdleProofSubject(
                name=SUBJECT_QUEUED_STOP_SET_LABEL,
                verdict=_W,
                count=0,
                reason="no queued job names a stop-set label",
            ),
            [],
        )
