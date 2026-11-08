# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRepoStateQuery: answer a repository state query from this host's mirror (OMN-23033).

Canonical handler: ``handle(request: ModelRepoStateQuery) -> ModelRepoStateAnswer``.

Three steps, always in this order:

1. COMPARE the mirror with the latest state the bus has carried for what the query wants
   (a branch head against the canonical clone; a pull request's last event against the PR
   watcher's read time). Local reads only: a current mirror costs this comparison and no
   GitHub call.
2. PULL, only when the mirror is behind. Concurrent queries of the same repository and
   mirror share one pull: the first starts it, the rest wait on it. After the pull the
   query compares once more; a mirror still behind answers as ``mirror_behind`` and never
   triggers a second pull for the same query.
3. ANSWER from the mirror.

The handler builds none of its dependencies: the clone, the watcher state, the bus state
and the puller are given to it (protocols in ``protocols/``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    MIRROR_FOR_WANT,
    EnumRepoStateMirror,
    EnumRepoStateSource,
    EnumRepoStateWant,
    ModelMirrorPull,
    ModelRepoStateAnswer,
    ModelRepoStateQuery,
    is_full_sha,
)
from omnimarket.nodes.node_repo_state_query_effect.protocols.protocol_repo_state import (
    ProtocolBusRepoState,
    ProtocolMirrorPuller,
    ProtocolPrWatcherMirror,
    ProtocolRepoCloneMirror,
)

logger = logging.getLogger(__name__)


@dataclass
class _Comparison:
    behind: bool
    comparisons: int
    target_sha: str = ""
    branch: str | None = None
    mirror_sha: str | None = None
    bus_sha: str | None = None
    bus_observed_at: datetime | None = None
    error: str | None = None


@dataclass
class _Flight:
    task: asyncio.Task[ModelMirrorPull]
    waiters: int = 1


class HandlerRepoStateQuery:
    """Compare, pull only when behind (one pull per repository at a time), answer."""

    def __init__(
        self,
        *,
        host_name: str,
        clone: ProtocolRepoCloneMirror,
        watcher: ProtocolPrWatcherMirror,
        bus_state: ProtocolBusRepoState,
        puller: ProtocolMirrorPuller,
    ) -> None:
        self.host_name = host_name
        self._clone = clone
        self._watcher = watcher
        self._bus = bus_state
        self._puller = puller
        self._inflight: dict[tuple[str, EnumRepoStateMirror], _Flight] = {}
        self.pulls = 0
        self.answered = 0

    async def handle(self, request: ModelRepoStateQuery) -> ModelRepoStateAnswer:
        if request.host != self.host_name:
            return self._fail(
                request, 0, f"addressed to {request.host}, not {self.host_name}"
            )
        mirror = MIRROR_FOR_WANT[request.want]
        first = await asyncio.to_thread(self._compare, request)
        if first.error:
            return self._fail(request, first.comparisons, first.error)
        comparisons, pull, final = first.comparisons, None, first
        source = EnumRepoStateSource.MIRROR
        if first.behind:
            pull = await self._pull_once(request.repo, mirror, first.target_sha)
            final = await asyncio.to_thread(self._compare, request)
            comparisons += final.comparisons
            source = (
                EnumRepoStateSource.MIRROR_BEHIND
                if final.behind or final.error
                else EnumRepoStateSource.MIRROR_AFTER_PULL
            )
        answer = await asyncio.to_thread(
            self._answer, request, final, source, comparisons, pull
        )
        self.answered += 1
        logger.info(
            "repo-state %s %s %s source=%s current=%s pulled=%s comparisons=%d ok=%s",
            self.host_name,
            request.repo,
            request.want.value,
            answer.source.value,
            answer.current,
            answer.pulled,
            answer.comparisons,
            answer.ok,
        )
        return answer

    # -- step 1 -----------------------------------------------------------------------------

    def _compare(self, request: ModelRepoStateQuery) -> _Comparison:
        if MIRROR_FOR_WANT[request.want] is EnumRepoStateMirror.WATCHER:
            return self._compare_watcher(request)
        repo = request.repo
        if request.want is EnumRepoStateWant.REFS:
            heads = self._bus.heads(repo)
            lagging = [
                h for h in heads if not self._clone.contains(repo, h.branch, h.sha)
            ]
            newest = max(lagging, key=lambda h: h.observed_at) if lagging else None
            return _Comparison(
                behind=bool(lagging),
                comparisons=max(1, len(heads)),
                target_sha=newest.sha if newest else "",
                bus_sha=newest.sha if newest else None,
                bus_observed_at=newest.observed_at if newest else None,
            )
        if request.want is EnumRepoStateWant.FILES and is_full_sha(request.revision):
            sha = request.revision or ""
            present = self._clone.resolve(repo, sha) is not None
            return _Comparison(
                behind=not present,
                comparisons=1,
                target_sha=sha,
                mirror_sha=sha if present else None,
            )
        branch = (
            request.revision
            if request.want is EnumRepoStateWant.FILES and request.revision
            else request.branch
        ) or self._clone.default_branch(repo)
        if branch is None:
            return _Comparison(
                behind=False,
                comparisons=1,
                error=f"no canonical clone of {repo} on {self.host_name}",
            )
        mirror_sha = self._clone.resolve(repo, branch)
        head = self._bus.head(repo, branch)
        if head is None:
            return _Comparison(
                behind=False, comparisons=1, branch=branch, mirror_sha=mirror_sha
            )
        behind = head.sha != mirror_sha and not self._clone.contains(
            repo, branch, head.sha
        )
        return _Comparison(
            behind=behind,
            comparisons=1,
            target_sha=head.sha,
            branch=branch,
            mirror_sha=mirror_sha,
            bus_sha=head.sha,
            bus_observed_at=head.observed_at,
        )

    def _compare_watcher(self, request: ModelRepoStateQuery) -> _Comparison:
        event = self._bus.pr_event(request.repo, request.pr_number or 0)
        read_at = self._watcher.read_at()
        if event is None:
            return _Comparison(behind=False, comparisons=1)
        behind = read_at is None or read_at < event.observed_at
        return _Comparison(
            behind=behind,
            comparisons=1,
            target_sha=event.sha,
            bus_sha=event.sha,
            bus_observed_at=event.observed_at,
        )

    # -- step 2 -----------------------------------------------------------------------------

    async def _pull_once(
        self, repo: str, mirror: EnumRepoStateMirror, target_sha: str
    ) -> ModelMirrorPull:
        key = (repo, mirror)
        flight = self._inflight.get(key)
        if flight is None:
            self.pulls += 1
            task = asyncio.ensure_future(
                asyncio.to_thread(self._pull, repo, mirror, target_sha)
            )
            flight = _Flight(task=task)
            self._inflight[key] = flight
            task.add_done_callback(lambda _done: self._inflight.pop(key, None))
        else:
            flight.waiters += 1
        result = await asyncio.shield(flight.task)
        return result.model_copy(update={"waiters": flight.waiters})

    def _pull(
        self, repo: str, mirror: EnumRepoStateMirror, target_sha: str
    ) -> ModelMirrorPull:
        started = time.monotonic()
        try:
            return self._puller.pull(repo, mirror, target_sha)
        except (
            Exception
        ) as exc:  # the puller is a dependency; its crash is this pull's failure
            return ModelMirrorPull(
                repo=repo,
                mirror=mirror,
                status="failed",
                ok=False,
                target_sha=target_sha,
                duration_ms=int((time.monotonic() - started) * 1000),
                message=f"{type(exc).__name__}: {exc}"[-2000:],
            )

    # -- step 3 -----------------------------------------------------------------------------

    def _answer(
        self,
        request: ModelRepoStateQuery,
        cmp: _Comparison,
        source: EnumRepoStateSource,
        comparisons: int,
        pull: ModelMirrorPull | None,
    ) -> ModelRepoStateAnswer:
        base: dict[str, Any] = {
            "correlation_id": request.correlation_id,
            "host": self.host_name,
            "repo": request.repo,
            "want": request.want,
            "source": source,
            "current": not cmp.behind and cmp.error is None,
            "comparisons": comparisons,
            "pulled": pull is not None,
            "pull": pull,
            "bus_sha": cmp.bus_sha,
            "bus_observed_at": cmp.bus_observed_at,
            "mirror_read_at": datetime.now(UTC),
        }
        repo, want = request.repo, request.want
        if want is EnumRepoStateWant.REFS:
            refs = self._clone.refs(repo)
            if refs is None:
                return self._fail(
                    request, comparisons, f"no canonical clone of {repo}", pull
                )
            return ModelRepoStateAnswer(ok=True, refs=refs, **base)
        if want in (EnumRepoStateWant.BRANCH_HEAD, EnumRepoStateWant.FILES):
            revision = request.revision if is_full_sha(request.revision) else cmp.branch
            sha = self._clone.resolve(repo, revision) if revision else None
            if sha is None:
                return self._fail(
                    request,
                    comparisons,
                    f"{revision} is not in the mirror of {repo}",
                    pull,
                )
            if want is EnumRepoStateWant.BRANCH_HEAD:
                return ModelRepoStateAnswer(
                    ok=True, mirror_sha=sha, revision_sha=sha, **base
                )
            files = self._clone.read_files(repo, sha, request.paths)
            return ModelRepoStateAnswer(
                ok=True, mirror_sha=sha, revision_sha=sha, files=files, **base
            )
        record = self._watcher.record(repo, request.pr_number or 0)
        base["mirror_read_at"] = self._watcher.read_at()
        if record is None:
            return self._fail(
                request,
                comparisons,
                f"the PR watcher holds no {repo}#{request.pr_number}",
                pull,
            )
        if want is EnumRepoStateWant.PULL_REQUEST:
            return ModelRepoStateAnswer(ok=True, record=record, **base)
        ci = record.get("ci")
        if not isinstance(ci, dict):
            return self._fail(
                request, comparisons, "the watcher record holds no ci", pull
            )
        if want is EnumRepoStateWant.CHECKS:
            return ModelRepoStateAnswer(ok=True, record=ci, **base)
        job = _find_job(ci, request.job_id or "")
        if job is None:
            return self._fail(
                request, comparisons, f"no job {request.job_id} in the ci record", pull
            )
        return ModelRepoStateAnswer(ok=True, record=job, **base)

    def _fail(
        self,
        request: ModelRepoStateQuery,
        comparisons: int,
        error: str,
        pull: ModelMirrorPull | None = None,
    ) -> ModelRepoStateAnswer:
        logger.info("repo-state %s %s refused: %s", self.host_name, request.repo, error)
        return ModelRepoStateAnswer(
            correlation_id=request.correlation_id,
            host=self.host_name,
            repo=request.repo,
            want=request.want,
            ok=False,
            source=EnumRepoStateSource.UNAVAILABLE,
            current=False,
            comparisons=comparisons,
            pulled=pull is not None,
            pull=pull,
            error=error[-2000:],
        )


def _find_job(ci: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    """The job record whose id is ``job_id``, anywhere in the ci record's lists."""
    stack: list[Any] = [ci]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(str(item.get(k)) == job_id for k in ("job_id", "id", "databaseId")):
                return item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


__all__ = ["HandlerRepoStateQuery"]
