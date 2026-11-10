# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing watcher facts node's handler (OMN-23507).

The old landing controller gives a repository ruled out of its scope no facts, so the composed
landing workflow's snapshot step would read no pull request of a repository switched to it. This
handler reads the PR watcher's state (a projection of GitHub; no GitHub read here), keeps the open
pull requests of the repositories the overlay names, and writes them as one tick of landing facts,
the shape the snapshot step's facts source reads. It only reads and records: it never writes to
GitHub and never acts on a pull request.

A state that cannot be read, holds no pr table or an unreadable stamp, or is older than the
overlay's bound ends in the typed failed event, and nothing is written.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from omnimarket.models.landing_decision import (
    SHA_PATTERN,
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingCi,
    EnumLandingMergeState,
    EnumLandingPrState,
    EnumLandingSuspension,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_errors import (
    WatcherFactsSourceError,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_ports import (
    InMemoryLandingFactsSink,
    InMemoryWatcherFactsPublisher,
    InMemoryWatcherStateSource,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts import (
    EnumWatcherFactsFailureReason,
    EnumWatcherFactsStatus,
    ModelWatcherFactsFailed,
    ModelWatcherFactsRequest,
    ModelWatcherFactsResult,
    ModelWatcherFactsWritten,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts_overlay import (
    EnumWatcherFactsBackend,
    ModelWatcherFactsOverlay,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.protocols.protocol_watcher_facts import (
    ProtocolLandingFactsSink,
    ProtocolWatcherFactsPublisher,
    ProtocolWatcherStateSource,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_SHA = re.compile(SHA_PATTERN)
_CI = {"GREEN": EnumLandingCi.GREEN, "RED": EnumLandingCi.RED}
_MERGE_STATE = {
    "CLEAN": EnumLandingMergeState.CLEAN,
    "UNSTABLE": EnumLandingMergeState.CLEAN,
    "HAS_HOOKS": EnumLandingMergeState.CLEAN,
    "BEHIND": EnumLandingMergeState.BEHIND,
    "DIRTY": EnumLandingMergeState.CONFLICTING,
    "BLOCKED": EnumLandingMergeState.BLOCKED,
}


def _stamp(value: object) -> datetime:
    try:
        return datetime.strptime(str(value), _STAMP).replace(tzinfo=UTC)
    except ValueError:
        raise WatcherFactsSourceError(f"stamp {value!r} unreadable") from None


def _table(state: object) -> tuple[datetime, Mapping[str, object]]:
    if not isinstance(state, dict) or not isinstance(state.get("prs"), dict):
        raise WatcherFactsSourceError("the watcher state holds no pr table")
    return _stamp(state.get("last_tick")), state["prs"]


def _pr_facts(
    entry: Mapping[str, object], overlay: ModelWatcherFactsOverlay
) -> ModelLandingPrFacts | None:
    facts = entry.get("facts")
    if not isinstance(facts, dict):
        return None
    repo, head = facts.get("repo"), facts.get("head_sha")
    if repo not in overlay.repositories or facts.get("state") != "OPEN":
        return None
    if not isinstance(head, str) or not _SHA.match(head):
        return None
    ci_raw = entry.get("ci")
    ci = ci_raw if isinstance(ci_raw, dict) and ci_raw.get("sha") == head else {}
    verdict = _CI.get(str(ci.get("verdict")), EnumLandingCi.PENDING)
    runs: list[int] = []
    for row in ci.get("red_facts") or []:
        if isinstance(row, list) and len(row) > 2 and str(row[2]).isdigit():
            run = int(row[2])
            if run not in runs:
                runs.append(run)
    review = entry.get("review")
    merge_raw = review.get("merge_state") if isinstance(review, dict) else None
    merge_state = _MERGE_STATE.get(str(merge_raw), EnumLandingMergeState.UNKNOWN)
    red = verdict is EnumLandingCi.RED
    node_id = facts.get("node_id")
    return ModelLandingPrFacts(
        pr=f"{overlay.owner}/{repo}#{facts.get('number')}",
        head_sha=head,
        state=EnumLandingPrState.OPEN,
        ci=verdict,
        red_class=overlay.unclassified_red_class if red else None,
        red_checks=tuple(str(c) for c in ci.get("red") or ()) if red else (),
        cancelled_checks=tuple(str(c) for c in ci.get("cancelled") or ()),
        merge_state=merge_state,
        suspensions=(EnumLandingSuspension.DRAFT,) if facts.get("draft") else (),
        merge_queue=bool(entry.get("queued")),
        created_at=_stamp(facts.get("created_at")),
        pr_node_id=str(node_id) if node_id else None,
        failed_run_ids=tuple(runs),
    )


def map_watcher_state(
    state: object, overlay: ModelWatcherFactsOverlay
) -> ModelLandingFacts:
    """The watcher state's open pull requests of the overlay's repositories, as one tick of landing facts.

    The tick is the watcher's last_tick in epoch seconds, so the same state gives the same tick.
    """
    at, table = _table(state)
    prs = [
        pr
        for entry in table.values()
        if isinstance(entry, dict) and (pr := _pr_facts(entry, overlay)) is not None
    ]
    return ModelLandingFacts(
        tick=int(at.timestamp()),
        observed_at=at,
        prs=tuple(sorted(prs, key=lambda p: p.pr)),
    )


def _inmemory_overlay() -> ModelWatcherFactsOverlay:
    return ModelWatcherFactsOverlay(
        backend=EnumWatcherFactsBackend.INMEMORY,
        repositories=("omnimemory",),
        owner="OmniNode-ai",
        unclassified_red_class="product",
        written_topic="onex.evt.omnimarket.pr-landing-watcher-facts-written.v1",
        failed_topic="onex.evt.omnimarket.pr-landing-watcher-facts-failed.v1",
        floor_piece=9,
        watcher_max_age_seconds=900,
    )


class HandlerPrLandingWatcherFacts:
    """One pass: read the watcher's state, write the overlay's repositories' landing facts."""

    def __init__(
        self,
        overlay: ModelWatcherFactsOverlay | None = None,
        state_source: ProtocolWatcherStateSource | None = None,
        sink: ProtocolLandingFactsSink | None = None,
        publisher: ProtocolWatcherFactsPublisher | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._overlay = overlay or _inmemory_overlay()
        self._source = state_source or InMemoryWatcherStateSource({})
        self._sink = sink or InMemoryLandingFactsSink()
        self._publisher = publisher or InMemoryWatcherFactsPublisher()
        self._now = now

    def _fail(
        self,
        request: ModelWatcherFactsRequest,
        reason: EnumWatcherFactsFailureReason,
        detail: str,
    ) -> ModelWatcherFactsResult:
        self._publisher.publish(
            self._overlay.failed_topic,
            ModelWatcherFactsFailed(
                correlation_id=request.correlation_id, reason=reason, detail=detail
            ),
        )
        return ModelWatcherFactsResult(
            status=EnumWatcherFactsStatus.FAILED,
            correlation_id=request.correlation_id,
            failure_reason=reason,
            failure_detail=detail,
        )

    def handle(self, request: ModelWatcherFactsRequest) -> ModelWatcherFactsResult:
        try:
            state = self._source.read_state()
            at, _ = _table(state)
        except WatcherFactsSourceError as exc:
            return self._fail(
                request, EnumWatcherFactsFailureReason.STATE_UNREADABLE, str(exc)
            )
        age = (self._now() - at).total_seconds()
        if age > self._overlay.watcher_max_age_seconds:
            return self._fail(
                request,
                EnumWatcherFactsFailureReason.STATE_STALE,
                f"the watcher state is {int(age)}s old, past its bound of "
                f"{self._overlay.watcher_max_age_seconds}s",
            )
        try:
            facts = map_watcher_state(state, self._overlay)
            where = self._sink.write(facts)
        except (OSError, WatcherFactsSourceError) as exc:
            return self._fail(
                request,
                EnumWatcherFactsFailureReason.FACTS_UNWRITABLE,
                f"{type(exc).__name__}: {exc}",
            )
        written = ModelWatcherFactsWritten(
            correlation_id=request.correlation_id,
            tick=facts.tick,
            prs=len(facts.prs),
            repositories=self._overlay.repositories,
            facts_path=where,
        )
        self._publisher.publish(self._overlay.written_topic, written)
        return ModelWatcherFactsResult(
            status=EnumWatcherFactsStatus.WRITTEN,
            correlation_id=request.correlation_id,
            written=written,
            facts=facts,
        )


__all__: list[str] = ["HandlerPrLandingWatcherFacts", "map_watcher_state"]
