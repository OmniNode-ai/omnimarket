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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.landing_decision import (
    SHA_PATTERN,
    EnumLandingCi,
    EnumLandingMergeState,
    EnumLandingPrState,
    EnumLandingSuspension,
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_line import (
    ModelLandingLabLineFact,
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


_PASSED = frozenset({"success", "skipped", "neutral"})


def _passed_checks(ci: Mapping[str, object]) -> tuple[str, ...]:
    """Names of the head's check-runs whose newest copy passed, from the watcher's run rows."""
    runs = ci.get("runs")
    names = {
        str(row[0])
        for row in (runs if isinstance(runs, list) else [])
        if isinstance(row, list)
        and len(row) > 2
        and row[1] == "completed"
        and row[2] in _PASSED
    }
    return tuple(sorted(names))


_LAB_LINE = re.compile(r"^\s*Lab:(?P<rest>.*)$", re.MULTILINE)
_LAB_HEAD = re.compile(r"\bhead=(?P<sha>[0-9a-f]{40})\b")


def _newest_lab_head(texts: Sequence[str]) -> tuple[bool, str | None]:
    """(a Lab line exists, the head the newest one names) over texts ordered oldest first."""
    found, named = False, None
    for text in texts:
        for line in _LAB_LINE.finditer(text):
            found = True
            m = _LAB_HEAD.search(line.group("rest"))
            named = m.group("sha") if m else None
    return found, named


def _lab_line_fact(
    body: object, comments: Sequence[str] | None, head: str
) -> ModelLandingLabLineFact | None:
    """The newest Lab line as a fact (OMN-26736).

    The watcher state holds the PR body but no comments. With ``comments`` unread (None) the fact is
    given only when the body's Lab line names the head; any other body reading stays unread, because
    a newer comment may name the head. With ``comments`` read, the newest Lab line over body then
    comments is the fact, and no Lab line at all is a fact naming no head.
    """
    texts = [body] if isinstance(body, str) else []
    if comments is None:
        _, named = _newest_lab_head(texts)
        return None if named != head else ModelLandingLabLineFact(named_head=named)
    _, named = _newest_lab_head([*texts, *comments])
    return ModelLandingLabLineFact(named_head=named)


def _pr_facts(
    entry: Mapping[str, object],
    overlay: ModelWatcherFactsOverlay,
    required: Mapping[str, Sequence[str]] | None = None,
    lab_comments: Mapping[str, Sequence[str]] | None = None,
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
    contexts = None if required is None else required.get(f"{repo}:{facts.get('base')}")
    pr_key = f"{overlay.owner}/{repo}#{facts.get('number')}"
    return ModelLandingPrFacts(
        pr=pr_key,
        lab_line=_lab_line_fact(
            facts.get("body"),
            None if lab_comments is None else lab_comments.get(pr_key),
            head,
        ),
        head_sha=head,
        state=EnumLandingPrState.OPEN,
        ci=verdict,
        red_class=overlay.unclassified_red_class if red else None,
        red_checks=tuple(str(c) for c in ci.get("red") or ()) if red else (),
        cancelled_checks=tuple(str(c) for c in ci.get("cancelled") or ()),
        required_contexts=None if contexts is None else tuple(contexts),
        passed_checks=_passed_checks(ci),
        merge_state=merge_state,
        suspensions=(EnumLandingSuspension.DRAFT,) if facts.get("draft") else (),
        merge_queue=bool(entry.get("queued")),
        created_at=_stamp(facts.get("created_at")),
        pr_node_id=str(node_id) if node_id else None,
        failed_run_ids=tuple(runs),
    )


def map_watcher_state(
    state: object,
    overlay: ModelWatcherFactsOverlay,
    required: Mapping[str, Sequence[str]] | None = None,
    lab_comments: Mapping[str, Sequence[str]] | None = None,
) -> ModelLandingFacts:
    """The watcher state's open pull requests of the overlay's repositories, as one tick of landing facts.

    The tick is the watcher's last_tick in epoch seconds, so the same state gives the same tick.
    ``required`` maps ``repo:base`` to the contexts that base branch requires, as the snapshot
    reader read them from branch protection and rulesets; a base it does not name is unread.
    ``lab_comments`` maps a PR (``owner/repo#n``) to its comment bodies, oldest first, for the
    newest Lab line; a PR it does not name has its comments unread.
    """
    at, table = _table(state)
    prs = [
        pr
        for entry in table.values()
        if isinstance(entry, dict)
        and (pr := _pr_facts(entry, overlay, required, lab_comments)) is not None
    ]
    return ModelLandingFacts(
        tick=int(at.timestamp()),
        observed_at=at,
        prs=tuple(sorted(prs, key=lambda p: p.pr)),
    )


_OVERLAY_PATH = (
    Path(__file__).resolve().parents[1] / "watcher_facts.overlay.example.yaml"
)


def default_overlay() -> ModelWatcherFactsOverlay:
    """The node's shipped example overlay: the in-memory default when none is injected."""
    return ModelWatcherFactsOverlay.model_validate(
        yaml.safe_load(_OVERLAY_PATH.read_text(encoding="utf-8"))
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
        required_contexts: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._required = required_contexts
        self._overlay = default_overlay() if overlay is None else overlay
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
            facts = map_watcher_state(state, self._overlay, self._required)
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


__all__: list[str] = [
    "HandlerPrLandingWatcherFacts",
    "default_overlay",
    "map_watcher_state",
]
