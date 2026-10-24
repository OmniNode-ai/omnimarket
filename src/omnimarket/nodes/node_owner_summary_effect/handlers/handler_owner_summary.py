# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts, shape check and send watermark for the twice-daily owner summary.

Four commands, none of which reads GitHub:

* ``facts``     what the factory did in a window. Merges come from the PR watcher's local
                state, with the canonical clones' first-parent history as a cross-check and
                gap fill. Lanes come from the work ledger. A source that cannot be read is an
                ``errors`` entry, never a guess.
* ``check``     validates a draft against the facts: footer, length, no token-shaped string, no
                home path, and every ticket id and ``repo#number`` the draft cites must be in
                the facts. A cited id the facts do not hold is a fabricated one.
* ``mark-sent`` records the window end as sent, so the next window starts where this one ended.
* ``triage``    separates named omissions from actionable reviewer findings.

Authors are dropped from the facts on purpose: the summary refers to people by role, and a name
that is not in the facts cannot reach the draft.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from omnimarket.nodes.node_owner_summary_effect.models.model_clone_commit import (
    ModelCloneCommit,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_draft_coverage import (
    ModelDraftCoverage,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_closed import (
    ModelLaneClosed,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_started import (
    ModelLaneStarted,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_m4_row import ModelM4Row
from omnimarket.nodes.node_owner_summary_effect.models.model_merged_change import (
    ModelMergedChange,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_owner_summary_facts import (
    ModelOwnerSummaryFacts,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_owner_summary_request import (
    ModelOwnerSummaryRequest,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_owner_summary_result import (
    ModelOwnerSummaryResult,
)

DEFAULT_WINDOW_HOURS = 12
MAX_WINDOW_HOURS = 48
MAX_WORDS = 420
TEXT_LIMIT = 240
LANE_LIST_LIMIT = 80
STALE_WATCHER_S = 1800
FRICTION_HEADING = re.compile(r"(?im)^\W*friction\W*$")
KEY_NAME = re.compile(r"[a-zA-Z_-]+")
KEY_VALUE = re.compile(r"^[a-zA-Z_-]+=")
CONTROLLER_LANE = re.compile(r"^landing-L\d+")
M4_PATTERN = re.compile(r"\bM4\b|\bC(?:1[1-7]|2[89]|3[01])\b")
TICKET_PATTERN = re.compile(r"\bOMN-\d+\b")
PR_PATTERN = re.compile(r"\(#(\d+)\)")
REPO_PR_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9._-]*)#(\d+)\b")
FOOTER_PATTERN = re.compile(r"^\(via [A-Z][A-Za-z'-]*'s agent\)$")
SECRET_PATTERN = re.compile(
    r"xox[abprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}"
)
HOME_PATH_PATTERN = re.compile(r"/(?:Users|home|Volumes)/[A-Za-z]")
DEPENDENCY_TITLE = re.compile(r"^\s*(?:chore|build)\(deps(?:-dev)?\)", re.IGNORECASE)
TICKET_COVERAGE_LIMIT = 30
CHANGE_CLASS_PLAIN: dict[str, str] = {
    "feat": "new capabilities",
    "fix": "fixes",
    "docs": "documentation changes",
    "test": "test changes",
    "ci": "check and build setup changes",
    "chore": "upkeep changes",
    "refactor": "cleanups",
    "perf": "speed changes",
    "build": "build changes",
    "style": "formatting changes",
    "revert": "reverts",
    "other": "other changes",
}
OMITTED_LINE = re.compile(r"(?im)^\W*not listed\b.*$")
HEADLINE_PATTERN = re.compile(
    r"(\d+) changes? merged across (\d+) repositor(?:y|ies)", re.IGNORECASE
)
GAPS_HEADING = re.compile(r"(?im)^\W*gaps?\b")
DEPENDENCY_WORD = re.compile(r"dependenc", re.IGNORECASE)
UNCONFIRMED = re.compile(
    r"\b(?:unconfirmed|unverified|not (?:yet )?(?:confirmed|verified|shown|running|deployed)"
    r"|none confirmed|no confirmation)\b",
    re.IGNORECASE,
)
OVERCLAIM = re.compile(
    r"\b(?:can now|is now|are now|now (?:always|reads|runs|works|writes|passes))\b",
    re.IGNORECASE,
)
LANE_WORD = re.compile(r"\blanes?\b", re.IGNORECASE)
WORK_RUN_WORD = re.compile(r"\bwork runs?\b", re.IGNORECASE)
SUPERLATIVE = re.compile(
    r"(?<![\w-])(?:first|only|fully|entire(?:ly)?)(?![\w-])", re.IGNORECASE
)
MILESTONE_WORD = re.compile(r"\bmilestones?\b", re.IGNORECASE)
_JARGON_SOURCE = (
    (r"\bgreen\b", ("green",), "passing checks"),
    (r"\bred\b", ("red",), "failing checks"),
    (
        r"\b(?:un)?armed\b",
        ("armed", "unarmed"),
        "set (or not set) to merge automatically",
    ),
    (r"\bledger\b", ("ledger",), "shared work log"),
    (
        r"\blanding controller\b",
        ("landing controller",),
        "the program that lands pull requests",
    ),
    (r"\brouting receipts?\b", ("routing receipt",), "recorded routing evidence"),
    (r"\breceipts?\b", ("receipt",), "recorded evidence"),
    (r"\bsteering prompts?\b", ("steering prompt",), "operator corrections"),
    (r"\bticks?\b", ("tick",), "scheduled run"),
    (r"\bvoters?\b", ("voter",), "automated reviewer"),
    (r"\bgateway\b", ("gateway",), "review service"),
    (r"\bworktrees?\b", ("worktree",), "working copy"),
    (r"\bplacement\b", ("placement",), "where a job runs"),
    (r"\bplists?\b", ("plist",), "launch settings"),
    (r"\binstaller contract\b", ("installer contract",), "setup for the scheduled job"),
    (r"\blaunch units?\b", ("launch unit",), "scheduled-job setup"),
    (r"\bpinned\b", ("pinned",), "fixed to one"),
    (r"\bquorum\b", ("quorum",), "enough reviewers agreeing"),
    (r"\bheartbeat\b", ("heartbeat",), "status signal"),
    (r"\bcompaction\b", ("compaction",), "trimming a long session"),
    (r"\bclaims? tables?\b", ("claims table",), "record table"),
    (r"\bscorecard\b", ("scorecard",), "report card"),
)
JARGON: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), plain) for pattern, _, plain in _JARGON_SOURCE
)
PLAIN_WORDS: dict[str, str] = {
    term: plain for _, terms, plain in _JARGON_SOURCE for term in terms
} | {
    "lane": 'work run (write "work runs (lanes)" once, then either word)',
    "M4": 'milestones (write "M4 board (milestones)" once)',
}
CLASS_PLAIN: dict[str, str] = {
    "red": "failing checks",
    "green-unarmed": "passing checks, not set to merge automatically",
    "green-armed": "passing checks, set to merge automatically",
    "queue-stuck": "stuck in the merge queue",
    "held-excluded": "deliberately held back",
    "pending": "checks still running",
    "queued": "waiting in the merge queue",
    "unread": "not yet looked at",
}
DRAFT_RULES: tuple[str, ...] = (
    "After the greeting and the window, state the coverage headline. Rank items by what the owner must act on first: blocked or partial work, M4 rows, then changes that matter most to users or reliability. Cite what fits in 420 words, giving cited merged changes as repo#number with their ticket; dependency bumps get one clause with their count and no ids.",
    "Cite the M4, blocked and partial tickets in coverage.tickets and state every number in coverage.counts (the queue figures are as of generated_at). Name a ticket's kind as ticket_kinds gives it (a partial ticket is not blocked).",
    'End just above the footer with one line starting "Not listed:" that counts omitted merged changes by class, merged-only tickets and closed work runs by outcome, with coverage.full_list_link. Example: Not listed: 11 merged changes (6 fixes, 3 new capabilities, 2 documentation changes), 4 tickets, and the reports of 38 closed work runs (30 as done, 8 as handed-off). Full list: <coverage.full_list_link>',
    "The counts on the Not listed: line are what check recomputes from the facts; do not guess them.",
    "Put a lane's report under that lane's own ticket (lanes_closed ticket); when two reports of the same subject disagree, give both with their tickets.",
    "Say a merge as its title does (a configuration or code change); a merge is not a running service, so say what is unconfirmed.",
    "Scope every figure to its row: the snapshot time, the batch, the hosts checked; never write all or every unless one row says so.",
    'Use plain_words and open_by_class_plain for any term a non-specialist would not follow; write "work runs (lanes)" and "M4 board (milestones)" once.',
    "Cite a merged change inside the same parentheses or semicolon group as its ticket; call blocked work blocked and partial work partial on the line that cites its ticket; name the friction gap in one clause; do not write first, only, fully or entire.",
    "When a review answers REVISE, write the next review prompt with the earlier answers under the heading === EARLIER FINDINGS === and, after each finding, the line applied or not applied: <the fact that contradicts it>; the rubric and the facts do not change between rounds.",
)
COVERAGE_SCOPE = 'The check requires the M4, blocked and partial tickets and every number in counts. Merged changes, merged-only tickets and closed-lane reports that do not fit the 420 words are counted on the "Not listed:" line by class with the full list link. Lanes that handed off, were skipped or have no recorded outcome are counted in counts and not listed in lanes_closed; the other closed lanes are listed, blocked and partial first.'


def _change_class(title: str) -> str:
    """Return the plain class of a conventional-commit title."""
    match = re.match(r"^\s*(\w+)(?:\([^)]*\))?!?:", title)
    return CHANGE_CLASS_PLAIN.get(
        match.group(1).lower() if match else "other", CHANGE_CLASS_PLAIN["other"]
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _tickets(text: str) -> tuple[str, ...]:
    return tuple(
        sorted(set(TICKET_PATTERN.findall(text)), key=lambda t: int(t.split("-")[1]))
    )


def _draft_body(text: str) -> str:
    return OMITTED_LINE.sub("", text)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if done.returncode:
        message = done.stderr.strip().splitlines()
        raise OSError(message[0] if message else "git failed")
    return done.stdout


def _closes(terminal: dict[str, str], when: datetime, claim: dict[str, str]) -> bool:
    """True when this TERMINAL ends this CLAIM: same lane, and it names the claim or follows it."""
    if terminal.get("lane") != claim.get("lane"):
        return False
    if "closes-CLAIM" in terminal:
        return terminal["closes-CLAIM"] == claim["ts"]
    opened = _parse(claim["ts"])
    return opened is not None and when > opened


class HandlerOwnerSummary:
    """Run one owner-summary command."""

    def handle(self, request: ModelOwnerSummaryRequest) -> ModelOwnerSummaryResult:
        if request.command == "facts":
            return self._facts(request)
        if request.command == "check":
            return self._check(request)
        if request.command == "triage":
            return self._triage(request)
        return self._mark_sent(request)

    # -- window and watermark -------------------------------------------------

    @staticmethod
    def _state_file(request: ModelOwnerSummaryRequest) -> Path:
        return request.state_file or (
            request.root / ".onex_state" / "owner-summary" / "state.json"
        )

    def resolve_window(
        self, now: datetime, since: str, until: str, state_file: Path
    ) -> tuple[datetime, datetime, str]:
        """Return (since, until, source); the start is the last sent end when it is recent."""
        end = _parse(until) if until else now
        if end is None:
            raise ValueError(f"--until is not an ISO instant: {until}")
        if since:
            start = _parse(since)
            if start is None:
                raise ValueError(f"--since is not an ISO instant: {since}")
            if start >= end:
                raise ValueError("--since must be before the window end")
            return start, end, "--since"
        floor = end - timedelta(hours=MAX_WINDOW_HOURS)
        last: datetime | None = None
        try:
            last = _parse(
                str(
                    json.loads(state_file.read_text(encoding="utf-8")).get(
                        "last_sent_until", ""
                    )
                )
            )
        except (OSError, ValueError, AttributeError):
            last = None
        if last is not None and last < end:
            if last < floor:
                return floor, end, f"last-sent capped at {MAX_WINDOW_HOURS}h"
            return last, end, "last-sent"
        return (
            end - timedelta(hours=DEFAULT_WINDOW_HOURS),
            end,
            f"default {DEFAULT_WINDOW_HOURS}h",
        )

    def _mark_sent(self, request: ModelOwnerSummaryRequest) -> ModelOwnerSummaryResult:
        end = _parse(request.until)
        if end is None:
            return ModelOwnerSummaryResult(
                exit_code=2, message=f"--until is not an ISO instant: {request.until}"
            )
        path = self._state_file(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_sent_until": _iso(end),
                    "marked_at": _iso(datetime.now(UTC)),
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
        return ModelOwnerSummaryResult(
            exit_code=0, message=f"marked sent through {_iso(end)}"
        )

    # -- sources ---------------------------------------------------------------

    @staticmethod
    def _watcher(
        path: Path, start: datetime, end: datetime, now: datetime, errors: list[str]
    ) -> tuple[list[ModelMergedChange], dict[str, Any], int | None]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"pr watcher state unreadable: {path.name}: {exc}")
            return [], {}, None
        age: int | None = None
        last_tick = _parse(str(state.get("last_tick", "")))
        if last_tick is None:
            errors.append("pr watcher state has no last_tick")
        else:
            age = int((now - last_tick).total_seconds())
            if age > STALE_WATCHER_S:
                errors.append(f"pr watcher state is {age} s old; counts may be short")
        merged: list[ModelMergedChange] = []
        for record in (state.get("merges") or {}).values():
            merged_at = _parse(str(record.get("merged_at", "")))
            if merged_at is None or not (start < merged_at <= end):
                continue
            title = str(record.get("title", ""))
            merged.append(
                ModelMergedChange(
                    repo=str(record.get("repo", "")),
                    number=int(record.get("number", 0)),
                    title=title[:TEXT_LIMIT],
                    merged_at=_iso(merged_at),
                    tickets=_tickets(title),
                )
            )
        merged.sort(key=lambda m: m.merged_at)
        by_repo: dict[str, int] = {}
        by_class: dict[str, int] = {}
        drafts = 0
        prs = state.get("prs") or {}
        for key, record in prs.items():
            repo = key.rsplit("#", 1)[0]
            by_repo[repo] = by_repo.get(repo, 0) + 1
            cls = str(record.get("cls") or "unclassified")
            by_class[cls] = by_class.get(cls, 0) + 1
            drafts += bool((record.get("facts") or {}).get("draft"))
        queue = {
            "total": len(prs),
            "drafts": drafts,
            "by_repo": dict(sorted(by_repo.items(), key=lambda kv: -kv[1])),
            "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        }
        return merged, queue, age

    @staticmethod
    def _clones(
        root: Path, start: datetime, end: datetime, now: datetime, errors: list[str]
    ) -> tuple[dict[str, list[ModelCloneCommit]], int]:
        out: dict[str, list[ModelCloneCommit]] = {}
        fetch_age = 0
        seen: set[Path] = set()
        for entry in sorted(root.iterdir(), key=lambda e: ("-" in e.name, e.name)):
            if (
                entry.name in {"omni_worktrees", "node_modules"}
                or not (entry / ".git").exists()
            ):
                continue
            if entry.resolve() in seen:
                continue
            seen.add(entry.resolve())
            try:
                ref = _git(
                    entry, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"
                ).strip()
                log = _git(
                    entry,
                    "log",
                    "--first-parent",
                    f"--since={_iso(start)}",
                    f"--until={_iso(end)}",
                    "--format=%s",
                    ref,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"clone {entry.name} unreadable: {exc}")
                continue
            commits = []
            for subject in (line for line in log.splitlines() if line.strip()):
                match = PR_PATTERN.search(subject)
                commits.append(
                    ModelCloneCommit(
                        repo=entry.name,
                        pr=int(match.group(1)) if match else None,
                        tickets=_tickets(subject),
                        subject=subject[:TEXT_LIMIT],
                    )
                )
            if commits:
                out[entry.name] = commits
            fetch = entry / ".git" / "FETCH_HEAD"
            if fetch.is_file():
                fetch_age = max(fetch_age, int(now.timestamp() - fetch.stat().st_mtime))
        return out, fetch_age

    @staticmethod
    def _ledger_rows(
        path: Path, start: datetime, errors: list[str]
    ) -> list[dict[str, str]]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            errors.append(f"ledger unreadable: {exc}")
            return []
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for line in reversed(lines):
            moment = _parse(line[:20])
            if moment is None:
                continue
            if moment < start - timedelta(hours=1):
                break
            parts = [p.strip() for p in line.split(" | ")]
            if len(parts) < 3 or parts[1] not in {"CLAIM", "TERMINAL", "STATUS"}:
                continue
            cells: dict[str, str] = {"ts": parts[0], "type": parts[1], "text": ""}
            for part in parts[2:]:
                if not KEY_VALUE.match(part):
                    cells["text"] = (cells["text"] + " " + part).strip()
                    continue
                for token in part.split():
                    key, sep, value = token.partition("=")
                    if sep and KEY_NAME.fullmatch(key):
                        cells.setdefault(key, value)
                    else:
                        cells["text"] = (cells["text"] + " " + token).strip()
            identity = (cells["type"], cells.get("lane", ""), cells["ts"])
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(cells)
        rows.reverse()
        return rows

    @staticmethod
    def _listed_lanes(closed: list[ModelLaneClosed]) -> tuple[ModelLaneClosed, ...]:
        """Prioritize recent blocked and partial lanes, preserving source order."""
        priority = {"blocked", "partial"}
        counted_only = {"handed-off", "skipped", "skipped-owned", ""}
        indices = [
            i for i in reversed(range(len(closed))) if closed[i].outcome in priority
        ][:LANE_LIST_LIMIT]
        remaining = LANE_LIST_LIMIT - len(indices)
        indices.extend(
            [
                i
                for i in reversed(range(len(closed)))
                if closed[i].outcome not in priority | counted_only
            ][:remaining]
        )
        substantive = {
            closed[i].lane
            for i in indices
            if not closed[i].text.startswith("remote lane on ")
        }
        return tuple(
            closed[i]
            for i in sorted(indices)
            if not closed[i].text.startswith("remote lane on ")
            or closed[i].lane not in substantive
        )

    def _lanes(
        self, path: Path, start: datetime, end: datetime, errors: list[str]
    ) -> dict[str, Any]:
        rows = self._ledger_rows(path, start, errors)
        in_window = []
        for row in rows:
            moment = _parse(row["ts"])
            if moment is not None and start < moment <= end:
                in_window.append(row)
        closed: list[ModelLaneClosed] = []
        controller_workers = 0
        for row in in_window:
            if row["type"] != "TERMINAL":
                continue
            if CONTROLLER_LANE.match(row.get("lane", "")):
                controller_workers += 1
                continue
            closed.append(
                ModelLaneClosed(
                    ts=row["ts"],
                    lane=row.get("lane", ""),
                    ticket=row.get("ticket", ""),
                    outcome=row.get("outcome", ""),
                    pr=row.get("pr", ""),
                    delegated=row.get("delegated", ""),
                    codex=bool(row.get("codex")),
                    text=row["text"][:TEXT_LIMIT],
                )
            )
        terminals = [
            (r, moment)
            for r in rows
            if r["type"] == "TERMINAL"
            and (moment := _parse(r["ts"])) is not None
            and moment <= end
        ]
        started = [
            ModelLaneStarted(
                ts=r["ts"], lane=r.get("lane", ""), ticket=r.get("ticket", "")
            )
            for r in in_window
            if r["type"] == "CLAIM"
            and not CONTROLLER_LANE.match(r.get("lane", ""))
            and not any(_closes(terminal, when, r) for terminal, when in terminals)
        ]
        m4 = [
            ModelM4Row(
                ts=r["ts"],
                row_type=r["type"],
                lane=r.get("lane", ""),
                ticket=r.get("ticket", ""),
                outcome=r.get("outcome", ""),
                text=r["text"][:TEXT_LIMIT],
            )
            for r in in_window
            if r["type"] in {"TERMINAL", "STATUS"}
            and M4_PATTERN.search(f"{r.get('lane', '')} {r['text']}")
        ]
        outcomes: dict[str, int] = {}
        for item in closed:
            key = item.outcome or "unrecorded"
            outcomes[key] = outcomes.get(key, 0) + 1
        listed = self._listed_lanes(closed)
        return {
            "count": len(closed),
            "closed": listed,
            "omitted": len(closed) - len(listed),
            "outcomes": outcomes,
            "controller": controller_workers,
            "started": tuple(started),
            "m4": tuple(m4),
        }

    @staticmethod
    def _friction(
        path: Path | None, errors: list[str]
    ) -> tuple[Literal["ok", "unavailable"], str, tuple[str, ...]]:
        if path is None:
            return "unavailable", "no friction data path given", ()
        if not path.is_file():
            return "unavailable", "the friction data file does not exist yet", ()
        try:
            top = json.loads(path.read_text(encoding="utf-8"))["top_dropped"]
            if not isinstance(top, list):
                raise TypeError("top_dropped is not a list")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"friction data unreadable: {exc}")
            return "unavailable", f"the friction data file did not parse: {exc}", ()
        return (
            "ok",
            "",
            tuple(json.dumps(item, sort_keys=True)[:TEXT_LIMIT] for item in top[:5]),
        )

    # -- commands ---------------------------------------------------------------

    def _facts(self, request: ModelOwnerSummaryRequest) -> ModelOwnerSummaryResult:
        now = (
            _parse(request.now)
            if request.now
            else datetime.now(UTC).replace(microsecond=0)
        )
        if now is None:
            return ModelOwnerSummaryResult(
                exit_code=2, message=f"--now is not an ISO instant: {request.now}"
            )
        if request.ledger is None:
            return ModelOwnerSummaryResult(
                exit_code=2, message="--ledger is required for facts"
            )
        try:
            start, end, source = self.resolve_window(
                now, request.since, request.until, self._state_file(request)
            )
        except ValueError as exc:
            return ModelOwnerSummaryResult(exit_code=2, message=str(exc))
        errors: list[str] = []
        watcher_path = request.watcher_state or (
            request.root / ".claude_scratch" / "pr-watcher" / "state.json"
        )
        merged, queue, age = self._watcher(watcher_path, start, end, now, errors)
        clones, fetch_age = self._clones(request.root, start, end, now, errors)
        lanes = self._lanes(request.ledger, start, end, errors)
        friction_status, friction_reason, friction_top = self._friction(
            request.friction_data, errors
        )
        watcher_pairs = {(m.repo, m.number) for m in merged}
        clone_only = tuple(
            c
            for commits in clones.values()
            for c in commits
            if (c.repo, c.pr) not in watcher_pairs
        )
        by_repo: dict[str, int] = {}
        for change in merged:
            by_repo[change.repo] = by_repo.get(change.repo, 0) + 1
        facts = ModelOwnerSummaryFacts(
            generated_at=_iso(now),
            window_since=_iso(start),
            window_until=_iso(end),
            window_source=source,
            full_list_link=request.full_list_link,
            merged_changes=tuple(merged),
            clone_only_commits=clone_only,
            merged_by_repo=dict(sorted(by_repo.items(), key=lambda kv: -kv[1])),
            clone_commits_by_repo={r: len(c) for r, c in clones.items()},
            watcher_state_age_s=age,
            clone_fetch_age_max_s=fetch_age,
            open_total=int(queue.get("total", 0)),
            open_drafts=int(queue.get("drafts", 0)),
            open_by_repo=queue.get("by_repo", {}),
            open_by_class=queue.get("by_class", {}),
            lanes_closed_count=lanes["count"],
            lanes_closed=lanes["closed"],
            lanes_closed_omitted=lanes["omitted"],
            lanes_closed_by_outcome=lanes["outcomes"],
            controller_worker_lanes_closed=lanes["controller"],
            lanes_started_not_closed=lanes["started"],
            m4_rows=lanes["m4"],
            friction_status=friction_status,
            friction_reason=friction_reason,
            friction_top_dropped=friction_top,
            errors=tuple(errors),
        )
        facts = facts.model_copy(
            update={
                "coverage": self.coverage_of(facts),
                "open_by_class_plain": {
                    key: CLASS_PLAIN.get(key, key) for key in facts.open_by_class
                },
                "plain_words": PLAIN_WORDS.copy(),
                "draft_rules": DRAFT_RULES,
            }
        )
        return ModelOwnerSummaryResult(
            exit_code=0,
            message=f"{len(merged)} merges, {lanes['count']} lanes closed",
            facts=facts,
        )

    @staticmethod
    def coverage_of(facts: ModelOwnerSummaryFacts) -> ModelDraftCoverage:
        """Derive required coverage from source facts, including older facts files."""
        changes = tuple(
            change
            for change in facts.merged_changes
            if not DEPENDENCY_TITLE.match(change.title)
        )
        merged_tickets = tuple(
            dict.fromkeys(ticket for change in changes for ticket in change.tickets)
        )
        untracked_changes = tuple(
            f"{change.repo}#{change.number}" for change in changes if not change.tickets
        )
        n = len(facts.merged_changes)
        r = len({change.repo for change in facts.merged_changes})
        headline = ""
        if n:
            headline = (
                f"{n} {'change' if n == 1 else 'changes'} merged across "
                f"{r} {'repository' if r == 1 else 'repositories'}"
            )
        tickets = list(dict.fromkeys(row.ticket for row in facts.m4_rows if row.ticket))
        tickets.extend(ticket for ticket in merged_tickets if ticket not in tickets)
        for outcome in ("blocked", "partial"):
            for lane in facts.lanes_closed:
                if (
                    lane.outcome == outcome
                    and lane.ticket
                    and lane.ticket not in tickets
                ):
                    tickets.append(lane.ticket)
        tickets = tickets[:TICKET_COVERAGE_LIMIT]
        kinds: dict[str, set[str]] = {ticket: set() for ticket in tickets}
        for row in facts.m4_rows:
            if row.ticket in kinds:
                kinds[row.ticket].add("m4")
        for ticket in merged_tickets:
            if ticket in kinds:
                kinds[ticket].add("merged")
        for lane in facts.lanes_closed:
            if lane.ticket in kinds and lane.outcome in {"blocked", "partial"}:
                kinds[lane.ticket].add(lane.outcome)
        counts = {
            "lanes closed": facts.lanes_closed_count,
            "controller worker lanes closed": facts.controller_worker_lanes_closed,
            "lanes started and not closed": len(facts.lanes_started_not_closed),
            "open pull requests": facts.open_total,
        }
        counts.update(
            (CLASS_PLAIN.get(cls, cls), number)
            for cls, number in sorted(
                facts.open_by_class.items(), key=lambda item: (-item[1], item[0])
            )[:4]
        )
        counts["open pull requests that are drafts"] = facts.open_drafts
        counts.update(
            (f"lanes closed as {outcome}", number)
            for outcome, number in facts.lanes_closed_by_outcome.items()
            if number > 0
        )
        return ModelDraftCoverage(
            scope=COVERAGE_SCOPE,
            headline=headline,
            changes=tuple(f"{change.repo}#{change.number}" for change in changes),
            change_classes={
                f"{change.repo}#{change.number}": _change_class(change.title)
                for change in changes
            },
            full_list_link=facts.full_list_link,
            merged_tickets=merged_tickets,
            untracked_changes=untracked_changes,
            dependency_changes=n - len(changes),
            tickets=tuple(tickets),
            ticket_kinds={
                ticket: ",".join(sorted(values)) for ticket, values in kinds.items()
            },
            counts={label: number for label, number in counts.items() if number > 0},
        )

    @staticmethod
    def check_draft(text: str, facts: ModelOwnerSummaryFacts) -> list[str]:
        """Return every rule the draft breaks; an empty list is a pass."""
        problems: list[str] = []
        body = text.rstrip().splitlines()
        if not body or not FOOTER_PATTERN.match(body[-1].strip()):
            problems.append(
                "the last line must be the footer: (via <first name>'s agent)"
            )
        words = len(text.split())
        if words > MAX_WORDS:
            problems.append(f"{words} words; at most {MAX_WORDS}")
        if SECRET_PATTERN.search(text):
            problems.append("the draft holds a token-shaped string")
        if HOME_PATH_PATTERN.search(text):
            problems.append("the draft names a home or volume path")
        corpus = facts.model_dump_json()
        known_tickets = set(TICKET_PATTERN.findall(corpus))
        cited = _tickets(text)
        if not cited and (facts.merged_changes or facts.clone_only_commits):
            problems.append("the draft cites no ticket number")
        problems.extend(
            f"{t} is not in the facts" for t in cited if t not in known_tickets
        )
        pairs = {(m.repo, m.number) for m in facts.merged_changes}
        pairs |= {(c.repo, c.pr) for c in facts.clone_only_commits if c.pr}
        for repo, number in REPO_PR_PATTERN.findall(text):
            if (repo, int(number)) not in pairs:
                problems.append(
                    f"{repo}#{number} is not a merge in the facts; cite merged changes only"
                )
        if "M4" not in text:
            problems.append("the draft has no M4 board line")
        if facts.friction_status != "ok" and FRICTION_HEADING.search(text):
            problems.append(
                "the draft has a Friction section but the friction data is unavailable"
            )
        cov = HandlerOwnerSummary.coverage_of(facts)
        if cov.headline:
            headline = HEADLINE_PATTERN.search(text)
            expected_counts = (
                len(facts.merged_changes),
                len({change.repo for change in facts.merged_changes}),
            )
            actual_counts = None
            if headline:
                actual_counts = (int(headline.group(1)), int(headline.group(2)))
            if actual_counts != expected_counts:
                problems.append(f'the opening must say "{cov.headline}"')
        omitted_lines = list(OMITTED_LINE.finditer(text))
        coverage_body = _draft_body(text)
        if omitted_lines:
            if len(omitted_lines) != 1:
                problems.append('use one line starting "Not listed:"')
            lines = [line for line in text.rstrip().splitlines() if line.strip()]
            if len(lines) < 2 or not OMITTED_LINE.fullmatch(lines[-2]):
                problems.append('the "Not listed:" line must be just above the footer')
            if not omitted_lines[0].group().lstrip().startswith("Not listed:"):
                problems.append('the omitted line must start with "Not listed:"')
        uncited_changes = [
            change
            for change in cov.changes
            if not re.search(
                rf"(?<![\w.-]){re.escape(change)}(?![\w.-])", coverage_body
            )
        ]
        body_tickets = {
            ticket
            for ticket in cov.tickets
            if re.search(rf"(?<!\w){re.escape(ticket)}(?!\d)", coverage_body)
        }
        uncited_tickets = [
            ticket
            for ticket in cov.tickets
            if cov.ticket_kinds[ticket] == "merged" and ticket not in body_tickets
        ]
        omitted_lanes = facts.lanes_closed_by_outcome.copy()
        for lane in facts.lanes_closed:
            if lane.ticket and re.search(
                rf"(?<!\w){re.escape(lane.ticket)}(?!\d)", coverage_body
            ):
                outcome = lane.outcome or "unrecorded"
                omitted_lanes[outcome] = max(0, omitted_lanes.get(outcome, 0) - 1)
        omitted_lanes = {
            outcome: count for outcome, count in omitted_lanes.items() if count > 0
        }
        omitted_lane_total = sum(omitted_lanes.values())
        omitted_total = len(uncited_changes) + len(uncited_tickets) + omitted_lane_total
        if omitted_total and not omitted_lines:
            problems.append(
                f"{omitted_total} uncited rows are not named; end the draft with one line "
                'starting "Not listed:" that counts them by class (merged changes by '
                "class, tickets, closed work runs by outcome) and gives the full list link"
            )
        elif omitted_total:
            omitted_line = omitted_lines[0].group()
            expected: list[tuple[int, str, str]] = []
            if uncited_changes:
                expected.append(
                    (len(uncited_changes), r"merged changes?", "merged changes")
                )
                class_counts: dict[str, int] = {}
                for uncited in uncited_changes:
                    cls = cov.change_classes[uncited]
                    class_counts[cls] = class_counts.get(cls, 0) + 1
                expected.extend(
                    (count, re.escape(cls), cls) for cls, count in class_counts.items()
                )
            if uncited_tickets:
                expected.append((len(uncited_tickets), r"tickets?", "tickets"))
            if omitted_lane_total:
                expected.append(
                    (omitted_lane_total, r"closed work runs?", "closed work runs")
                )
                expected.extend(
                    (count, rf"as\s+{re.escape(outcome)}", f"as {outcome}")
                    for outcome, count in omitted_lanes.items()
                )
            for count, expression, label in expected:
                stated = re.findall(
                    rf"(?<![\w.,+-])(\d+)\s+{expression}(?![\w-])",
                    omitted_line,
                    re.IGNORECASE,
                )
                if [int(value) for value in stated] != [count]:
                    problems.append(f"unnamed omission: facts give {count} {label}")
            if cov.full_list_link and cov.full_list_link not in omitted_line:
                problems.append(
                    f"unnamed omission: full list link must be {cov.full_list_link}"
                )
        if cov.dependency_changes and not DEPENDENCY_WORD.search(text):
            problems.append(
                f"{cov.dependency_changes} dependency bumps merged; say so in one clause"
            )
        missing_tickets = [
            ticket
            for ticket in cov.tickets
            if {"m4", "blocked", "partial"}.intersection(
                cov.ticket_kinds[ticket].split(",")
            )
            and ticket not in body_tickets
        ]
        if missing_tickets:
            problems.append(
                "tickets not cited (M4 rows, blocked or partial lanes): "
                + ", ".join(missing_tickets)
            )
        missing_kinds: list[str] = []
        for ticket in cov.tickets:
            ticket_lines = [
                line
                for line in text.split("\n")
                if re.search(rf"(?<!\w){re.escape(ticket)}(?!\w)", line)
            ]
            if not ticket_lines:
                continue
            kinds = [
                kind
                for kind in ("blocked", "partial")
                if kind in cov.ticket_kinds[ticket].split(",")
                and not any(
                    re.search(rf"\b{kind}\b", line, re.IGNORECASE)
                    for line in ticket_lines
                )
            ]
            if kinds:
                missing_kinds.append(f"{ticket} ({', '.join(kinds)})")
        if missing_kinds:
            problems.append(
                "tickets whose blocked or partial work is not named as such: "
                + ", ".join(missing_kinds)
            )
        missing_counts = [
            f"{label} {number}"
            for label, number in cov.counts.items()
            if not re.search(rf"(?<![\w#-]){number}(?![\w-])", text)
        ]
        if missing_counts:
            problems.append("numbers not stated: " + "; ".join(missing_counts))
        for pattern, plain in JARGON:
            match = pattern.search(text)
            if match:
                problems.append(
                    f'"{match.group().lower()}" is shorthand; write "{plain}"'
                )
        if LANE_WORD.search(text) and not WORK_RUN_WORD.search(text):
            problems.append('define lanes once: write "work runs (lanes)"')
        if "M4" in text and not MILESTONE_WORD.search(text):
            problems.append('define M4 once: write "M4 board (milestones)"')
        for claim in dict.fromkeys(match.lower() for match in OVERCLAIM.findall(text)):
            problems.append(
                f'"{claim}" claims a running service; a merge is a change, '
                "say what is unconfirmed"
            )
        has_changes = bool(facts.merged_changes or facts.clone_only_commits)
        if has_changes and not UNCONFIRMED.search(text):
            problems.append(
                "the draft never says the merges are unconfirmed as running; "
                "a merge is not a deploy"
            )
        if facts.errors and not GAPS_HEADING.search(text):
            problems.append(
                "the facts hold errors; add a Gaps section that names each one"
            )
        groups = re.split(r"[();\n]", text)
        without_tickets: list[str] = []
        for change in facts.merged_changes:
            if not change.tickets or DEPENDENCY_TITLE.search(change.title):
                continue
            reference = f"{change.repo}#{change.number}"
            if any(
                re.search(rf"(?<![\w.-]){re.escape(reference)}(?![\w.-])", group)
                and not set(change.tickets).intersection(TICKET_PATTERN.findall(group))
                for group in groups
            ):
                label = f"{reference} ({change.tickets[0]})"
                if label not in without_tickets:
                    without_tickets.append(label)
        if without_tickets:
            problems.append(
                "merged changes cited without their ticket in the same group: "
                + ", ".join(without_tickets)
            )
        if facts.friction_status != "ok" and not re.search(
            r"\bfriction\b", text, re.IGNORECASE
        ):
            problems.append(
                "the friction data is unavailable; say so in one clause (not as a heading)"
            )
        for claim in dict.fromkeys(
            match.lower() for match in SUPERLATIVE.findall(text)
        ):
            problems.append(
                f'"{claim}" asserts what no fact row says; drop it or cite the row'
            )
        return problems

    @staticmethod
    def triage_findings(
        answer: str, draft: str, facts: ModelOwnerSummaryFacts
    ) -> list[tuple[str, str]]:
        """Separate accounted-for omissions from actionable reviewer findings."""
        sound = not any(
            "unnamed omission" in problem or "uncited rows are not named" in problem
            for problem in HandlerOwnerSummary.check_draft(draft, facts)
        )
        body = _draft_body(draft)
        coverage = HandlerOwnerSummary.coverage_of(facts)
        body_changes = {
            f"{repo}#{number}" for repo, number in REPO_PR_PATTERN.findall(body)
        }
        body_tickets = set(TICKET_PATTERN.findall(body))
        named_changes = set(coverage.changes) - body_changes
        named_tickets = {
            ticket
            for change in facts.merged_changes
            for ticket in change.tickets
            if not {"m4", "blocked", "partial"}.intersection(
                coverage.ticket_kinds.get(ticket, "").split(",")
            )
            and ticket not in body_tickets
        }
        named_lanes = {
            lane.lane
            for lane in facts.lanes_closed
            if lane.outcome not in {"blocked", "partial"}
            and (not lane.ticket or lane.ticket not in body_tickets)
        }
        by_number = {
            change.number: f"{change.repo}#{change.number}"
            for change in facts.merged_changes
        }
        results: list[tuple[str, str]] = []
        after_verdict = False
        for line in answer.splitlines():
            if not after_verdict:
                after_verdict = line.startswith("VERDICT:")
                continue
            if not line.strip():
                continue
            changes = [
                f"{repo}#{number}" for repo, number in REPO_PR_PATTERN.findall(line)
            ] + [
                by_number.get(int(number), f"merged_changes#{number}")
                for number in re.findall(r"merged_changes\[number=(\d+)\]", line)
            ]
            tickets = TICKET_PATTERN.findall(line)
            lanes = [
                a or b for a, b in re.findall(r'\blane=(?:"([^"]+)"|([^\s,;)]+))', line)
            ]
            named = (
                sound
                and (changes or tickets or lanes)
                and all(change in named_changes for change in changes)
                and all(ticket in named_tickets for ticket in tickets)
                and all(lane in named_lanes for lane in lanes)
            )
            results.append(("NAMED-OMISSION" if named else "FINDING", line))
        return results

    def _triage(self, request: ModelOwnerSummaryRequest) -> ModelOwnerSummaryResult:
        if (
            request.draft is None
            or request.facts_file is None
            or request.findings is None
        ):
            return ModelOwnerSummaryResult(
                exit_code=2, message="--draft, --facts and --findings are required"
            )
        try:
            facts = ModelOwnerSummaryFacts.model_validate_json(
                request.facts_file.read_text(encoding="utf-8")
            )
            results = self.triage_findings(
                request.findings.read_text(encoding="utf-8"),
                request.draft.read_text(encoding="utf-8"),
                facts,
            )
        except (OSError, ValueError) as exc:
            return ModelOwnerSummaryResult(exit_code=2, message=str(exc))
        findings = sum(label == "FINDING" for label, _ in results)
        return ModelOwnerSummaryResult(
            exit_code=int(findings > 0),
            message=f"triage: {findings} finding(s), {len(results) - findings} named omission(s)",
            problems=tuple(f"{label}: {line}" for label, line in results),
        )

    def _check(self, request: ModelOwnerSummaryRequest) -> ModelOwnerSummaryResult:
        if request.draft is None or request.facts_file is None:
            return ModelOwnerSummaryResult(
                exit_code=2, message="--draft and --facts are required"
            )
        try:
            facts = ModelOwnerSummaryFacts.model_validate_json(
                request.facts_file.read_text(encoding="utf-8")
            )
            text = request.draft.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return ModelOwnerSummaryResult(exit_code=2, message=str(exc))
        problems = self.check_draft(text, facts)
        if problems:
            return ModelOwnerSummaryResult(
                exit_code=1,
                message=f"{len(problems)} problem(s); rewrite the draft",
                problems=tuple(problems),
            )
        return ModelOwnerSummaryResult(exit_code=0, message="PASS")
