# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_github_schedule_observer_effect (OMN-20803).

One hourly tick. For each watched repository it:

- checks the clone's head against its remote default branch's head; a stale
  clone makes the repository's workflows UNOBSERVABLE (SOURCE_STALE) and no
  GitHub call is made for it;
- derives each scheduled workflow and its cron from the clone, and reports a
  scheduled workflow with no overlay entry as UNDECLARED;
- reads the scheduled runs (``event=schedule``) from a persisted per-repository
  cursor, fully paginated, and publishes one event per run: started when first
  seen unfinished, finished once it completes, including a run whose
  completion lands after the cursor moved past it (read again by id);
- reads the workflow states once a day and reports a disabled scheduled
  workflow MISSED (WORKFLOW_DISABLED);
- reads the closed pull requests and opens an expectation, as a started run of
  the repository's event-triggered entries, for each runtime-affecting merge
  whose trigger the bus did not carry, and for none that it did.

Plus, when the request names it, the external dead-man's last completed run and
the observer's own heartbeat.

A source that cannot be read is reported UNOBSERVABLE with the reason it maps
to (EVIDENCE_UNREADABLE for an inaccessible repository or a failed read,
READ_INCOMPLETE for an exhausted budget or a truncated listing, SOURCE_STALE
for a stale clone), never as zero runs.

State persists before the events are returned. A publication lost after that
is recovered by the cursors only for what lies ahead of them; run ids are
stable, so a re-read never double-counts downstream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ValidationError

from omnimarket.github_landing.github_landing_transport import (
    UrllibGithubLandingTransport,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.github_quota_floor import (
    ModelGithubQuotaFloor,
)
from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunPhase,
    EnumAutomationTriggerKind,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessOverlay,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
    load_automation_liveness_overlay,
    load_default_automation_liveness_overlay,
)
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.git_schedule_clone_reader import (
    GitScheduleCloneReader,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.github_schedule_reader import (
    PAGE_SIZE,
    GithubScheduleReader,
    ScheduleReadError,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.schedule_observer_events import (
    finished_event,
    heartbeat_event,
    run_id_of,
    started_event,
    undeclared_process_id,
    unobservable_event,
    verdict_event,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.schedule_observer_state_store import (
    FileScheduleObserverStateStore,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models.model_cloned_repository import (
    ModelClonedRepository,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models.model_github_schedule_facts import (
    ModelGithubClosedPull,
    ModelGithubPullFile,
    ModelGithubWorkflow,
    ModelGithubWorkflowRun,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models.model_github_schedule_observer_request import (
    ModelGithubScheduleObserverRequest,
    ModelObservedRepository,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models.model_schedule_observer_state import (
    ModelEmittedRun,
    ModelOutstandingRun,
    ModelRepositoryObserverState,
    ModelScheduleObserverState,
)
from omnimarket.nodes.node_github_schedule_observer_effect.protocols import (
    ProtocolGithubScheduleTransport,
    ProtocolScheduleCloneReader,
    ProtocolScheduleObserverStateStore,
    ScheduleCloneError,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"
#: GitHub ends a run after 35 days, so an older outstanding record is dead.
_OUTSTANDING_MAX_AGE = timedelta(days=35)
_GITHUB_KINDS = frozenset(
    {EnumAutomationTriggerKind.GH_SCHEDULE, EnumAutomationTriggerKind.GH_EVENT}
)
_ACTIVE_WORKFLOW_STATE = "active"


#: What a tick publishes; the runtime routes each class to its contract-declared topic.
SeamEvent = (
    ModelAutomationRunObserved
    | ModelAutomationHeartbeat
    | ModelAutomationLivenessVerdictEvent
)


@dataclass
class _Tick:
    request: ModelGithubScheduleObserverRequest
    overlay: ModelAutomationLivenessOverlay
    state: ModelScheduleObserverState
    reader: GithubScheduleReader
    published: frozenset[str]
    events: list[SeamEvent] = field(default_factory=list)
    reported_runs: set[str] = field(default_factory=set)

    @property
    def now(self) -> datetime:
        return self.request.observed_at


def _workflow_file(entry: ModelAutomationLivenessEntry) -> str:
    return PurePosixPath(entry.trigger.native_id).name


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse[M: BaseModel](model: type[M], items: list[dict[str, object]]) -> list[M]:
    try:
        return [model.model_validate(item) for item in items]
    except ValidationError as exc:
        raise ScheduleReadError(
            EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
            f"unreadable {model.__name__} from GitHub: {exc.error_count()} invalid field(s)",
        ) from None


class HandlerGithubScheduleObserver:
    """EFFECT: observe GitHub scheduled workflows and report them on the seam.

    ``transport``, ``clone_reader`` and ``state_store`` are injected in tests.
    Without a transport, the handler builds the live one on the first read from
    the contract-declared ``GITHUB_TOKEN`` ref.
    """

    def __init__(
        self,
        transport: ProtocolGithubScheduleTransport | None = None,
        *,
        clone_reader: ProtocolScheduleCloneReader | None = None,
        state_store: ProtocolScheduleObserverStateStore | None = None,
        quota_floor: ModelGithubQuotaFloor | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._clone_reader: ProtocolScheduleCloneReader = (
            clone_reader or GitScheduleCloneReader()
        )
        self._state_store = state_store
        self._floor = quota_floor or ModelGithubQuotaFloor.from_contract(_CONTRACT_PATH)
        self._clock = clock
        self.identity = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)

    # --- entry point ----------------------------------------------------------

    async def handle(
        self, request: ModelGithubScheduleObserverRequest
    ) -> tuple[SeamEvent, ...]:
        """Run one tick and return the events it observed, in order."""
        overlay = (
            load_automation_liveness_overlay(Path(request.overlay_path))
            if request.overlay_path
            else load_default_automation_liveness_overlay()
        )
        deadman = request.external_deadman
        if deadman is not None and not any(
            e.process_id == deadman.process_id for e in overlay.processes
        ):
            raise ValueError(
                f"external dead-man {deadman.process_id} has no overlay entry"
            )
        store = self._state_store or FileScheduleObserverStateStore(
            Path(request.state_path)
        )
        state = store.load()
        if state.first_tick_at is None:
            state.first_tick_at = request.observed_at
        tick = _Tick(
            request=request,
            overlay=overlay,
            state=state,
            reader=GithubScheduleReader(
                self._transport_factory,
                quota_floor=self._floor,
                identity=self.identity,
                max_pages=request.max_pages,
                clock=self._clock,
            ),
            published=frozenset(request.published_trigger_keys),
        )
        for repo in request.repositories:
            await self._observe_repository(tick, repo)
        if deadman is not None:
            await self._observe_deadman(tick)
        state.ticks_completed += 1
        if request.observer is not None:
            tick.events.append(
                heartbeat_event(
                    process_id=request.observer.process_id,
                    host=request.observer.host,
                    process_started_at=state.first_tick_at,
                    ticks_completed=state.ticks_completed,
                    observed_at=request.observed_at,
                )
            )
        store.save(state)
        _log.info(
            "github schedule observer: %d events, %d requests",
            len(tick.events),
            tick.reader.requests_sent,
        )
        return tuple(tick.events)

    async def _transport_factory(self) -> ProtocolGithubScheduleTransport:
        if self._transport is not None:
            return self._transport
        # env_var_fallback (OMN-14452): the lane resolver does not serve the
        # GitHub token from the store; the literal container env var does.
        secret = await resolve_api_key_async(
            self.identity, env_var_fallback=self.identity
        )
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {self.identity!r} resolved to None; set it in the secret store"
            )
        return UrllibGithubLandingTransport(secret)

    # --- one repository -------------------------------------------------------

    async def _observe_repository(
        self, tick: _Tick, repo: ModelObservedRepository
    ) -> None:
        repo_name = repo.repository.split("/", 1)[1]
        entries = tuple(
            e
            for e in tick.overlay.processes
            if e.owner_repo == repo.repository
            and e.trigger.kind in _GITHUB_KINDS
            and e.state is EnumAutomationProcessState.ACTIVE
        )
        try:
            clone = await asyncio.to_thread(
                self._clone_reader.read_clone, Path(tick.request.clone_root) / repo_name
            )
        except ScheduleCloneError as exc:
            self._unobservable(
                tick,
                entries,
                EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
                str(exc),
            )
            return
        if not clone.is_current:
            self._unobservable(
                tick,
                entries,
                EnumAutomationLivenessReason.SOURCE_STALE,
                f"clone head {clone.head_sha[:12]} differs from the remote default "
                f"branch {clone.default_branch} head {clone.remote_default_head_sha[:12]}",
            )
            return
        self._report_undeclared(tick, repo, clone)
        repo_state = tick.state.repositories.setdefault(
            repo.repository, ModelRepositoryObserverState()
        )
        schedule_entries = tuple(
            e
            for e in entries
            if e.trigger.kind is EnumAutomationTriggerKind.GH_SCHEDULE
        )
        event_entries = tuple(
            e for e in entries if e.trigger.kind is EnumAutomationTriggerKind.GH_EVENT
        )
        if schedule_entries:
            await self._observe_schedule(tick, repo, repo_state, schedule_entries)
        if event_entries and repo.runtime_paths:
            await self._reconcile_triggers(
                tick, repo, repo_state, event_entries, clone.default_branch
            )

    def _unobservable(
        self,
        tick: _Tick,
        entries: tuple[ModelAutomationLivenessEntry, ...],
        reason: EnumAutomationLivenessReason,
        detail: str,
    ) -> None:
        for entry in entries:
            tick.events.append(
                unobservable_event(entry, reason, detail, observed_at=tick.now)
            )

    def _report_undeclared(
        self, tick: _Tick, repo: ModelObservedRepository, clone: ModelClonedRepository
    ) -> None:
        declared = {
            _workflow_file(e)
            for e in tick.overlay.processes
            if e.owner_repo == repo.repository and e.trigger.kind in _GITHUB_KINDS
        }
        for workflow in clone.workflows:
            file_name = PurePosixPath(workflow.path).name
            if file_name in declared:
                continue
            tick.events.append(
                verdict_event(
                    None,
                    process_id=undeclared_process_id(repo.repository, file_name),
                    host="github",
                    verdict=EnumAutomationLivenessVerdict.UNDECLARED,
                    reason=EnumAutomationLivenessReason.PROCESS_WITHOUT_ENTRY,
                    detail=f"{repo.repository} {workflow.path} is scheduled "
                    f"({'; '.join(workflow.crons)}) and has no overlay entry",
                    observed_at=tick.now,
                )
            )

    # --- scheduled runs -------------------------------------------------------

    async def _observe_schedule(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        entries: tuple[ModelAutomationLivenessEntry, ...],
    ) -> None:
        demanding = tuple(e for e in entries if e.declares_demand)
        self._unobservable(
            tick,
            demanding,
            EnumAutomationLivenessReason.DEMAND_UNREADABLE,
            "the entry declares a demand source and the GitHub observer reads none",
        )
        readable = tuple(e for e in entries if not e.declares_demand)
        if not readable:
            return
        by_file: dict[str, list[ModelAutomationLivenessEntry]] = {}
        for entry in readable:
            by_file.setdefault(_workflow_file(entry), []).append(entry)
        failures: list[ScheduleReadError] = []
        steps: tuple[Callable[[], Awaitable[None]], ...] = (
            lambda: self._read_workflow_states(tick, repo, repo_state, by_file),
            lambda: self._read_runs(tick, repo, repo_state, by_file),
            lambda: self._read_outstanding(tick, repo, repo_state, readable),
        )
        for step in steps:
            try:
                await step()
            except ScheduleReadError as exc:
                failures.append(exc)
        if failures:
            self._unobservable(
                tick,
                readable,
                failures[0].reason,
                "; ".join(f.detail for f in failures),
            )

    async def _read_workflow_states(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        by_file: dict[str, list[ModelAutomationLivenessEntry]],
    ) -> None:
        read_at = repo_state.workflow_states_read_at
        interval = timedelta(seconds=tick.request.workflow_state_interval_seconds)
        if read_at is not None and tick.now - read_at < interval:
            return
        raw = await tick.reader.collect(
            f"/repos/{repo.repository}/actions/workflows?per_page={PAGE_SIZE}",
            key="workflows",
        )
        for workflow in _parse(ModelGithubWorkflow, raw):
            if workflow.state == _ACTIVE_WORKFLOW_STATE:
                continue
            for entry in by_file.get(workflow.workflow_file, ()):
                tick.events.append(
                    verdict_event(
                        entry,
                        process_id=entry.process_id,
                        host=entry.host,
                        verdict=EnumAutomationLivenessVerdict.MISSED,
                        reason=EnumAutomationLivenessReason.WORKFLOW_DISABLED,
                        detail=f"{repo.repository} {workflow.path} is {workflow.state}",
                        observed_at=tick.now,
                    )
                )
        repo_state.workflow_states_read_at = tick.now

    async def _read_runs(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        by_file: dict[str, list[ModelAutomationLivenessEntry]],
    ) -> None:
        cursor = repo_state.runs_cursor or tick.now - timedelta(
            seconds=tick.request.initial_lookback_seconds
        )
        raw = await tick.reader.collect(
            f"/repos/{repo.repository}/actions/runs?event=schedule"
            f"&created=%3E%3D{_utc(cursor)}&per_page={PAGE_SIZE}",
            key="workflow_runs",
        )
        newest = cursor
        for run in sorted(
            _parse(ModelGithubWorkflowRun, raw), key=lambda r: (r.created_at, r.id)
        ):
            newest = max(newest, run.created_at)
            for entry in by_file.get(run.workflow_file, ()):
                self._report_run(tick, repo, repo_state, entry, run)
        repo_state.runs_cursor = newest
        repo_state.emitted = {
            run_id: emitted
            for run_id, emitted in repo_state.emitted.items()
            if emitted.created_at >= newest
        }

    def _report_run(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        entry: ModelAutomationLivenessEntry,
        run: ModelGithubWorkflowRun,
    ) -> None:
        run_id = run_id_of(entry, str(run.id))
        tick.reported_runs.add(run_id)
        prior = repo_state.emitted.get(run_id)
        evidence_ref = f"{repo.repository}/actions/runs/{run.id}"
        if run.status == "completed":
            if prior is not None and prior.phase is EnumAutomationRunPhase.FINISHED:
                return
            tick.events.append(
                finished_event(
                    entry, run, evidence_ref=evidence_ref, observed_at=tick.now
                )
            )
            repo_state.emitted[run_id] = ModelEmittedRun(
                phase=EnumAutomationRunPhase.FINISHED, created_at=run.created_at
            )
            repo_state.outstanding.pop(run_id, None)
            return
        if prior is None:
            tick.events.append(
                started_event(
                    entry,
                    run_key=str(run.id),
                    started_at=run.run_started_at or run.created_at,
                    work_unit=None,
                    evidence_ref=evidence_ref,
                    observed_at=tick.now,
                )
            )
            repo_state.emitted[run_id] = ModelEmittedRun(
                phase=EnumAutomationRunPhase.STARTED, created_at=run.created_at
            )
        repo_state.outstanding[run_id] = ModelOutstandingRun(
            process_id=entry.process_id,
            github_run_id=run.id,
            created_at=run.created_at,
        )

    async def _read_outstanding(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        entries: tuple[ModelAutomationLivenessEntry, ...],
    ) -> None:
        """Read by id each run first seen unfinished that this tick's listing did not settle."""
        by_process = {e.process_id: e for e in entries}
        failures: list[ScheduleReadError] = []
        for run_id, outstanding in list(repo_state.outstanding.items()):
            if run_id in tick.reported_runs:
                continue
            entry = by_process.get(outstanding.process_id)
            if (
                entry is None
                or tick.now - outstanding.created_at > _OUTSTANDING_MAX_AGE
            ):
                del repo_state.outstanding[run_id]
                continue
            try:
                response = await tick.reader.get(
                    f"/repos/{repo.repository}/actions/runs/{outstanding.github_run_id}"
                )
                run = ModelGithubWorkflowRun.model_validate(response.body or {})
            except ValidationError:
                failures.append(
                    ScheduleReadError(
                        EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
                        f"unreadable run {outstanding.github_run_id} of {repo.repository}",
                    )
                )
                continue
            except ScheduleReadError as exc:
                if exc.http_status == 404:
                    del repo_state.outstanding[run_id]
                failures.append(exc)
                continue
            self._report_run(tick, repo, repo_state, entry, run)
        if failures:
            raise failures[0]

    # --- lost triggers --------------------------------------------------------

    async def _reconcile_triggers(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        entries: tuple[ModelAutomationLivenessEntry, ...],
        default_branch: str,
    ) -> None:
        try:
            newest = await self._read_closed_pulls(
                tick, repo, repo_state, entries, default_branch
            )
        except ScheduleReadError as exc:
            self._unobservable(tick, entries, exc.reason, exc.detail)
            return
        repo_state.closed_pr_cursor = newest
        repo_state.examined_merges = {
            sha: merged_at
            for sha, merged_at in repo_state.examined_merges.items()
            if merged_at >= newest
        }

    async def _read_closed_pulls(
        self,
        tick: _Tick,
        repo: ModelObservedRepository,
        repo_state: ModelRepositoryObserverState,
        entries: tuple[ModelAutomationLivenessEntry, ...],
        default_branch: str,
    ) -> datetime:
        cursor = repo_state.closed_pr_cursor or tick.now - timedelta(
            seconds=tick.request.initial_lookback_seconds
        )
        merged: list[ModelGithubClosedPull] = []
        async for page in tick.reader.pages(
            f"/repos/{repo.repository}/pulls?state=closed&sort=updated"
            f"&direction=desc&per_page={PAGE_SIZE}",
            key=None,
        ):
            pulls = _parse(ModelGithubClosedPull, page.items)
            merged.extend(
                p
                for p in pulls
                if p.merged_at is not None
                and p.merged_at >= cursor
                and p.base.ref == default_branch
            )
            # Sorted by update time, newest first, and a merge is an update.
            if any(p.updated_at < cursor for p in pulls):
                break
        newest = cursor
        for pull in sorted(merged, key=lambda p: (p.merged_at or cursor, p.number)):
            merged_at = pull.merged_at
            sha = pull.merge_commit_sha
            if merged_at is None or sha is None or sha in repo_state.examined_merges:
                continue
            if sha not in tick.published and await self._is_runtime_affecting(
                tick, repo, pull.number
            ):
                for entry in entries:
                    tick.events.append(
                        started_event(
                            entry,
                            run_key=sha,
                            started_at=merged_at,
                            work_unit=f"{repo.repository}#{pull.number}",
                            evidence_ref=f"{repo.repository}/pull/{pull.number}",
                            observed_at=tick.now,
                        )
                    )
            repo_state.examined_merges[sha] = merged_at
            newest = max(newest, merged_at)
        return newest

    async def _is_runtime_affecting(
        self, tick: _Tick, repo: ModelObservedRepository, number: int
    ) -> bool:
        raw = await tick.reader.collect(
            f"/repos/{repo.repository}/pulls/{number}/files?per_page={PAGE_SIZE}",
            key=None,
        )
        return any(
            file.filename.startswith(prefix)
            for file in _parse(ModelGithubPullFile, raw)
            for prefix in repo.runtime_paths
        )

    # --- external dead-man ----------------------------------------------------

    async def _observe_deadman(self, tick: _Tick) -> None:
        deadman = tick.request.external_deadman
        if deadman is None:
            return
        entry = next(
            e for e in tick.overlay.processes if e.process_id == deadman.process_id
        )
        try:
            response = await tick.reader.get(
                f"/repos/{deadman.repository}/actions/workflows/"
                f"{deadman.workflow_file}/runs?status=completed&per_page=1"
            )
            listed = (response.body or {}).get("workflow_runs")
            if not isinstance(listed, list):
                raise ScheduleReadError(
                    EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
                    f"unexpected body shape from {deadman.repository} workflow runs",
                )
            runs = _parse(
                ModelGithubWorkflowRun, [i for i in listed if isinstance(i, dict)]
            )
        except ScheduleReadError as exc:
            self._unobservable(tick, (entry,), exc.reason, exc.detail)
            return
        if not runs:
            return
        latest = max(runs, key=lambda r: (r.created_at, r.id))
        if tick.state.external_deadman_last_run_id == str(latest.id):
            return
        tick.events.append(
            finished_event(
                entry,
                latest,
                evidence_ref=f"{deadman.repository}/actions/runs/{latest.id}",
                observed_at=tick.now,
            )
        )
        tick.state.external_deadman_last_run_id = str(latest.id)
