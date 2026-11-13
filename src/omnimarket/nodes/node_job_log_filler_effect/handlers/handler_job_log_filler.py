# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: fill the job-log cache from the merge-relevant runs of open pull requests (OMN-23032).

A merge-relevant run is a job of an open pull request's *current* head that decides its
merge: a red job, or a job the overlay names. They come from the PR watcher's state (the
mirror lanes read), never from the GitHub API. One reader call fetches each run's log
into the gateway's job-log cache, after a cache-only probe that skips a log already held,
so a pass makes one GitHub read per missing log and none for a log it holds. Lane
``job-log`` reads then answer from the cache.

Distinct from node_repo_state_query_effect (OMN-23033), whose ``job_log`` want answers a
job's *record* from the same watcher state: that node never fetches a log.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_github_gateway_io import ModelJobLogTailResult
from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
    cache_policy,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    BusJobLogReader,
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_red_facts import (
    RedFactsStore,
    build_record,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    EnumJobLogFillOutcome,
    EnumJobLogFillReason,
    EnumJobLogFillStatus,
    ModelJobLogFillerOverlay,
    ModelJobLogFillItem,
    ModelJobLogFillRequest,
    ModelJobLogFillResult,
    ModelJobLogHold,
    ModelMergeRelevantRun,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
    JobLogNotFoundError,
    ProtocolJobLogReader,
)

_HANDLER_ID = "node_job_log_filler_effect"


def load_overlay(path: Path | None = None) -> ModelJobLogFillerOverlay:
    import yaml

    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    return load_structured_overlay(
        OVERLAY_ID,
        ModelJobLogFillerOverlay,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )


def build_reader(
    overlay: ModelJobLogFillerOverlay, profile_name: str | None
) -> ProtocolJobLogReader:
    """The reader of the named profile, every setting from the overlay."""
    profile = overlay.profile(profile_name)
    if profile.kind == "kafka":
        if profile.lane is None:
            raise ValueError("a kafka profile names its lane")
        return BusJobLogReader(
            lane=profile.lane,
            timeout_seconds=overlay.reply_timeout_seconds,
            tail_bytes=overlay.reply_tail_bytes,
        )
    policy = cache_policy()
    assert policy.root is not None
    return InMemoryJobLogReader(
        policy.root,
        ttl_seconds=policy.ttl_seconds,
        tail_bytes=overlay.reply_tail_bytes,
    )


def read_watcher_state(overlay: ModelJobLogFillerOverlay) -> Mapping[str, Any]:
    path = os.environ.get(overlay.watcher_state_env, "").strip()
    if not path:
        raise ValueError(
            f"the overlay names {overlay.watcher_state_env} for the PR watcher state, "
            "and it is unset on this host"
        )
    state: Mapping[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return state


def select_merge_relevant_runs(
    state: Mapping[str, Any],
    overlay: ModelJobLogFillerOverlay,
    repos: tuple[str, ...] = (),
) -> tuple[list[ModelMergeRelevantRun], int]:
    """The runs that decide a merge, and how many pull requests had a stale CI record.

    Open, non-draft pull requests in the overlay's classes whose CI record is of their
    current head. Per job name the latest completed attempt's id is the one read: the
    watcher keeps the job id of each name in ``ci.detail``.

    The list is ordered for the per-pass cap: red jobs before named ones, and within each the
    most recently completed first. A backlog of old reds (a first pass over every open pull
    request) therefore never holds a fresh failure behind it; the fresh failure is filled in
    the pass after it appears.
    """
    wanted = {r.lower() for r in repos}
    ranked: list[tuple[int, str, ModelMergeRelevantRun]] = []
    stale = 0
    for record in (state.get("prs") or {}).values():
        facts = record.get("facts") or {}
        ci = record.get("ci")
        if (
            record.get("cls") not in overlay.pr_classes
            or facts.get("state") != "OPEN"
            or facts.get("draft")
            or not ci
        ):
            continue
        repo = f"{overlay.github_owner}/{facts['repo']}"
        if wanted and repo.lower() not in wanted:
            continue
        if ci.get("sha") != facts.get("head_sha"):
            stale += 1
            continue
        latest: dict[str, tuple[str, int]] = {}
        for name, job_id, completed_at in ci.get("detail") or []:
            if not str(job_id).isdigit():
                continue
            if name not in latest or completed_at >= latest[name][0]:
                latest[name] = (completed_at, int(job_id))
        red = set(ci.get("red") or [])
        for name, (completed_at, job_id) in sorted(latest.items()):
            reason = (
                EnumJobLogFillReason.RED
                if name in red
                else EnumJobLogFillReason.NAMED
                if name in overlay.always_job_names
                else None
            )
            if reason is None:
                continue
            ranked.append(
                (
                    0 if reason is EnumJobLogFillReason.RED else 1,
                    str(completed_at),
                    ModelMergeRelevantRun(
                        repo=repo,
                        pr_number=int(facts["number"]),
                        head_sha=str(facts["head_sha"]),
                        job_name=name,
                        job_id=job_id,
                        reason=reason,
                    ),
                )
            )
    # Stable two-key order: newest completion first, then red before named.
    ranked.sort(key=lambda item: item[1], reverse=True)
    ranked.sort(key=lambda item: item[0])
    return [run for _, _, run in ranked], stale


class HandlerJobLogFiller:
    """EFFECT: one pass reads the watcher state and fills the cache, one reader call per run."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        *,
        overlay: ModelJobLogFillerOverlay | None = None,
        reader_factory: Callable[[str | None], ProtocolJobLogReader] | None = None,
        state_reader: Callable[[], Mapping[str, Any]] | None = None,
        held: MutableMapping[int, float] | None = None,
        not_found: MutableMapping[int, ModelJobLogHold] | None = None,
        clock: Callable[[], float] = time.time,
        record_store: RedFactsStore | None = None,
    ) -> None:
        self._record_store = record_store
        self._held: MutableMapping[int, float] = {} if held is None else held
        self._not_found: MutableMapping[int, ModelJobLogHold] = (
            {} if not_found is None else not_found
        )
        self._clock = clock
        self._overlay = overlay
        self._reader_factory = reader_factory
        self._state_reader = state_reader

    @property
    def held(self) -> MutableMapping[int, float]:
        """Job id -> when a pass last confirmed its log cached; the tick keeps it between passes."""
        return self._held

    @property
    def not_found(self) -> MutableMapping[int, ModelJobLogHold]:
        """Job id -> when a pass last saw its log answered HTTP 404 and why; the tick keeps it between passes."""
        return self._not_found

    @property
    def overlay(self) -> ModelJobLogFillerOverlay:
        if self._overlay is None:
            self._overlay = load_overlay()
        return self._overlay

    def fill(self, request: ModelJobLogFillRequest) -> ModelJobLogFillResult:
        overlay = self.overlay
        overlay.profile(request.profile)  # an unknown profile refuses before any read
        profile_name = request.profile or overlay.default_profile
        try:
            state = (
                self._state_reader()
                if self._state_reader
                else read_watcher_state(overlay)
            )
            reader = (
                self._reader_factory(request.profile)
                if self._reader_factory
                else build_reader(overlay, request.profile)
            )
            store = self._record_store or RedFactsStore.from_overlay(overlay)
        except Exception as exc:
            return ModelJobLogFillResult(
                correlation_id=request.correlation_id,
                status=EnumJobLogFillStatus.FAILED,
                profile=profile_name,
                considered=0,
                filled=0,
                already_cached=0,
                failed=0,
                deferred=0,
                stale_skipped=0,
                error=f"{type(exc).__name__}: {exc}"[-2000:],
            )
        runs, stale = select_merge_relevant_runs(state, overlay, request.repos)
        cap = min(
            overlay.max_reads_per_pass, request.max_reads or overlay.max_reads_per_pass
        )
        # One worker takes one run at a time, newest failure first: it probes the cache, and on a miss reads the
        # log at once, so the newest failures are filled before any older run is probed. Two bounds end the work:
        # the read cap (GitHub reads) and the pass budget (wall clock; a probe over the bus is a round trip the
        # gateway answers one at a time, measured 2026-11-09 on h051 at a median 2.4 s with six readers, so
        # probing 886 runs before the first fill never ended inside the unit's limit). A run the pass did not
        # reach is deferred and is the next pass's first work, because the held memo skips what this one confirmed.
        items: list[ModelJobLogFillItem] = []
        lock = threading.Lock()
        live_reads = 0
        deferred = 0
        now = self._clock()
        deadline = now + overlay.pass_budget_seconds
        budget_exhausted = False
        memo = self._held
        records_written = 0
        records_failed = 0
        record_stamp = datetime.fromtimestamp(now, UTC)

        def needs_record(run: ModelMergeRelevantRun) -> bool:
            """A red job with no record yet; its log is read again even when the memo holds it."""
            return run.reason is EnumJobLogFillReason.RED and not store.has(
                run.repo, run.pr_number, run.head_sha, run.job_id
            )

        remembered = [
            r
            for r in runs
            if now - memo.get(r.job_id, -math.inf) < overlay.reprobe_seconds
            and not needs_record(r)
        ]
        remembered_ids = {r.job_id for r in remembered}
        # A job whose log the gateway answered HTTP 404 recently enough is not probed again
        # until the overlay's hold interval has passed: the answer would be the same 404, and
        # one read per run per pass is the budget the pass has. No setting, no hold.
        hold_seconds = overlay.not_found_hold_seconds
        parked = (
            [
                r
                for r in runs
                if r.job_id in self._not_found
                and r.job_id not in remembered_ids
                and now - self._not_found[r.job_id].held_at < hold_seconds
            ]
            if hold_seconds is not None
            else []
        )
        parked_ids = {r.job_id for r in parked}
        to_probe = [
            r
            for r in runs
            if r.job_id not in remembered_ids and r.job_id not in parked_ids
        ]
        items.extend(
            ModelJobLogFillItem(run=r, outcome=EnumJobLogFillOutcome.ALREADY_CACHED)
            for r in remembered
        )
        items.extend(
            ModelJobLogFillItem(
                run=r,
                outcome=EnumJobLogFillOutcome.HELD_NOT_FOUND,
                error=self._not_found[r.job_id].cause,
            )
            for r in parked
        )

        def record(run: ModelMergeRelevantRun, log: ModelJobLogTailResult) -> None:
            nonlocal records_written, records_failed
            try:
                store.write(
                    build_record(
                        repo=run.repo,
                        pr_number=run.pr_number,
                        head_sha=run.head_sha,
                        job_name=run.job_name,
                        job_id=run.job_id,
                        text=log.text,
                        total_bytes=log.total_bytes,
                        cache_path=reader.cache_path,
                        overlay=overlay,
                        now=record_stamp,
                    )
                )
                with lock:
                    records_written += 1
            except Exception:
                with lock:
                    records_failed += 1

        def work(run: ModelMergeRelevantRun) -> ModelJobLogFillItem | None:
            nonlocal live_reads, deferred, budget_exhausted
            tail = overlay.red_facts_tail_bytes if needs_record(run) else None
            with lock:
                if self._clock() >= deadline:
                    budget_exhausted = True
                    deferred += 1
                    return None
            try:
                cached = reader.read(
                    run.repo, run.job_id, cache_only=True, tail_bytes=tail
                )
                if tail is not None:
                    record(run, cached)
                return ModelJobLogFillItem(
                    run=run,
                    outcome=EnumJobLogFillOutcome.ALREADY_CACHED,
                    total_bytes=cached.total_bytes,
                )
            except JobLogCacheMissError:
                pass
            except Exception as exc:
                return self._failed(run, exc)
            with lock:
                if live_reads >= cap:
                    deferred += 1
                    return None
                live_reads += 1
            try:
                filled = reader.read(
                    run.repo, run.job_id, cache_only=False, tail_bytes=tail
                )
            except JobLogNotFoundError as exc:
                # The first 404 still counts failed once; the hold starts here so the next
                # pass parks the job instead of reading the same missing log again.
                if overlay.not_found_hold_seconds is not None:
                    with lock:
                        self._not_found[run.job_id] = ModelJobLogHold(
                            held_at=now,
                            cause=f"{type(exc).__name__}: {exc}"[-500:],
                        )
                return self._failed(run, exc)
            except Exception as exc:
                return self._failed(run, exc)
            if tail is not None:
                record(run, filled)
            return ModelJobLogFillItem(
                run=run,
                outcome=EnumJobLogFillOutcome.FILLED,
                total_bytes=filled.total_bytes,
            )

        with ThreadPoolExecutor(max_workers=overlay.reader_concurrency) as pool:
            items.extend(i for i in pool.map(work, to_probe) if i is not None)
        # What this pass confirmed held is remembered, what it did not is forgotten, and a job no longer
        # in play is dropped, so the memo never outgrows the open pull requests.
        confirmed = {
            i.run.job_id
            for i in items
            if i.outcome is not EnumJobLogFillOutcome.FAILED
            and i.outcome is not EnumJobLogFillOutcome.HELD_NOT_FOUND
        }
        current = {r.job_id for r in runs}
        for job_id in [j for j in memo if j not in current or j not in confirmed]:
            del memo[job_id]
        # A 404 hold ends when its job is no longer merge-relevant, or when a pass read the
        # log for real again (filled or found cached), so the holds never outgrow the runs.
        for job_id in [
            j for j in self._not_found if j not in current or j in confirmed
        ]:
            del self._not_found[job_id]
        for job_id in confirmed - remembered_ids:
            memo[job_id] = now
        filled = sum(i.outcome is EnumJobLogFillOutcome.FILLED for i in items)
        cached = sum(i.outcome is EnumJobLogFillOutcome.ALREADY_CACHED for i in items)
        failed = sum(i.outcome is EnumJobLogFillOutcome.FAILED for i in items)
        status = (
            EnumJobLogFillStatus.COMPLETED
            if not failed
            else EnumJobLogFillStatus.FAILED
            if not (filled or cached)
            else EnumJobLogFillStatus.PARTIAL
        )
        return ModelJobLogFillResult(
            correlation_id=request.correlation_id,
            status=status,
            profile=profile_name,
            watcher_tick=str(state.get("last_tick"))
            if state.get("last_tick")
            else None,
            cache_path=reader.cache_path,
            considered=len(runs),
            filled=filled,
            already_cached=cached,
            failed=failed,
            deferred=deferred,
            held_not_found=sum(
                i.outcome is EnumJobLogFillOutcome.HELD_NOT_FOUND for i in items
            ),
            budget_exhausted=budget_exhausted,
            stale_skipped=stale,
            records_written=records_written,
            records_failed=records_failed,
            items=tuple(items),
        )

    @staticmethod
    def _failed(run: ModelMergeRelevantRun, exc: Exception) -> ModelJobLogFillItem:
        return ModelJobLogFillItem(
            run=run,
            outcome=EnumJobLogFillOutcome.FAILED,
            error=f"{type(exc).__name__}: {exc}"[-2000:],
        )

    async def handle(self, request: ModelJobLogFillRequest) -> ModelHandlerOutput[None]:
        """Run one pass off the event loop and emit its typed result as the terminal event."""
        result = await asyncio.to_thread(self.fill, request)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=(result,),
        )


__all__ = [
    "HandlerJobLogFiller",
    "build_reader",
    "load_overlay",
    "read_watcher_state",
    "select_merge_relevant_runs",
]
