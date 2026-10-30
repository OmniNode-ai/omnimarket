# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fix, one pull request: refuse a new pull request an open one or a live CLAIM already covers (OMN-17427).

The fold reads nothing. The caller supplies the new pull request (its branch, title, body and what its
diff changes), the open pull requests of the same repository with what each changes (the PR watcher's
records plus a ``git diff -U0`` against the merge base) and the rolling ledger text. It answers ``adopt``
when any of these holds, naming the pull request to take over:

- ``branch``: a pull request is already open from this branch;
- ``lines``: an open pull request against the same base changes the same existing lines of a file, or adds
  the same new file (three omnibase_core pull requests renamed one baseline line on 2026-10-30);
- ``files_and_title``: it changes the same file and its title shares three distinctive words with the new
  one (two pull requests in a second repository added one dispatch on 2026-10-30);
- ``cause_cited``: its title (a fix title) or a ``Fix-Check:`` / ``Fix-Cause:`` body line cites a check or
  cause the new pull request is for, taken from the lane's own CLAIM ``cause=`` cell, the caller's ``cause``
  or the new body's markers;
- ``claim``: another lane's live CLAIM carries the same ``cause=`` cell.

Two pull requests on the same file that change different lines, with unrelated titles, are not duplicates:
they pass, and the overlap is reported in ``notes``. A pull request whose diff was not read passes on its
diff and is listed in ``unread``; the other evidence still applies to it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import Final

from omnimarket.handlers.handler_ledger_claims import claim_lane, live_claims
from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    ModelDuplicateEvidence,
    ModelFileTouch,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateGuardResult,
    ModelPrTouch,
)

#: A CLAIM's lane is live this long after its last ledger row (the same bar every cause reader uses).
CLAIM_LIVE_HOURS: Final[float] = 0.75
#: Distinctive title words two pull requests on one file must share to count as one change.
SHARED_WORDS_NEEDED: Final[int] = 3
#: Files every kind of change touches; sharing one says nothing about a shared fix.
_GENERIC_FILES: Final[tuple[str, ...]] = (
    "*.lock",
    "package-lock.json",
    "CHANGELOG*",
    "contracts/OMN-*.yaml",
    "*ROLLING_WORK_LEDGER*",
)
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    [
        "fix",
        "fixes",
        "follow",
        "follows",
        "check",
        "checks",
        "test",
        "tests",
        "with",
        "from",
        "that",
        "this",
        "name",
        "names",
        "file",
        "files",
        "line",
        "lines",
        "when",
        "what",
        "into",
        "node",
        "nodes",
        "lane",
        "lanes",
        "now",
        "hold",
        "holds",
        "add",
        "adds",
        "added",
        "make",
        "makes",
        "remove",
        "removes",
        "move",
        "moves",
        "rename",
        "renames",
        "runs",
        "uses",
        "read",
        "reads",
        "gets",
        "keeps",
        "again",
        "also",
        "only",
        "more",
        "than",
        "then",
        "them",
        "they",
        "have",
        "been",
        "which",
        "after",
        "before",
        "wrongly",
    ]
)
_FIXISH = re.compile(r"^(?:(?:hot)?fix|repair|correct|refresh)\b|^OMN-\d+\s*:", re.I)
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")
_TICKET = re.compile(r"\s*\(\s*OMN-\d+\s*\)|\bOMN-\d+\b", re.I)
_SPELLING = re.compile(r"[\s_-]+")
_MARKER_CHECK = re.compile(r"^Fix-Check:[ \t]*(.+)$", re.M | re.I)
_MARKER_CAUSE = re.compile(r"^Fix-Cause:[ \t]*(.+)$", re.M | re.I)
_PR_REF = re.compile(r"([A-Za-z0-9_.-]+)#([1-9][0-9]*)")


def canonical(name: str) -> str:
    """A check name in the one spelling it is compared in: case folded, runs of spaces, underscores and hyphens one space."""
    return _SPELLING.sub(" ", _TICKET.sub("", str(name)).casefold()).strip()


def _short(repo: str) -> str:
    return repo.strip().split("/")[-1].casefold()


def is_generic(path: str) -> bool:
    return any(
        fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern)
        for pattern in _GENERIC_FILES
    )


def distinctive_words(title: str) -> frozenset[str]:
    return frozenset(
        w
        for w in _WORD.findall(_TICKET.sub("", title).casefold())
        if w not in _STOP_WORDS
    )


def cells(row: str) -> dict[str, str]:
    """``key=value`` cells of one ledger row, split on the pipe. A later cell of the same key does not replace the first."""
    out: dict[str, str] = {}
    for cell in row.split("\n", 1)[0].split("|"):
        key, sep, value = cell.strip().partition("=")
        if sep and key and " " not in key:
            out.setdefault(key, value.strip())
    return out


def cause_of(row: str) -> tuple[str, str] | None:
    """The (repo, check) of a row's ``cause=<repo>:<check>`` cell, or None."""
    cause = cells(row).get("cause")
    if not cause:
        return None
    repo, sep, check = cause.partition(":")
    return (_short(repo), canonical(check)) if sep and canonical(check) else None


def _parse_cause_argument(cause: str, repo: str) -> tuple[str, str] | None:
    head, sep, tail = cause.partition(":")
    pair = (_short(head), canonical(tail)) if sep else (repo, canonical(cause))
    return pair if pair[1] else None


def _pr_ref(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def overlap(new: ModelPrTouch, other: ModelPrTouch) -> tuple[list[str], list[str]]:
    """(same-line hits, shared non-generic files) between two diffs, each sorted."""
    theirs: dict[str, ModelFileTouch] = {f.path: f for f in other.files}
    lines: list[str] = []
    shared: list[str] = []
    for mine in new.files:
        match = theirs.get(mine.path)
        if match is None or is_generic(mine.path):
            continue
        shared.append(mine.path)
        if mine.added and match.added:
            lines.append(f"{mine.path} (both add it)")
            continue
        for a in mine.spans:
            hit = next((b for b in match.spans if a[0] <= b[1] and b[0] <= a[1]), None)
            if hit is not None:
                lines.append(f"{mine.path}:{max(a[0], hit[0])}")
                break
    return sorted(lines), sorted(shared)


def _wanted_causes(
    request: ModelPrDuplicateGuardRequest, repo: str, own_rows: list[str]
) -> set[tuple[str, str]]:
    wanted: set[tuple[str, str]] = set()
    for row in own_rows:
        pair = cause_of(row)
        if pair is not None and pair[0] == repo:
            wanted.add(pair)
    if request.cause:
        pair = _parse_cause_argument(request.cause, repo)
        if pair is not None:
            wanted.add(pair)
    for text in _MARKER_CHECK.findall(request.body):
        if canonical(text):
            wanted.add((repo, canonical(text)))
    for text in _MARKER_CAUSE.findall(request.body):
        pair = _parse_cause_argument(text.strip(), repo)
        if pair is not None:
            wanted.add(pair)
    return wanted


def _cites(title: str, check: str) -> bool:
    return (
        bool(_FIXISH.match(title.strip()))
        and re.search(rf"(?<![a-z0-9]){re.escape(check)}(?![a-z0-9])", canonical(title))
        is not None
    )


def _markers(pr: ModelOpenPr, repo: str) -> set[tuple[str, str]]:
    found = {
        (repo, canonical(t)) for t in _MARKER_CHECK.findall(pr.body) if canonical(t)
    }
    for text in _MARKER_CAUSE.findall(pr.body):
        pair = _parse_cause_argument(text.strip(), repo)
        if pair is not None:
            found.add(pair)
    return found


def _claim_prs(row: str, repo: str, open_numbers: set[int]) -> list[int]:
    found: list[int] = []
    cell = cells(row)
    for key in ("pr", "fix"):
        for name, number in _PR_REF.findall(cell.get(key, "")):
            if _short(name) == repo and int(number) in open_numbers:
                found.append(int(number))
    return sorted(set(found))


class HandlerPrDuplicateGuard:
    """Fold one new pull request against the open ones and the live CLAIMs."""

    def handle(
        self, request: ModelPrDuplicateGuardRequest
    ) -> ModelPrDuplicateGuardResult:
        repo = _short(request.repo)
        evidence: list[ModelDuplicateEvidence] = []
        notes: list[str] = []
        unread: list[str] = []
        open_numbers = {pr.number for pr in request.open_prs}
        live = live_claims(request.ledger_text, _utc(request.now), CLAIM_LIVE_HOURS)
        own_rows = [
            row for row in live if request.lane and claim_lane(row) == request.lane
        ]
        wanted = _wanted_causes(request, repo, own_rows)
        new_words = distinctive_words(request.title)

        for pr in sorted(request.open_prs, key=lambda p: p.number):
            ref = _pr_ref(repo, pr.number)
            if pr.head_ref == request.head_ref:
                evidence.append(
                    ModelDuplicateEvidence(
                        kind=EnumDuplicateEvidence.BRANCH,
                        pr=ref,
                        detail=f"{ref} is already open from branch {request.head_ref}",
                    )
                )
                continue
            if request.base is not None and pr.base != request.base:
                continue
            if pr.head_ref == request.base:
                continue  # the new pull request is stacked on this one
            hit = False
            if request.touch is not None and pr.touch is not None:
                lines, shared = overlap(request.touch, pr.touch)
                words = sorted(new_words & distinctive_words(pr.title))
                if lines:
                    hit = True
                    evidence.append(
                        ModelDuplicateEvidence(
                            kind=EnumDuplicateEvidence.LINES,
                            pr=ref,
                            detail=f"{ref} changes the same lines: {', '.join(lines)}",
                        )
                    )
                elif shared and len(words) >= SHARED_WORDS_NEEDED:
                    hit = True
                    evidence.append(
                        ModelDuplicateEvidence(
                            kind=EnumDuplicateEvidence.FILES_AND_TITLE,
                            pr=ref,
                            detail=(
                                f"{ref} changes {', '.join(shared)} and its title shares "
                                f"{', '.join(words)}"
                            ),
                        )
                    )
                elif shared:
                    notes.append(
                        f"{ref} also changes {', '.join(shared)}, on other lines and under an unrelated title: not the same fix"
                    )
            elif pr.touch is None:
                unread.append(ref)
            if hit:
                continue
            cited = sorted({check for _, check in wanted if _cites(pr.title, check)})
            marked = sorted(check for _, check in wanted & _markers(pr, repo))
            if cited or marked:
                names = ", ".join(cited or marked)
                evidence.append(
                    ModelDuplicateEvidence(
                        kind=EnumDuplicateEvidence.CAUSE_CITED,
                        pr=ref,
                        detail=f"{ref} cites the same check or cause: {names}",
                    )
                )

        titled = _title_checks(request.title, live, repo)
        for row in live:
            lane = claim_lane(row)
            pair = cause_of(row)
            if lane is None or lane == request.lane or pair is None or pair[0] != repo:
                continue
            if (pair in wanted) or (pair[1] in titled):
                numbers = _claim_prs(row, repo, open_numbers)
                evidence.append(
                    ModelDuplicateEvidence(
                        kind=EnumDuplicateEvidence.CLAIM,
                        pr=_pr_ref(repo, numbers[0]) if numbers else None,
                        lane=lane,
                        detail=f"lane {lane} holds a live CLAIM with cause={repo}:{pair[1]}",
                    )
                )

        if not evidence:
            return ModelPrDuplicateGuardResult(
                state="clear",
                notes=tuple(notes),
                unread=tuple(unread),
                message=f"no open pull request or live CLAIM in {repo} covers this change",
            )
        ordered = sorted(
            evidence,
            key=lambda e: (
                e.pr is None,
                int(e.pr.split("#")[1]) if e.pr else 0,
                e.kind.value,
                e.lane or "",
            ),
        )
        adopt_pr = next((e.pr for e in ordered if e.pr), None)
        adopt_lane = next(
            (e.lane for e in ordered if e.lane and e.pr == adopt_pr), None
        ) or next((e.lane for e in ordered if e.lane), None)
        target = adopt_pr or (
            f"the CLAIM of lane {adopt_lane}" if adopt_lane else "the existing owner"
        )
        return ModelPrDuplicateGuardResult(
            state="adopt",
            adopt_pr=adopt_pr,
            adopt_lane=adopt_lane,
            evidence=tuple(ordered),
            notes=tuple(notes),
            unread=tuple(unread),
            message=(
                f"{target} already covers this change ({'; '.join(e.detail for e in ordered)}). "
                "Open no second pull request: comment on it, rebase it or push to its branch, and say so on your STATUS row "
                f"(adopted={adopt_pr or target})."
            ),
        )


def _title_checks(title: str, live: tuple[str, ...], repo: str) -> set[str]:
    """Checks of other lanes' live cause CLAIMs in this repo that a fix title cites."""
    found: set[str] = set()
    for row in live:
        pair = cause_of(row)
        if pair is not None and pair[0] == repo and _cites(title, pair[1]):
            found.add(pair[1])
    return found


def _utc(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
