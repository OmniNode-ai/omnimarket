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
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
    cache_policy,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    BusJobLogReader,
    InMemoryJobLogReader,
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
    ModelMergeRelevantRun,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
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
    """
    wanted = {r.lower() for r in repos}
    runs: list[ModelMergeRelevantRun] = []
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
        for name, (_, job_id) in sorted(latest.items()):
            reason = (
                EnumJobLogFillReason.RED
                if name in red
                else EnumJobLogFillReason.NAMED
                if name in overlay.always_job_names
                else None
            )
            if reason is None:
                continue
            runs.append(
                ModelMergeRelevantRun(
                    repo=repo,
                    pr_number=int(facts["number"]),
                    head_sha=str(facts["head_sha"]),
                    job_name=name,
                    job_id=job_id,
                    reason=reason,
                )
            )
    return runs, stale


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
    ) -> None:
        self._overlay = overlay
        self._reader_factory = reader_factory
        self._state_reader = state_reader

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
        # Runs whose log is cached cost one local probe; only the missing ones count
        # against the cap, so the cap bounds GitHub reads.
        items: list[ModelJobLogFillItem] = []
        live_reads = 0
        deferred = 0
        todo: list[ModelMergeRelevantRun] = []
        for run in runs:
            try:
                result = reader.read(run.repo, run.job_id, cache_only=True)
            except JobLogCacheMissError:
                if live_reads >= cap:
                    deferred += 1
                    continue
                live_reads += 1
                todo.append(run)
            except Exception as exc:
                items.append(self._failed(run, exc))
            else:
                items.append(
                    ModelJobLogFillItem(
                        run=run,
                        outcome=EnumJobLogFillOutcome.ALREADY_CACHED,
                        total_bytes=result.total_bytes,
                    )
                )

        def one(run: ModelMergeRelevantRun) -> ModelJobLogFillItem:
            try:
                result = reader.read(run.repo, run.job_id, cache_only=False)
            except Exception as exc:
                return self._failed(run, exc)
            return ModelJobLogFillItem(
                run=run,
                outcome=EnumJobLogFillOutcome.FILLED,
                total_bytes=result.total_bytes,
            )

        with ThreadPoolExecutor(max_workers=overlay.reader_concurrency) as pool:
            items.extend(pool.map(one, todo))
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
            stale_skipped=stale,
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
