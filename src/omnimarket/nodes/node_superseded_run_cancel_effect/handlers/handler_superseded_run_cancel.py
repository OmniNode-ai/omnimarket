# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_superseded_run_cancel_effect (OMN-17427).

One sweep request in, one result out. The handler reads the repository's queued
and in-progress runs, then (after the run list, so every listed merge-group run
had its queue entry before the read) the merge queue of each base branch a
merge_group run builds for, decides with :func:`decide_superseded_runs` from
those two reads alone, and cancels what it selects. A run list that cannot be
read cancels nothing. A cancel GitHub refuses (the run finished first) is
reported in the result, not raised.

Every GitHub call goes through the shared landing transport
(``omnimarket.github_landing``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    cancel_run_request,
    merge_queue_entries_request,
    workflow_runs_request,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
)
from omnimarket.nodes.node_superseded_run_cancel_effect.handlers.decide_superseded_runs import (
    decide_superseded_runs,
    merge_group_bases,
    unjudged_merge_group_runs,
)
from omnimarket.nodes.node_superseded_run_cancel_effect.models import (
    ModelMergeQueueRead,
    ModelSupersededRunCancelledEvent,
    ModelSupersededRunSkip,
    ModelSupersededRunSweepRequest,
    ModelSupersededRunSweepResult,
    ModelWorkflowRunFact,
)
from omnimarket.nodes.node_superseded_run_cancel_effect.protocols import (
    ProtocolEventPublisher,
    ProtocolRunCancelTransport,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"
# Read in this order: a run that moves queued -> in_progress between the two
# reads shows up in the second, so no live run slips both.
_LIVE_STATUSES = ("queued", "in_progress")
# A runaway pagination guard: 10 pages of 100 is far past any real run list.
_MAX_PAGES = 10
_CANCEL_ACCEPTED = 202


class _RunListUnreadError(Exception):
    """Internal: the run list could not be read."""


class HandlerSupersededRunCancel:
    """EFFECT: cancel the queued and in-progress runs a later push or a queue exit superseded."""

    def __init__(
        self,
        transport: ProtocolRunCancelTransport | None = None,
        event_bus: ProtocolEventPublisher | None = None,
    ) -> None:
        self._transport = transport
        self._event_bus = event_bus
        topics = contract_publish_topics(_CONTRACT_PATH)
        self._cancelled_topic = next(t for t in topics if "-cancelled." in t)
        self._completed_topic = next(t for t in topics if "-sweep-completed." in t)

    async def handle(
        self, request: ModelSupersededRunSweepRequest
    ) -> ModelSupersededRunSweepResult:
        transport = self._transport or await self._live_transport()
        try:
            runs = await self._read_runs(transport, request.repo)
        except (_RunListUnreadError, GithubLandingTransportError) as exc:
            _log.error(
                "superseded-run sweep %s: run list unread: %s", request.repo, exc
            )
            unread = ModelSupersededRunSweepResult(
                correlation_id=request.correlation_id,
                repo=request.repo,
                dry_run=request.dry_run,
                runs_read=0,
                read_error=str(exc),
            )
            await self._publish_result(unread)
            return unread
        queues = [
            await self._read_queue(transport, request.repo, base)
            for base in merge_group_bases(runs)
        ]
        selected = decide_superseded_runs(runs, queues)
        cancelled: list[ModelSupersededRunCancelledEvent] = []
        refused: list[ModelSupersededRunSkip] = []
        for run in selected:
            if not request.dry_run:
                status, detail = await self._cancel(transport, request.repo, run.run_id)
                if status != _CANCEL_ACCEPTED:
                    refused.append(
                        ModelSupersededRunSkip(
                            run_id=run.run_id, http_status=status, detail=detail
                        )
                    )
                    continue
            cancelled.append(
                ModelSupersededRunCancelledEvent(
                    correlation_id=request.correlation_id,
                    repo=request.repo,
                    run_id=run.run_id,
                    reason=run.reason,
                    workflow_name=run.workflow_name,
                    event=run.event,
                    head_sha=run.head_sha,
                    pr_number=run.pr_number,
                    current_head_sha=run.current_head_sha,
                    base_branch=run.base_branch,
                    dry_run=request.dry_run,
                )
            )
        _log.info(
            "superseded-run sweep %s: read=%d cancelled=%d refused=%d dry_run=%s",
            request.repo,
            len(runs),
            len(cancelled),
            len(refused),
            request.dry_run,
        )
        result = ModelSupersededRunSweepResult(
            correlation_id=request.correlation_id,
            repo=request.repo,
            dry_run=request.dry_run,
            runs_read=len(runs),
            cancelled=tuple(cancelled),
            not_cancelled=tuple(refused),
            unjudged_merge_group_runs=unjudged_merge_group_runs(runs, queues),
        )
        await self._publish_result(result)
        return result

    async def _publish_result(self, result: ModelSupersededRunSweepResult) -> None:
        """One cancelled event per cancel, then the sweep's completed event.

        A publish failure is logged and never undoes a cancel already sent.
        """
        if self._event_bus is None:
            return
        events: list[tuple[str, BaseModel]] = [
            (self._cancelled_topic, event) for event in result.cancelled
        ]
        events.append((self._completed_topic, result))
        for topic, event in events:
            try:
                await self._event_bus.publish(
                    topic,
                    key=result.repo.encode(),
                    value=event.model_dump_json().encode(),
                )
            except Exception:
                _log.exception("superseded-run sweep: failed to publish on %s", topic)

    async def _live_transport(self) -> UrllibGithubLandingTransport:
        ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
        secret = await resolve_api_key_async(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {ref!r} resolved to None; set it in the secret store"
            )
        return UrllibGithubLandingTransport(secret)

    async def _read_runs(
        self, transport: ProtocolRunCancelTransport, repo: str
    ) -> list[ModelWorkflowRunFact]:
        by_id: dict[int, ModelWorkflowRunFact] = {}
        for status in _LIVE_STATUSES:
            for page in range(1, _MAX_PAGES + 1):
                request = workflow_runs_request(
                    repo, per_page=LIST_PAGE_SIZE, status=status
                )
                paged = request.model_copy(
                    update={"path": f"{request.path}&page={page}"}
                )
                response = await transport.send(paged)
                if response.status != 200:
                    raise _RunListUnreadError(
                        f"{status} run list page {page}: {response.message()}"
                    )
                entries = (response.body or {}).get("workflow_runs")
                if not isinstance(entries, list):
                    raise _RunListUnreadError(
                        f"{status} run list page {page}: no workflow_runs"
                    )
                for entry in entries:
                    fact = (
                        ModelWorkflowRunFact.from_github(entry)
                        if isinstance(entry, dict)
                        else None
                    )
                    if fact is not None:
                        by_id[fact.run_id] = fact
                if len(entries) < LIST_PAGE_SIZE:
                    break
        return sorted(by_id.values(), key=lambda r: r.run_id)

    async def _read_queue(
        self, transport: ProtocolRunCancelTransport, repo: str, base: str
    ) -> ModelMergeQueueRead:
        try:
            response = await transport.send(merge_queue_entries_request(repo, base))
        except GithubLandingTransportError:
            return ModelMergeQueueRead(base_branch=base, complete=False)
        if response.status != 200:
            return ModelMergeQueueRead(base_branch=base, complete=False)
        return ModelMergeQueueRead.from_github(base, response.body)

    async def _cancel(
        self, transport: ProtocolRunCancelTransport, repo: str, run_id: int
    ) -> tuple[int, str]:
        try:
            response = await transport.send(cancel_run_request(repo, run_id))
        except GithubLandingTransportError as exc:
            return 0, str(exc)
        return (
            response.status,
            "" if response.status == _CANCEL_ACCEPTED else response.message(),
        )
