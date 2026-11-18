# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HELD, CLEAR or UNDECIDED for one pull request from the bus-fed ledger holds answer.

Cutover plan step L6 (OMN-19609): ``pause_check`` read the file with awk. The question it asks of
the holds, "does a hold in force apply to this pull request?", is answered here from the lines of
``node_work_ledger_reader_effect`` (the query node's ``holds`` verb over the projection). A hold
present only in the projection answers HELD. CLEAR needs a read that answered: the node fresh, or,
while the overlay allows it (until plan step L15), the file's holds lines the caller carried,
stamped ``source=file-fallback``. A failed read, a stale answer with no hold, a retired fallback
and a hold line this handler cannot read are UNDECIDED, never CLEAR.

Every verdict emits one event: ``answered`` (golden) for HELD and CLEAR, ``refused`` (error) for
UNDECIDED. Named break: ``_applies`` always false reads the projection-only hold as CLEAR.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.hold_verdict import (
    EnumHoldSource,
    EnumHoldVerdict,
    ModelHoldVerdict,
    ModelHoldVerdictEvent,
    ModelHoldVerdictOverlay,
    ModelHoldVerdictRequest,
)
from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    EnumReaderStatus,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.protocols import (
    ProtocolHoldVerdictEventSink,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

# A hold line of the query node (``db:<row id>``) or of the file (``L<n>``): the head is the same.
_HOLD_HEAD = re.compile(
    r"^HOLD (?P<id>\S+) by=(?P<by>\S+) scope=<(?P<scope>[^>]*)> until=(?P<until>\S+)"
)
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class _Hold:
    """One parsed hold: its id, its scope cells and its end."""

    __slots__ = ("hold_id", "scope", "until")

    def __init__(
        self, hold_id: str, scope: dict[str, tuple[str, ...]], until: str
    ) -> None:
        self.hold_id = hold_id
        self.scope = scope
        self.until = until


def _scope_cells(text: str) -> dict[str, tuple[str, ...]]:
    cells: dict[str, tuple[str, ...]] = {}
    for part in text.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            cells[key.strip()] = tuple(
                t for t in re.split(r"[ ,]+", value.strip()) if t
            )
    return cells


def _short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def _until_passed(until: str, now: datetime) -> bool:
    if not _STAMP.match(until):
        return False
    stamp = until if until.count(":") == 2 else until[:-1] + ":00Z"
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) < now


def _applies(hold: _Hold, request: ModelHoldVerdictRequest, now: datetime) -> bool:
    """Does this hold in force apply to the request's pull request or lane?"""
    if (
        "surface" in hold.scope
    ):  # a lease on a proof surface holds the surface, never a PR
        return False
    if _until_passed(hold.until, now):
        return False
    repo, _, number = request.pr.partition("#")
    if "pr" in hold.scope:  # the pr cell alone decides
        repos = {_short(r) for r in hold.scope.get("repo", ())}
        for token in hold.scope["pr"]:
            if "#" in token:
                token_repo, _, token_number = token.rpartition("#")
                if _short(token_repo) == repo and token_number == number:
                    return True
            elif token.lstrip("#") == number and repo in repos:
                return True
        return False
    if "repo" in hold.scope:
        return repo in {_short(r) for r in hold.scope["repo"]}
    addressed = hold.scope.get("to", ())
    return "all" in addressed or (
        request.lane is not None and request.lane in addressed
    )


class HandlerHoldVerdict:
    """``handle(ModelHoldVerdictRequest) -> ModelHoldVerdict``."""

    def __init__(
        self,
        overlay: ModelHoldVerdictOverlay,
        events: ProtocolHoldVerdictEventSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        topics: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._events = events
        self._clock = clock
        self._topics = dict(load_topics() if topics is None else topics)

    def handle(self, request: ModelHoldVerdictRequest) -> ModelHoldVerdict:
        answer = request.node_answer
        if answer.status in (EnumReaderStatus.OK, EnumReaderStatus.STALE):
            lines, source = answer.lines, EnumHoldSource.NODE
        elif not self._overlay.file_fallback_allowed:
            return self._finish(
                request,
                EnumHoldVerdict.UNDECIDED,
                EnumHoldSource.NONE,
                "file-fallback-retired",
            )
        elif request.file_fallback_lines is None:
            return self._finish(
                request,
                EnumHoldVerdict.UNDECIDED,
                EnumHoldSource.NONE,
                "node-unreadable-and-no-file-lines",
            )
        else:
            lines, source = request.file_fallback_lines, EnumHoldSource.FILE_FALLBACK
        holds: list[_Hold] = []
        for line in lines:
            if (
                not line.startswith("HOLD ")
                or line.startswith("HOLD none ")
                or line.startswith("HOLD stale=")
            ):
                continue
            head = _HOLD_HEAD.match(line)
            if head is None:
                return self._finish(
                    request, EnumHoldVerdict.UNDECIDED, source, "unparseable-hold-line"
                )
            holds.append(_Hold(head["id"], _scope_cells(head["scope"]), head["until"]))
        now = self._clock().astimezone(UTC)
        applying = tuple(h.hold_id for h in holds if _applies(h, request, now))
        if applying:
            return self._finish(
                request, EnumHoldVerdict.HELD, source, "hold-in-force", applying
            )
        if answer.status is EnumReaderStatus.STALE:
            return self._finish(
                request,
                EnumHoldVerdict.UNDECIDED,
                source,
                "watermark-stale-and-no-hold",
            )
        reason = (
            "node-answered-no-hold"
            if source is EnumHoldSource.NODE
            else "file-fallback-answered-no-hold"
        )
        return self._finish(request, EnumHoldVerdict.CLEAR, source, reason)

    def _finish(
        self,
        request: ModelHoldVerdictRequest,
        verdict: EnumHoldVerdict,
        source: EnumHoldSource,
        reason: str,
        hold_ids: tuple[str, ...] = (),
    ) -> ModelHoldVerdict:
        golden = verdict is not EnumHoldVerdict.UNDECIDED
        event = ModelHoldVerdictEvent(
            kind=EnumReaderEventKind.ANSWERED
            if golden
            else EnumReaderEventKind.REFUSED,
            topic=self._topics[
                "hold_verdict_answered" if golden else "hold_verdict_refused"
            ],
            pr=request.pr,
            verdict=verdict,
            source=source,
            reason=reason,
            at=self._clock().astimezone(UTC),
            hold_ids=hold_ids,
        )
        self._events.emit(event)
        return ModelHoldVerdict(
            pr=request.pr,
            verdict=verdict,
            source=source,
            reason=reason,
            hold_ids=hold_ids,
            event=event,
        )


__all__ = ["HandlerHoldVerdict"]
