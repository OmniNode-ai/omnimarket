# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today: bring up every relevant plan for the day, ranked, with the next action.

Reads, in this order:
  * the plans repository's ``beta/plans`` at ``origin/main`` after a ``git fetch`` (never the local
    working tree, which lags), plus ``PLAN_LIFECYCLE.yaml`` there;
  * the caller's identity, resolved through the team identity declaration on every invocation from the
    Linear subject the snapshot's sidecar records (see :mod:`.handler_plans_today_member`); only an
    authenticated operator match runs the operator's mode;
  * the operator's mode: the pr-watcher state file (``$OMNI_HOME/.claude_scratch/pr-watcher/state.json``)
    is the only PR source, and no GitHub call of any kind is made;
  * member mode: the watcher is neither read nor required; the member's scoped pull requests are read with
    one ``gh pr view`` each under the member's own GitHub login, capped at 30;
  * the rolling ledger, only through the plugin's ``onex_ledger.py query`` reader;
  * a Linear snapshot file the caller wrote (``--linear-snapshot``) and its ``.json`` sidecar.

For each plan dated in the last 14 days, or naming an In Progress ticket, or naming an open PR (in member
mode: holding a ticket in the member's scope), it maps the plan's tasks to tickets and PRs, gives each task
one state (merged, open-green, open-red, not-started; in member mode a task outside the scope is
``unread``: other member, state not read), names the owning live lane or NONE, counts hours since the
newest movement, and gives the plan a verdict: STALLED, IN-PROGRESS, NOT-STARTED or DONE. It then acts:
one ledger MSG per STALLED plan with no owner (deduped per 12 hours), through the ledger-msg executor.

Exit 0 ok; 2 refused (bad input, stale watcher, missing source, identity or scope); 4 a MSG failed.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models.model_plans_today import ModelPlansTodayRequest, ModelPlansTodayResult
from ..models.model_plans_today_schema import PLANS_TODAY_SCHEMA_ID
from .handler_plans_today_member import (
    PR_READ_CAP,
    Identity,
    MemberRefusedError,
    member_scope,
    read_member_prs,
    resolve_identity,
)

SCHEMA_ID = PLANS_TODAY_SCHEMA_ID
MECHANISM_TICKET = "OMN-20099"
# GLM runs only through Claude Code: the Coding Plan terms bar direct API calls, so the cloud-glm backend
# pin of `onex delegate` is retired and always fails (omnimarket#3136, OMN-20173). The GLM rung is
# harness-delegate's claude-glm harness, flash first for cheap text.
HARNESS_DELEGATE_RELPATH = (
    "skills",
    "harness-delegate",
    "scripts",
    "harness_delegate.py",
)
GLM_HARNESS = "claude-glm"
GLM_MODEL = "glm-5.3-flash"
GLM_LANE = "plans-today"
LIVE_MINUTES = 45  # the owner-liveness window wave.py owners and drain_map.py use
MOVE_HOURS = 24
MSG_WINDOW_HOURS = 12
# Every child has a deadline (OMN-20787). A caller of this script has its own (a ten-minute tool timeout on
# 2026-10-09), so the budgets below sum to less than that: the sources take about ten seconds, the summary
# phase at most SUMMARY_BUDGET_S plus the GLM receipt margin, and a MSG send CHILD_TIMEOUT_S.
CHILD_TIMEOUT_S = 120
LEDGER_TIMEOUT_S = 300
SUMMARY_BUDGET_S = 330
DEFAULT_SUMMARY_TIMEOUT_S = 240
# harness_delegate.py refuses --timeout-s at or above its call budget less the cleanup reserve (GLM_TIMEOUT_MAX_S
# there is 174); a default of 240 made every GLM rung exit 64 before it started.
GLM_TIMEOUT_MAX_S = 170
GLM_RECEIPT_MARGIN_S = 30
SEND_TIMED_OUT = "a MSG send timed out"
# The ledger refuses a MSG whose id (<second>-<lane>) is already declared; the next second is a new id.
DUPLICATE_ID = "is already declared on this ledger"
ID_RETRY_WAIT_S = 1.1
REASON_CHARS = 300
PRIORITIES = ("urgent", "high", "medium", "low")
PRIORITY_SOURCES = ("plan-header", "linear", "none")
WATCHER_MAX_AGE_MIN = 90
PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"  # the omni plugin: the ledger reader, the MSG executor, the GLM harness
KB_ENV = "ONEX_PLANS_KB_PATH"

TICKET_RE = re.compile(r"\bOMN-\d+\b")
PR_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*)#(\d+)\b")
LABEL_RE = re.compile(
    r"^\s*(?:\*\*)?([A-Za-z]{1,4}\.?\d+(?:\.\d+)*[a-z]?)(?![A-Za-z0-9])"
)
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
ROW_RE = re.compile(r"^(?:(\S+?):)?L(\d+) (.*)$")
TASK_HEADERS = {"task", "tasks", "step", "item", "work", "deliverable"}
TICKET_HEADERS = ("ticket", "issue", "linear")
NOISE_HEADERS = (
    "depend",
    "after",
    "block",
    "need",
    "prereq",
    "requires",
    "state",
    "status",
    "note",
    "repo",
)
STATES = ("merged", "open-green", "open-red", "not-started")
UNREAD_STATE = (
    "unread"  # member mode: a task outside the member's scope; its state is never read
)
UNREAD_TEXT = "other member: state not read"
VERDICTS = ("STALLED", "IN-PROGRESS", "NOT-STARTED", "DONE")

# Set by tests to prove no GitHub call is made; the module itself never names the tool.
_SPAWNED: list[list[str]] = []


class RefusedError(Exception):
    """A source is missing or unusable; the message names the fix."""


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, TS_FMT).replace(tzinfo=UTC)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FMT)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group (it leads its own session), then reap the child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def run(
    argv: list[str],
    *,
    timeout: float | None = None,
    input: str | bytes | None = None,
    text: bool = True,
    **kw: Any,
) -> subprocess.CompletedProcess[Any]:
    """The one place a child process starts (tests patch it, and assert no GitHub client is spawned).

    The child has a deadline (CHILD_TIMEOUT_S unless `timeout` says otherwise), no stdin of the caller's
    (DEVNULL, or `input` through a file), and its own session so a timeout kills its descendants too.
    Output goes to temporary files, not pipes: a descendant that outlives the child and keeps the
    descriptor open cannot hold this call (subprocess.run waits for EOF on a pipe, a file has none).
    Raises subprocess.TimeoutExpired at the deadline.
    """
    _SPAWNED.append(list(argv))
    limit = CHILD_TIMEOUT_S if timeout is None else timeout
    with (
        tempfile.TemporaryFile() as out,
        tempfile.TemporaryFile() as err,
        tempfile.TemporaryFile() as feed,
    ):
        stdin: Any = subprocess.DEVNULL
        if input is not None:
            feed.write(input.encode() if isinstance(input, str) else input)
            feed.flush()
            feed.seek(0)
            stdin = feed
        proc = subprocess.Popen(
            argv, stdin=stdin, stdout=out, stderr=err, start_new_session=True, **kw
        )
        try:
            proc.wait(timeout=limit)
        except BaseException:
            _kill_group(proc)
            raise
        out.seek(0)
        err.seek(0)
        so, se = out.read(), err.read()
    if text:
        return subprocess.CompletedProcess(
            argv,
            proc.returncode,
            so.decode("utf-8", "replace"),
            se.decode("utf-8", "replace"),
        )
    return subprocess.CompletedProcess(argv, proc.returncode, so, se)


def timed_out(argv: list[str], limit: float | None) -> RefusedError:
    what = " ".join(str(a) for a in argv[:4])[:120]
    return RefusedError(
        f"`{what}` timed out after {CHILD_TIMEOUT_S if limit is None else limit:g} s and was killed"
    )


def omni_home(override: str | None = None) -> Path:
    home = (override or os.environ.get("OMNI_HOME", "")).strip()
    if not home:
        raise RefusedError(
            "OMNI_HOME is not set (fail fast, no default): export it or source session.env"
        )
    return Path(home)


# ----------------------------------------------------------------------------- sources: plans


def git(kb: Path, *args: str) -> str:
    argv = ["git", "-C", str(kb), *args]
    try:
        r = run(argv)
    except subprocess.TimeoutExpired:
        raise timed_out(argv, None) from None
    if r.returncode != 0:
        raise RefusedError(
            f"git {' '.join(args)} failed in {kb}: {r.stderr.strip()[:200]}"
        )
    return str(r.stdout)


def kb_revision(kb: Path, fetch: bool) -> str:
    if not (kb / ".git").exists():
        raise RefusedError(
            f"plans repository clone not found at {kb} (set --kb or {KB_ENV})"
        )
    if fetch:
        # A git-protocol fetch of the private plans repository. It is not a GitHub API call.
        git(kb, "fetch", "--quiet", "origin", "main")
    return git(kb, "rev-parse", "origin/main").strip()


def read_blobs(kb: Path, paths: list[str]) -> dict[str, str]:
    """Read many files at origin/main in one git process."""
    if not paths:
        return {}
    spec = "".join(f"origin/main:{p}\n" for p in paths)
    argv = ["git", "-C", str(kb), "cat-file", "--batch"]
    try:
        r = run(argv, input=spec.encode(), text=False)
    except subprocess.TimeoutExpired:
        raise timed_out(argv, None) from None
    out: dict[str, str] = {}
    data, i = r.stdout, 0
    for p in paths:
        nl = data.index(b"\n", i)
        head = data[i:nl].decode()
        if head.endswith("missing"):
            i = nl + 1
            continue
        size = int(head.split()[2])
        out[p] = data[nl + 1 : nl + 1 + size].decode("utf-8", "replace")
        i = nl + 1 + size + 1
    return out


def list_plan_files(kb: Path) -> list[str]:
    names = git(kb, "ls-tree", "--name-only", "origin/main", "beta/plans/").splitlines()
    skip = {"INDEX.md", "adversarial-rubric.md", "gmail-gems-plan-index.md"}
    return [
        n
        for n in names
        if n.endswith(".md")
        and n.rsplit("/", 1)[-1] not in skip
        and not n.rsplit("/", 1)[-1].startswith("ROLLING_PLAN_")
    ]


def registry_entries(text: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
    """A line scan of PLAN_LIFECYCLE.yaml for the wanted paths: title, date and ticket states."""
    out: dict[str, dict[str, Any]] = {}
    cur: str | None = None
    ent: dict[str, Any] = {}
    last_id: str | None = None
    for line in text.splitlines():
        m = re.match(r'^  - path: "(.*)"', line)
        if m:
            cur = m.group(1) if m.group(1) in wanted else None
            ent = {"tickets": {}} if cur else {}
            if cur:
                out[cur] = ent
            last_id = None
            continue
        if cur is None:
            continue
        m = re.match(r'^    (title|date|status|resolved_status): "?(.*?)"?$', line)
        if m:
            ent[m.group(1)] = m.group(2)
            continue
        m = re.match(r'^      - id: "(OMN-\d+)"', line)
        if m:
            last_id = m.group(1)
            continue
        m = re.match(r'^        type: "(\w+)"', line)
        if m and last_id:
            ent["tickets"][last_id] = m.group(1)
    return out


@dataclass
class Task:
    label: str
    text: str
    tickets: list[str] = field(default_factory=list)
    order: int = 0


@dataclass
class PlanDoc:
    path: str
    title: str
    date: str | None
    tasks: list[Task]
    header_tickets: list[str]
    named_tickets: list[str]
    named_prs: list[str]
    priority: str | None = (
        None  # declared by the plan's own `Priority:` header line, lowercase
    )


PRIORITY_HEADER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?priority(?:\*\*)?\s*:\s*(?:\*\*)?\s*(urgent|high|medium|low|p[0-3])\b",
    re.I,
)
PRIORITY_ALIASES = {
    "p0": "urgent",
    "p1": "high",
    "p2": "medium",
    "p3": "low",
    "normal": "medium",
}


def normalize_priority(raw: Any) -> str | None:
    """A Linear priority name (`Urgent`, `High`, `Medium`, `Low`, `No priority`) or a P0..P3 label, as one of PRIORITIES."""
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("label") or ""
    word = str(raw or "").strip().lower()
    word = PRIORITY_ALIASES.get(word, word)
    return word if word in PRIORITIES else None


def split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [c.strip() for c in body.split("|")]


MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
RUN_RE = re.compile(r"OMN-(\d+)((?:\s*(?:,|and|&|/)\s*\d{4,6}\b)*)")


def plain(text: str) -> str:
    """Markdown links to their text, emphasis and code marks removed."""
    return re.sub(r"`|\*\*", "", MD_LINK_RE.sub(r"\1", text)).strip()


def ids_in(text: str) -> list[str]:
    """Ticket ids, expanding the `OMN-1234, 1235` shorthand plans use."""
    seen: list[str] = []
    for m in RUN_RE.finditer(text):
        for n in [m.group(1), *re.findall(r"\d{4,6}", m.group(2))]:
            t = f"OMN-{n}"
            if t not in seen:
                seen.append(t)
    return seen


def ticket_only(cell: str) -> bool:
    """A cell that is nothing but ticket ids (`OMN-1234, 1235`), so it names no task of its own."""
    left = RUN_RE.sub("", cell)
    return bool(ids_in(cell)) and not re.sub(r"[\s,/&()]|\band\b|\bto\b", "", left)


def parse_plan(path: str, text: str, reg: dict[str, Any] | None) -> PlanDoc:
    lines = text.splitlines()
    title = plain(
        next(
            (ln.lstrip("# ").strip() for ln in lines if ln.startswith("# ")),
            path.rsplit("/", 1)[-1],
        )
    )
    if reg and reg.get("title"):
        title = title or reg["title"]
    date = None
    m = DATE_RE.match(path.rsplit("/", 1)[-1])
    if m:
        date = m.group(1)
    if not date and reg and DATE_RE.match(str(reg.get("date", ""))):
        date = str(reg["date"])[:10]
    if not date:
        m2 = re.search(r"^state_as_of:\s*(\d{4}-\d{2}-\d{2})", text, re.M)
        date = m2.group(1) if m2 else None

    header: list[str] = []
    priority: str | None = None
    for ln in lines[:60]:
        if ln.startswith("## "):
            break
        pm = PRIORITY_HEADER_RE.match(ln)
        if pm and priority is None:
            priority = normalize_priority(pm.group(1))
        if re.match(r"^\s*(\*\*)?(epic|tickets?)\b", ln, re.I):
            header += [t for t in ids_in(ln) if t not in header]

    tasks: dict[str, Task] = {}
    orphans: list[tuple[str, list[str]]] = []
    order = 0

    def add(label: str, text_: str, tickets: list[str]) -> None:
        nonlocal order
        key = label.lower()
        if key not in tasks:
            order += 1
            tasks[key] = Task(label=label, text=text_[:90], order=order)
        t = tasks[key]
        if len(text_) > len(t.text):
            t.text = text_[:90]
        for tk in tickets:
            if tk not in t.tickets:
                t.tickets.append(tk)

    i = 0
    while i < len(lines):
        ln = lines[i]
        if (
            ln.lstrip().startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])
        ):
            head = [h.lower() for h in split_row(ln)]
            ticket_cols = [
                j for j, h in enumerate(head) if any(k in h for k in TICKET_HEADERS)
            ]
            task_table = bool(ticket_cols) or (
                bool(head) and (head[0] in TASK_HEADERS or head[0].startswith("task"))
            )
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = split_row(lines[i])
                i += 1
                if not task_table or not cells:
                    continue
                first = plain(cells[0])
                lm = LABEL_RE.match(first)
                if ticket_cols:
                    tks = [
                        t
                        for j in ticket_cols
                        if j < len(cells)
                        for t in ids_in(cells[j])
                    ]
                else:
                    tks = [
                        t
                        for j, c in enumerate(cells)
                        if not (
                            j < len(head) and any(k in head[j] for k in NOISE_HEADERS)
                        )
                        for t in ids_in(c)
                    ]
                if not lm and ticket_only(first):
                    orphans.append((first[:40], tks or ids_in(first)))
                    continue
                if not lm and not tks:
                    continue
                add(lm.group(1) if lm else first[:60], first, tks)
            continue
        hm = re.match(r"^#{2,6}\s+(.*)$", ln) or re.match(
            r"^\s*(?:[-*]|\d+\.)\s+(.*)$", ln
        )
        if hm:
            body = plain(hm.group(1))
            lm = LABEL_RE.match(body)
            if lm and (ln.startswith("#") or TICKET_RE.search(body)):
                add(lm.group(1), body, ids_in(body))
        i += 1

    covered = {t for tk in tasks.values() for t in tk.tickets}
    for label, tks in orphans:
        rest = [t for t in tks if t not in covered]
        if rest:
            add(label, label, rest)
            covered.update(rest)
    merged: dict[tuple[str, ...], Task] = {}
    final: list[Task] = []
    for tk in sorted(tasks.values(), key=lambda t: t.order):
        key = tuple(sorted(tk.tickets))
        if key and key in merged:
            other = merged[key]
            if tk.label not in other.label:
                other.label = f"{other.label} ({tk.label})"
            continue
        if key:
            merged[key] = tk
        final.append(tk)
    tasks = {str(i): t for i, t in enumerate(final)}

    named_prs: list[str] = []
    for repo, num in PR_RE.findall(text):
        pr_key = f"{repo}#{num}"
        if pr_key not in named_prs:
            named_prs.append(pr_key)
    return PlanDoc(
        path=path,
        title=title,
        date=date,
        tasks=sorted(tasks.values(), key=lambda t: t.order),
        header_tickets=header,
        named_tickets=ids_in(text),
        named_prs=named_prs,
        priority=priority,
    )


# ----------------------------------------------------------------------------- sources: watcher


@dataclass
class Watcher:
    last_tick: datetime
    prs: dict[str, dict[str, Any]]
    by_ticket: dict[str, list[str]]


def load_watcher(path: Path, now: datetime, max_age_min: int) -> Watcher:
    if not path.is_file():
        raise RefusedError(
            f"pr-watcher state file not found: {path} (check: launchctl list ai.omninode.pr-watcher)"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    tick = parse_ts(data["last_tick"])
    age = (now - tick).total_seconds() / 60
    if age > max_age_min:
        raise RefusedError(
            f"pr-watcher state is {age:.0f} min old (last_tick {data['last_tick']}, limit {max_age_min}): "
            "the watcher is not running; check `launchctl list ai.omninode.pr-watcher` and its stdout log, "
            "or pass --max-watcher-age-min to accept older state on purpose"
        )
    prs: dict[str, dict[str, Any]] = data.get("prs", {})
    by_ticket: dict[str, list[str]] = {}
    for key, v in prs.items():
        f = v.get("facts", {})
        if f.get("state") not in ("OPEN", "MERGED"):
            continue
        blob = f"{f.get('title', '')}\n{f.get('body', '')}\n{f.get('head_ref', '')}"
        for t in set(TICKET_RE.findall(blob.replace("omn-", "OMN-"))):
            by_ticket.setdefault(t, []).append(key)
    return Watcher(tick, prs, by_ticket)


def pr_is_red(v: dict[str, Any]) -> bool:
    cls = str(v.get("cls", ""))
    return (
        cls == "red" or "red" in cls.split("-") or bool((v.get("ci") or {}).get("red"))
    )


# ----------------------------------------------------------------------------- sources: ledger


@dataclass
class LedgerRow:
    ts: datetime
    kind: str
    cells: list[str]
    where: str

    def cell(self, key: str) -> str:
        for c in self.cells[2:]:
            if c.startswith(key + "="):
                rest = c[len(key) + 1 :].split()
                return rest[0] if rest else ""
        return ""

    def names(self) -> set[str]:
        out: set[str] = set()
        for k in ("ticket", "tickets"):
            out.update(t for t in self.cell(k).split(",") if t)
        return out


def parse_ledger_rows(text: str) -> tuple[list[LedgerRow], str]:
    rows: list[LedgerRow] = []
    newest = ""
    for line in text.splitlines():
        if line.startswith("ROWS "):
            m = re.search(r"newest=(\S+)", line)
            newest = m.group(1) if m else ""
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(3).split(" | ")]
        try:
            ts = parse_ts(cells[0])
        except ValueError:
            continue
        rows.append(
            LedgerRow(
                ts,
                cells[1] if len(cells) > 1 else "",
                cells,
                f"{m.group(1) or 'ledger'}:L{m.group(2)}",
            )
        )
    return rows, newest


def read_ledger(
    window_days: int, plugin_root: Path | None
) -> tuple[list[LedgerRow], str]:
    if plugin_root is None:
        raise RefusedError(
            f"{PLUGIN_ROOT_ENV} is not set: the ledger is read only through the omni plugin's onex_ledger.py"
        )
    reader = plugin_root / "scripts" / "onex_ledger.py"
    if not reader.is_file():
        raise RefusedError(f"ledger reader not found: {reader}")
    if not os.environ.get("ONEX_LEDGER_PATH"):
        raise RefusedError(
            "ONEX_LEDGER_PATH is not set (no default): export it or source session.env"
        )
    argv = [
        sys.executable,
        str(reader),
        "query",
        "--since",
        f"{window_days}d",
        "--archives",
        "--limit",
        "1000000",
    ]
    try:
        r = run(argv, timeout=LEDGER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise timed_out(argv, LEDGER_TIMEOUT_S) from None
    if r.returncode != 0:
        raise RefusedError(
            f"onex_ledger.py query failed (exit {r.returncode}): {r.stderr.strip()[:200]}"
        )
    return parse_ledger_rows(r.stdout)


@dataclass
class LedgerIndex:
    newest_row: str
    last_by_ticket: dict[str, datetime]
    last_by_pr: dict[str, datetime]
    claims_by_ticket: dict[str, list[LedgerRow]]
    claims_by_pr: dict[str, list[LedgerRow]]
    lane_rows: dict[str, list[datetime]]
    terminals: dict[str, list[datetime]]


def index_ledger(rows: list[LedgerRow], newest: str) -> LedgerIndex:
    ix = LedgerIndex(newest, {}, {}, {}, {}, {}, {})
    for r in rows:
        lane = r.cell("lane") or r.cell("from")
        if lane.startswith("plans-today"):
            continue  # this skill's own MSG rows never count as movement
        if lane:
            ix.lane_rows.setdefault(lane, []).append(r.ts)
        if r.kind == "TERMINAL" and lane:
            ix.terminals.setdefault(lane, []).append(r.ts)
        prs = {p for p in r.cell("pr").split(",") if p}
        for t in r.names():
            if t not in ix.last_by_ticket or r.ts > ix.last_by_ticket[t]:
                ix.last_by_ticket[t] = r.ts
        for p in prs:
            if p not in ix.last_by_pr or r.ts > ix.last_by_pr[p]:
                ix.last_by_pr[p] = r.ts
        if r.kind == "CLAIM":
            for t in r.cell("ticket").split(","):
                if t:
                    ix.claims_by_ticket.setdefault(t, []).append(r)
            for p in prs:
                ix.claims_by_pr.setdefault(p, []).append(r)
    return ix


def owner_of(
    claims: list[LedgerRow], ix: LedgerIndex, now: datetime
) -> tuple[str, str]:
    """(state, lanes): OWNED-LIVE, OWNED-STALE or UNOWNED, by wave.py owners' rule; lanes comma-joined."""
    live: list[str] = []
    stale: list[str] = []
    for c in claims:
        lane = c.cell("lane")
        if not lane or lane in live or lane in stale:
            continue
        if any(t >= c.ts for t in ix.terminals.get(lane, [])):
            continue  # the lane closed with a TERMINAL after this claim
        last = max(ix.lane_rows.get(lane, [c.ts]))
        (live if (now - last).total_seconds() / 60 <= LIVE_MINUTES else stale).append(
            lane
        )
    if live:
        return "OWNED-LIVE", ",".join(sorted(live))
    if stale:
        return "OWNED-STALE", ",".join(sorted(stale))
    return "UNOWNED", ""


# ----------------------------------------------------------------------------- sources: linear


@dataclass
class LinearIssue:
    status: str
    status_type: str
    updated_at: datetime | None
    priority: str | None = None  # one of PRIORITIES, when the snapshot carries it


def load_linear(path: Path | None) -> dict[str, LinearIssue]:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    out: dict[str, LinearIssue] = {}
    if not text.lstrip().startswith(("{", "[")):
        # compact form, one issue per line: `OMN-1234 In Progress 2026-09-29T16:43:51Z`, optionally
        # followed by ` priority=High` (Urgent, High, Medium, Low)
        for line in text.splitlines():
            m = re.match(
                r"^(OMN-\d+)\s+(.+?)\s+(\d{4}-\d\d-\d\dT[\d:.]+Z?)(?:\s+priority=(.*?))?\s*$",
                line.strip(),
            )
            if m:
                when = datetime.fromisoformat(
                    m.group(3).replace("Z", "+00:00")
                ).astimezone(UTC)
                status = m.group(2)
                started = status.lower() in ("in progress", "in review")
                done = status.lower() in ("done", "canceled", "cancelled", "duplicate")
                out[m.group(1)] = LinearIssue(
                    status,
                    "started" if started else "completed" if done else "",
                    when,
                    normalize_priority(m.group(4)),
                )
        return out
    data = json.loads(text)
    issues = data.get("issues", []) if isinstance(data, dict) else data
    for i in issues:
        tid = i.get("id") or i.get("identifier")
        if not tid or not TICKET_RE.fullmatch(str(tid)):
            continue
        upd = i.get("updatedAt") or i.get("updated_at")
        updated: datetime | None = None
        if upd:
            updated = datetime.fromisoformat(
                str(upd).replace("Z", "+00:00")
            ).astimezone(UTC)
        out[str(tid)] = LinearIssue(
            str(i.get("status") or i.get("state") or ""),
            str(i.get("statusType") or ""),
            updated,
            normalize_priority(i.get("priority")),
        )
    return out


def linear_in_progress(li: LinearIssue | None) -> bool:
    return li is not None and (
        li.status.lower() == "in progress" or li.status_type == "started"
    )


def linear_done(li: LinearIssue | None) -> bool:
    return li is not None and (
        li.status_type in ("completed",) or li.status.lower() in ("done",)
    )


# ----------------------------------------------------------------------------- computation


@dataclass
class Sources:
    now: datetime
    watcher: Watcher
    ledger: LedgerIndex
    linear: dict[str, LinearIssue]
    registry: dict[str, dict[str, Any]]
    # None: the operator's mode, every ticket in scope. A set: member mode, only these tickets are read.
    scope: frozenset[str] | None = None
    pr_basis: str = "watcher"  # where PR state came from, in each state basis: watcher, or gh (member mode)

    def in_scope(self, ticket: str) -> bool:
        return self.scope is None or ticket in self.scope


def ticket_state(tid: str, src: Sources, reg: dict[str, Any] | None) -> dict[str, Any]:
    keys = src.watcher.by_ticket.get(tid, [])
    prs = [src.watcher.prs[k] for k in keys]
    open_prs = [k for k in keys if src.watcher.prs[k]["facts"]["state"] == "OPEN"]
    merged = [k for k in keys if src.watcher.prs[k]["facts"]["state"] == "MERGED"]
    li = src.linear.get(tid)
    basis = "none"
    if open_prs:
        red = [k for k in open_prs if pr_is_red(src.watcher.prs[k])]
        state = "open-red" if red else "open-green"
        basis = f"{src.pr_basis}-open-pr"
    elif merged:
        state, basis = "merged", f"{src.pr_basis}-merged-pr"
    elif linear_done(li):
        state, basis = "merged", "linear-completed"
    elif reg and reg.get("tickets", {}).get(tid) in ("completed", "canceled"):
        state, basis = "merged", "registry-completed"
    else:
        state = "not-started"
    claims = src.ledger.claims_by_ticket.get(tid, [])
    for k in open_prs:
        claims = claims + src.ledger.claims_by_pr.get(k, [])
    own_state, lane = owner_of(claims, src.ledger, src.now)
    pending = [k for k in open_prs if src.watcher.prs[k].get("cls") == "pending"]
    return {
        "id": tid,
        "state": state,
        "state_basis": basis,
        "prs": sorted(open_prs + merged),
        "open_prs": sorted(open_prs),
        "pending_prs": sorted(pending),
        "linear": li.status if li else "",
        "owner_state": own_state,
        "owner": lane,
        "prs_facts": len(prs),
    }


def task_rollup(states: list[str]) -> str:
    if not states:
        return "not-started"
    if "open-red" in states:
        return "open-red"
    if "open-green" in states:
        return "open-green"
    if all(s == "merged" for s in states):
        return "merged"
    return "not-started"


def short_lanes(lanes: list[str], n: int = 2) -> str:
    return ",".join(lanes[:n]) + (f"+{len(lanes) - n}" if len(lanes) > n else "")


def task_owner(tks: list[dict[str, Any]]) -> str:
    live = sorted(
        {
            ln
            for t in tks
            if t["owner_state"] == "OWNED-LIVE"
            for ln in t["owner"].split(",")
        }
    )
    if live:
        return short_lanes(live)
    stale = sorted(
        {
            ln
            for t in tks
            if t["owner_state"] == "OWNED-STALE"
            for ln in t["owner"].split(",")
        }
    )
    return f"NONE (stale {short_lanes(stale)})" if stale else "NONE"


def is_owned(owner: str) -> bool:
    return owner != "NONE" and not owner.startswith("NONE")


def build_plan(doc: PlanDoc, src: Sources, window_days: int) -> dict[str, Any] | None:
    reg = src.registry.get(doc.path)
    all_tickets = list(
        dict.fromkeys([t for tk in doc.tasks for t in tk.tickets] + doc.header_tickets)
    )
    plan_tickets = [t for t in all_tickets if src.in_scope(t)]
    if src.scope is not None and not plan_tickets:
        return None  # member mode: a plan holding none of the member's tickets is not ranked
    states = {t: ticket_state(t, src, reg) for t in plan_tickets}
    named_open = [
        p
        for p in doc.named_prs
        if p in src.watcher.prs and src.watcher.prs[p]["facts"]["state"] == "OPEN"
    ]

    reasons: list[str] = []
    if (
        doc.date
        and (src.now.date() - datetime.strptime(doc.date, "%Y-%m-%d").date()).days
        <= window_days
    ):
        reasons.append("dated")
    inprog = [t for t in plan_tickets if linear_in_progress(src.linear.get(t))]
    if inprog:
        reasons.append("in-progress-ticket")
    if named_open:
        reasons.append("open-pr")
    if src.scope is not None:
        reasons.append("member-scope")
    if not reasons:
        return None

    tasks_out: list[dict[str, Any]] = []
    for tk in doc.tasks:
        if src.scope is not None and not any(src.in_scope(t) for t in tk.tickets):
            tasks_out.append(
                {
                    "label": tk.label,
                    "text": tk.text,
                    "tickets": list(tk.tickets),
                    "prs": [],
                    "state": UNREAD_STATE,
                    "state_basis": UNREAD_TEXT,
                    "owner": UNREAD_TEXT,
                    "no_ticket": not tk.tickets,
                    "active": False,
                    "in_scope": False,
                }
            )
            continue
        tks = [states[t] for t in tk.tickets if src.in_scope(t)]
        if not tks:
            tasks_out.append(
                {
                    "label": tk.label,
                    "text": tk.text,
                    "tickets": [],
                    "prs": [],
                    "state": "not-started",
                    "state_basis": "no-ticket",
                    "owner": "NONE",
                    "no_ticket": True,
                    "active": False,
                    "in_scope": True,
                }
            )
            continue
        recent = [
            src.ledger.last_by_ticket[t["id"]]
            for t in tks
            if t["id"] in src.ledger.last_by_ticket
        ]
        active = any(linear_in_progress(src.linear.get(t["id"])) for t in tks) or any(
            (src.now - r).total_seconds() / 3600 < MOVE_HOURS for r in recent
        )
        tasks_out.append(
            {
                "label": tk.label,
                "text": tk.text,
                "active": active,
                "tickets": [t["id"] for t in tks],
                "prs": sorted({p for t in tks for p in t["prs"]}),
                "state": task_rollup([t["state"] for t in tks]),
                "state_basis": ",".join(sorted({t["state_basis"] for t in tks})),
                "owner": task_owner(tks),
                "no_ticket": False,
                "in_scope": True,
                "unread_tickets": [t for t in tk.tickets if not src.in_scope(t)],
            }
        )
    # header tickets that no task table maps still count for movement and ownership
    mapped = {t for tk in doc.tasks for t in tk.tickets}
    extra = [states[t] for t in doc.header_tickets if t not in mapped and t in states]

    movement: list[datetime] = []
    for t in plan_tickets:
        if t in src.ledger.last_by_ticket:
            movement.append(src.ledger.last_by_ticket[t])
        li = src.linear.get(t)
        if li and li.updated_at:
            movement.append(li.updated_at)
    pr_keys = sorted({p for s in states.values() for p in s["prs"]} | set(named_open))
    for p in pr_keys:
        v = src.watcher.prs.get(p)
        if v:
            upd = v["facts"].get("updated_at")
            if upd:
                movement.append(parse_ts(upd))
        if p in src.ledger.last_by_pr:
            movement.append(src.ledger.last_by_pr[p])
    last_move = max(movement) if movement else None
    hours_idle = (
        None
        if last_move is None
        else round((src.now - last_move).total_seconds() / 3600, 1)
    )
    moved = hours_idle is not None and hours_idle < MOVE_HOURS

    all_states = list(states.values())
    live_lanes = sorted(
        {
            ln
            for s in all_states
            if s["owner_state"] == "OWNED-LIVE"
            for ln in s["owner"].split(",")
        }
    )
    for p in named_open:
        st, lane = owner_of(src.ledger.claims_by_pr.get(p, []), src.ledger, src.now)
        if st == "OWNED-LIVE":
            live_lanes += [ln for ln in lane.split(",") if ln not in live_lanes]
    live_lanes.sort()
    stale_lanes = sorted(
        {
            ln
            for s in all_states
            if s["owner_state"] == "OWNED-STALE"
            for ln in s["owner"].split(",")
        }
        - set(live_lanes)
    )
    owner = (
        short_lanes(live_lanes)
        if live_lanes
        else (f"NONE (stale {short_lanes(stale_lanes)})" if stale_lanes else "NONE")
    )

    scoped = [t for t in tasks_out if t["in_scope"]]
    counts = {
        s: sum(1 for t in scoped if t["state"] == s and not t["no_ticket"])
        for s in STATES
    }
    counts["no-ticket"] = sum(1 for t in scoped if t["no_ticket"])
    counts[UNREAD_STATE] = len(tasks_out) - len(scoped)
    counts["not-started-active"] = sum(
        1
        for t in scoped
        if t["state"] == "not-started" and not t["no_ticket"] and t["active"]
    )
    ticketed = [t for t in scoped if not t["no_ticket"]]
    started = bool(
        any(s["state"] != "not-started" for s in all_states)
        or any(
            linear_in_progress(src.linear.get(t)) or linear_done(src.linear.get(t))
            for t in plan_tickets
        )
        or any(t in src.ledger.last_by_ticket for t in plan_tickets)
        or any(s["owner_state"] != "UNOWNED" for s in all_states)
        or named_open
    )
    red_unowned = [
        t for t in scoped if t["state"] == "open-red" and not is_owned(t["owner"])
    ] + [
        {"label": s["id"], "tickets": [s["id"]], "prs": s["prs"]}
        for s in extra
        if s["state"] == "open-red" and s["owner_state"] != "OWNED-LIVE"
    ]
    if ticketed and all(t["state"] == "merged" for t in ticketed):
        verdict, why = "DONE", "every ticketed task is merged"
    elif not started:
        verdict, why = (
            "NOT-STARTED",
            "no ticket has a PR, a claim, a ledger row or a started Linear state",
        )
    elif red_unowned:
        verdict, why = (
            "STALLED",
            f"open-red with no owner: {', '.join(t['label'] for t in red_unowned)}",
        )
    elif not moved:
        idle = (
            "no movement on record"
            if hours_idle is None
            else f"nothing moved in {hours_idle:g} h"
        )
        verdict, why = "STALLED", idle
    else:
        verdict, why = "IN-PROGRESS", f"last movement {hours_idle:g} h ago"

    plan: dict[str, Any] = {
        "path": doc.path,
        "title": doc.title,
        "date": doc.date,
        "selected_by": reasons,
        "verdict": verdict,
        "verdict_reason": why,
        "owner": owner,
        "hours_idle": hours_idle,
        "last_movement": fmt_ts(last_move) if last_move else None,
        "counts": counts,
        "tasks": tasks_out,
        "tickets": plan_tickets,
        "prs": pr_keys,
        "rank": 0,
    }
    plan["next_action"], plan["next_action_kind"], plan["next_action_ticket"] = (
        next_action(plan)
    )
    plan["priority"], plan["priority_source"] = plan_priority(
        doc, plan_tickets, src.linear
    )
    return plan


NEXT_ACTION_KINDS = (
    "dispatch-fix",
    "hand-to-drain",
    "dispatch",
    "land-or-close",
    "mint-tickets",
    "read-plan",
    "wait",
    "decide",
    "none",
)


def next_action(p: dict[str, Any]) -> tuple[str, str, str | None]:
    """(the sentence, its kind, the ticket it is about). The ticket is the first one of the task the sentence
    names, else the plan's first ticket, else None (a machine reader dispatches only on a ticket)."""
    tasks = [
        t for t in p["tasks"] if t.get("in_scope", True)
    ]  # an unread task is never dispatched
    v = p["verdict"]
    red = [t for t in tasks if t["state"] == "open-red"]
    unowned_red = [t for t in red if not is_owned(t["owner"])]
    green = [t for t in tasks if t["state"] == "open-green"]
    todo = [
        t
        for t in tasks
        if t["state"] == "not-started" and not t["no_ticket"] and not t.get("active")
    ] or [t for t in tasks if t["state"] == "not-started" and not t["no_ticket"]]
    untick = [t for t in tasks if t["no_ticket"]]
    first_plan_ticket = p["tickets"][0] if p["tickets"] else None

    def ref(t: dict[str, Any]) -> str:
        items = t["prs"] or t["tickets"]
        more = f" +{len(items) - 2}" if len(items) > 2 else ""
        return f"{t['label']} ({', '.join(items[:2])}{more})" if items else t["label"]

    def tk(t: dict[str, Any]) -> str | None:
        return t["tickets"][0] if t["tickets"] else first_plan_ticket

    if v == "DONE":
        return "none: every ticketed task is merged", "none", None
    if unowned_red:
        return (
            f"dispatch a fix lane for {ref(unowned_red[0])}, open-red with no owner",
            "dispatch-fix",
            tk(unowned_red[0]),
        )
    if v == "STALLED" and green:
        return (
            f"hand {ref(green[0])} to the drain, green with nothing moving",
            "hand-to-drain",
            tk(green[0]),
        )
    if todo:
        return (
            f"dispatch a lane for {ref(todo[0])}, the first task not started"
            + (f"; owner {p['owner']} is live" if is_owned(p["owner"]) else ""),
            "dispatch",
            tk(todo[0]),
        )
    if not tasks and not p["prs"]:
        tks = ", ".join(p["tickets"][:2]) or "no ticket"
        return (
            f"read the plan: no task table maps its ticket ({tks}) to work; cut its tasks into tickets, close it or park it",
            "read-plan",
            first_plan_ticket,
        )
    if not tasks and p["prs"]:
        if v == "STALLED":
            return (
                f"land or close {p['prs'][0]}, the plan's open PR, which nothing has touched",
                "land-or-close",
                first_plan_ticket,
            )
        return f"wait on {p['owner']}", "wait", first_plan_ticket
    if untick:
        return (
            f"mint tickets for {len(untick)} task(s) with none, starting with {untick[0]['label']}",
            "mint-tickets",
            None,
        )
    if green or red:
        if is_owned(p["owner"]):
            return f"wait on {p['owner']}", "wait", first_plan_ticket
        return "read the plan: nothing is dispatchable", "read-plan", first_plan_ticket
    if v == "NOT-STARTED":
        return (
            "decide: start the first task or park the plan",
            "decide",
            first_plan_ticket,
        )
    return "read the plan: nothing is dispatchable", "read-plan", first_plan_ticket


def plan_priority(
    doc: PlanDoc, tickets: list[str], linear: dict[str, LinearIssue]
) -> tuple[str, str]:
    """(priority, source): the plan's own `Priority:` header line, else the highest Linear priority among its
    tickets, else ("unset", "none")."""
    if doc.priority:
        return doc.priority, "plan-header"
    seen = [li.priority for t in tickets if (li := linear.get(t)) and li.priority]
    if seen:
        return min(seen, key=PRIORITIES.index), "linear"
    return "unset", "none"


def rank_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def klass(p: dict[str, Any]) -> int:
        owned = is_owned(p["owner"])
        v = p["verdict"]
        if v == "STALLED":
            return 1 if owned else 0
        if v == "IN-PROGRESS":
            return 3 if owned else 2
        return 4 if v == "NOT-STARTED" else 5

    def key(p: dict[str, Any]) -> tuple[int, float, str]:
        idle = p["hours_idle"] if p["hours_idle"] is not None else 1e9
        return (klass(p), -idle, p["path"])

    out = sorted(plans, key=key)
    for i, p in enumerate(out, 1):
        p["rank"] = i
    return out


# ----------------------------------------------------------------------------- summary prose


def n_scoped(p: dict[str, Any]) -> int:
    """The plan's tasks the run read (every task in the operator's mode)."""
    return sum(1 for t in p["tasks"] if t.get("in_scope", True))


def facts_block(p: dict[str, Any]) -> str:
    c = p["counts"]
    lines = [
        f"plan: {p['title']}",
        f"verdict: {p['verdict']}",
        f"tasks: {n_scoped(p)}",
        f"merged: {c['merged']}",
        f"open-green: {c['open-green']}",
        f"open-red: {c['open-red']}",
        f"not-started: {c['not-started']}",
        f"of the not-started, In Progress in Linear or touched in the last 24 hours: {c.get('not-started-active', 0)}",
        f"tasks with no ticket: {c['no-ticket']}",
        f"owner: {p['owner']}",
        "hours since anything moved: "
        + ("none on record" if p["hours_idle"] is None else f"{p['hours_idle']:g}"),
    ]
    return "\n".join(lines)


def template_summary(p: dict[str, Any]) -> str:
    c = p["counts"]
    idle = (
        "with no movement on record"
        if p["hours_idle"] is None
        else f"with the last movement {p['hours_idle']:g} hours ago"
    )
    return (
        f"Verdict {p['verdict']}: {n_scoped(p)} tasks, {c['merged']} merged, {c['open-green']} open-green, "
        f"{c['open-red']} open-red, {c['not-started']} not-started ({c.get('not-started-active', 0)} of them active), "
        f"{c['no-ticket']} with no ticket. Owner {p['owner']}, {idle}."
    )


NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
ID_RE = re.compile(
    r"\bOMN-\d+\b|\b[A-Za-z][A-Za-z0-9_.-]*#\d+\b|\b[A-Za-z]{1,4}\.?\d+(?:\.\d+)*\b"
)
VERDICT_WORDS = {
    "STALLED": ("stalled",),
    "IN-PROGRESS": ("in progress", "in-progress"),
    "NOT-STARTED": ("not started", "not-started"),
    "DONE": ("done",),
}


def factcheck(summary: str, p: dict[str, Any]) -> list[str]:
    """Findings that reject a summary: a number or identifier not in the computed facts, no verdict."""
    findings: list[str] = []
    text = facts_block(p) + "\n" + p["owner"] + "\n" + p["title"]
    known_nums = set(NUM_RE.findall(text))
    known_ids = {m.lower() for m in ID_RE.findall(text)}
    clean = summary
    for n in NUM_RE.findall(clean):
        if n not in known_nums:
            findings.append(f"number {n} is not in the computed facts")
    for ident in ID_RE.findall(clean):
        if ident.lower() not in known_ids and not NUM_RE.fullmatch(ident):
            findings.append(f"identifier {ident} is not in the computed facts")
    counts = p["counts"]
    label = r"(merged|open-green|open-red|not-started)"
    for m in re.finditer(rf"(\d+)\s+(?:tasks?\s+)?(?:are\s+|is\s+)?{label}", clean):
        if int(m.group(1)) != counts[m.group(2)]:
            findings.append(
                f"{m.group(1)} {m.group(2)} misstates the computed count {counts[m.group(2)]}"
            )
    total_form = r"(?=\s*(?:[,.;:()]|$|of which|consisting|including|and\b|with\s+\d|were|remain))"
    for m in re.finditer(
        rf"(\d+)(?:\s+total)?\s+tasks?\b{total_form}(?!\s+(?:are\s+|is\s+)?{label})",
        clean,
    ):
        if int(m.group(1)) != n_scoped(p):
            findings.append(
                f"{m.group(1)} tasks misstates the computed total {n_scoped(p)}"
            )
    for m in re.finditer(
        r"(\d+)\s+(?:tasks?\s+)?(?:with\s+|have\s+|has\s+)?no\s+ticket", clean
    ):
        if int(m.group(1)) != counts["no-ticket"]:
            findings.append(
                f"{m.group(1)} with no ticket misstates the computed count {counts['no-ticket']}"
            )
    if p["hours_idle"] is not None:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s+hours", clean):
            if float(m.group(1)) not in (float(f"{p['hours_idle']:g}"), 24.0):
                findings.append(
                    f"{m.group(1)} hours misstates the computed {p['hours_idle']:g}"
                )
    if not any(w in summary.lower() for w in VERDICT_WORDS[p["verdict"]]):
        findings.append(f"the summary omits the verdict {p['verdict']}")
    if len(summary) > 600 or "```" in summary:
        findings.append("the summary is too long or carries a code fence")
    return findings


def extract_response(stdout: str) -> tuple[str, str]:
    """The answer and the run id from `onex delegate` output (a wall of JSON lines)."""
    answer, run_id = "", ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        rid = _find_key(obj, "run_id") or _find_key(obj, "correlation_id")
        if rid and not run_id:
            run_id = str(rid)
        resp = _dig(obj, ("result", "terminal_payload", "payload", "response"))
        if isinstance(resp, str) and resp.strip():
            answer = resp.strip()
    return answer, run_id


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    for k in path:
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


def harness_path(plugin_root: Path | None) -> Path | None:
    return (
        None if plugin_root is None else plugin_root.joinpath(*HARNESS_DELEGATE_RELPATH)
    )


def glm_argv(prompt_file: Path, home: Path, timeout: float, harness: Path) -> list[str]:
    """The GLM rung: Claude Code on the GLM Coding Plan through harness-delegate, never an onex delegate pin."""
    timeout = max(1, min(int(timeout), GLM_TIMEOUT_MAX_S))
    return [
        sys.executable,
        str(harness),
        "run",
        "--harness",
        GLM_HARNESS,
        "--model",
        GLM_MODEL,
        "--task-class",
        "summarization",
        "--prompt-file",
        str(prompt_file),
        "--check",
        'test -s "$HARNESS_ANSWER_FILE"',
        "--lane",
        GLM_LANE,
        "--ticket",
        MECHANISM_TICKET,
        "--timeout-s",
        str(timeout),
        "--out-dir",
        str(home / ".onex_state" / "harness-delegate"),
    ]


def glm_argv_within(argv: list[str], left: float) -> list[str]:
    """`argv` with its --timeout-s cut to the seconds left (and to what the harness takes)."""
    out = list(argv)
    i = out.index("--timeout-s") + 1
    out[i] = str(max(1, min(int(out[i]), int(left), GLM_TIMEOUT_MAX_S)))
    return out


def extract_harness_answer(stdout: str) -> tuple[str, str]:
    """(answer, run id) from harness-delegate's last HARNESS-DELEGATE line and the receipt it names.

    Only an outcome of `done` (the harness ran and the check passed) carries an answer.
    """
    last = next(
        (
            line
            for line in reversed(stdout.splitlines())
            if line.startswith("HARNESS-DELEGATE ")
        ),
        "",
    )
    match = re.search(r"receipt=(\S+)", last)
    if not match:
        return "", ""
    try:
        rec = json.loads(Path(match.group(1)).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "", ""
    if not isinstance(rec, dict):
        return "", ""
    run_id = str(rec.get("run_id") or "")
    if rec.get("outcome") != "done" or not rec.get("answer_file"):
        return "", run_id
    try:
        answer = Path(str(rec["answer_file"])).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        answer = ""
    return answer, run_id


def delegate_prose(
    p: dict[str, Any],
    home: Path,
    timeout: int,
    deadline: float | None = None,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    """Two sentences: the lab through `onex delegate` (lane, then in-process), then GLM through Claude Code
    (harness-delegate claude-glm); template on any rejection. `deadline` is a time.monotonic() instant: no call
    starts after it, and none runs past it (the GLM call by its receipt margin), so the plan gets its template
    sentence instead of a wait."""
    onex = home / "omnibase_infra" / "scripts" / "onex"
    prompt = (
        "Write exactly two plain sentences that summarize this plan's status for an operator. "
        "Use only the facts below. Write every number exactly as given, say the verdict word, "
        "invent no ticket, no name and no decision, and do not write a next action.\n"
        + facts_block(p)
    )
    base = [
        str(onex),
        "delegate",
        prompt,
        "--task-type",
        "summarization",
        "--ticket",
        MECHANISM_TICKET,
        "--state-root",
        str(home / ".onex_state"),
        "--caller-lane",
        GLM_LANE,
        "--json",
    ]
    lane_flags = ["--bus", "kafka", "--lane", "dev", "--locus", "deployed-lane"]
    inproc_flags = ["--bus", "inmemory", "--locus", "in-process"]
    runs: list[str] = []
    rejected: list[str] = []
    if not onex.is_file():
        return {
            "summary": template_summary(p),
            "summary_source": "template",
            "runs": runs,
            "rejected": ["onex wrapper not found"],
        }
    with tempfile.TemporaryDirectory(prefix="plans-today-glm-") as tmp:
        prompt_file = Path(tmp) / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        # lab on the deployed lane first; the in-process retry only when the lane had no bound consumer; GLM second.
        queue: list[tuple[str, list[str]]] = [("lab", base + lane_flags)]
        harness = harness_path(plugin_root)
        if harness is not None and harness.is_file():
            queue.append(("glm", glm_argv(prompt_file, home, timeout, harness)))
        else:
            rejected.append(
                f"glm: harness_delegate.py not found under {PLUGIN_ROOT_ENV}"
            )
        while queue:
            name, argv = queue.pop(0)
            left = (
                timeout
                if deadline is None
                else min(timeout, deadline - time.monotonic())
            )
            if left < 1:
                rejected.append(f"{name}: summary budget exhausted")
                continue
            if name == "glm":
                argv = glm_argv_within(argv, left)
            try:
                # the harness ends its own run at --timeout-s and still writes its receipt; the margin lets it
                r = run(
                    argv,
                    timeout=min(left, GLM_TIMEOUT_MAX_S) + GLM_RECEIPT_MARGIN_S
                    if name == "glm"
                    else left,
                )
            except subprocess.TimeoutExpired:
                rejected.append(f"{name}: timed out")
                continue
            answer, run_id = (
                extract_harness_answer(r.stdout)
                if name == "glm"
                else extract_response(r.stdout)
            )
            if run_id:
                runs.append(run_id)
            if r.returncode != 0 or not answer:
                rejected.append(f"{name}: exit {r.returncode}, no answer")
                if name == "lab" and "no bound consumer" in (r.stderr + r.stdout):
                    queue.insert(0, ("lab-inprocess", base + inproc_flags))
                continue
            findings = factcheck(answer, p)
            if findings:
                rejected.append(f"{name}: rejected ({findings[0]})")
                continue
            return {
                "summary": " ".join(answer.split()),
                "summary_source": name,
                "runs": runs,
                "rejected": rejected,
            }
    return {
        "summary": template_summary(p),
        "summary_source": "template",
        "runs": runs,
        "rejected": rejected,
    }


def add_summaries(
    plans: list[dict[str, Any]],
    mode: str,
    limit: int,
    home: Path,
    timeout: int,
    workers: int,
    cache_path: Path,
    budget: float | None = None,
    plugin_root: Path | None = None,
) -> None:
    """Template for every plan; a delegated (and fact-checked) summary for the top `limit` started plans.

    A summary is cached by the hash of its facts block, so a rerun with unchanged facts spends no delegation.
    The whole delegation phase has one budget (SUMMARY_BUDGET_S unless given): a plan whose calls do not
    finish inside it keeps its template sentence (OMN-20787, the 2026-10-09 hang).
    """
    deadline = time.monotonic() + (SUMMARY_BUDGET_S if budget is None else budget)
    for p in plans:
        (
            p["summary"],
            p["summary_source"],
            p["delegation_runs"],
            p["summary_rejections"],
        ) = template_summary(p), "template", [], []
    if mode == "none":
        return
    try:
        cache: dict[str, Any] = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    targets = [
        p for p in plans if mode == "all" or p["verdict"] in ("STALLED", "IN-PROGRESS")
    ]
    if mode != "all":
        targets = targets[:limit]
    todo: list[dict[str, Any]] = []
    for p in targets:
        hit = cache.get(hashlib.sha1(facts_block(p).encode()).hexdigest())
        if (
            hit
            and hit.get("summary_source") != "template"
            and not factcheck(hit["summary"], p)
        ):
            p["summary"], p["summary_source"] = hit["summary"], hit["summary_source"]
            p["delegation_runs"] = hit.get("runs", [])
        else:
            todo.append(p)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {
            pool.submit(delegate_prose, p, home, timeout, deadline, plugin_root): p
            for p in todo
        }
        for f, p in futs.items():
            res = f.result()
            p["summary"], p["summary_source"] = res["summary"], res["summary_source"]
            p["delegation_runs"], p["summary_rejections"] = res["runs"], res["rejected"]
            if res["summary_source"] != "template":
                cache[hashlib.sha1(facts_block(p).encode()).hexdigest()] = {
                    "summary": res["summary"],
                    "summary_source": res["summary_source"],
                    "runs": res["runs"],
                }
    if todo:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )


# ----------------------------------------------------------------------------- acting


def msg_key(p: dict[str, Any]) -> str:
    return hashlib.sha1(f"{p['path']}|{p['verdict']}".encode()).hexdigest()[:16]


def load_dedupe(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def msg_text(p: dict[str, Any]) -> str:
    text = (
        f"STALLED plan with no owner: {p['title']} ({p['path']}), {p['verdict_reason']}; owner {p['owner']}. "
        f"Next action: {p['next_action']}. Source: /omni:plans-today"
    )
    return text.replace("|", "/").replace("\n", " ")[:600]


def send_reason(r: subprocess.CompletedProcess[Any]) -> str:
    """Why a send failed: the executor's REFUSED/RETRY line (it prints refusals on stdout), else the last stderr line."""
    out = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    err = [x.strip() for x in r.stderr.splitlines() if x.strip()]
    refusal = next((x for x in out if x.startswith(("REFUSED", "RETRY"))), "")
    return (refusal or (err[-1] if err else out[0] if out else "no output"))[
        :REASON_CHARS
    ]


def emit_msg(
    p: dict[str, Any], to: str, lane: str, home: Path, plugin_root: Path | None = None
) -> tuple[bool, str]:
    """Send one MSG. (sent, the OK line, or ``rc=<n> <reason>`` for a failure).

    The row id is ``<second>-<lane>``, so a second MSG in the same second is refused as a duplicate id (exit 65):
    the send waits out that second and goes once more.
    """
    if plugin_root is None:
        return (
            False,
            f"rc=-1 {PLUGIN_ROOT_ENV} is not set: the MSG goes through the omni plugin's skill_exec.py",
        )
    exe = plugin_root / "scripts" / "skill_exec.py"
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin_root))
    try:
        r = run([sys.executable, str(exe), "newdir", "ledger-msg"], env=env)
        if r.returncode != 0:
            return (
                False,
                f"rc={r.returncode} skill_exec newdir failed: {send_reason(r)}",
            )
        d = Path(r.stdout.strip().splitlines()[-1])
        args = d / "args.txt"
        args.write_text(
            f"msg --lane {lane} --to {to} --ticket {MECHANISM_TICKET}\n---\n{msg_text(p)}\n",
            encoding="utf-8",
        )
        for attempt in (1, 2):
            r = run(
                [sys.executable, str(exe), "ledger-msg", "--args-file", str(args)],
                env=env,
            )
            if (
                attempt == 1
                and r.returncode == 65
                and DUPLICATE_ID in r.stdout + r.stderr
            ):
                time.sleep(ID_RETRY_WAIT_S)
                continue
            break
    except subprocess.TimeoutExpired:
        return False, f"{SEND_TIMED_OUT} after {CHILD_TIMEOUT_S:g} s"
    line = (r.stdout.strip().splitlines() or [r.stderr.strip()[:160]])[0]
    if r.returncode == 0 and line.startswith("OK MSG"):
        return True, line
    return False, f"rc={r.returncode} {send_reason(r)}"


def act(
    plans: list[dict[str, Any]],
    out_root: Path,
    now: datetime,
    to: str,
    lane: str,
    home: Path,
    no_msg: bool,
    plugin_root: Path | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """One MSG per STALLED plan with no owner, deduped over MSG_WINDOW_HOURS. (results, all_ok)."""
    dedupe_path = out_root / "msg-dedupe.json"
    dedupe = load_dedupe(dedupe_path)
    results: list[dict[str, str]] = []
    ok = True
    stuck = ""  # one send that timed out means the ledger path is stuck: the rest are not tried, each would wait as long
    for p in plans:
        if p["verdict"] != "STALLED" or is_owned(p["owner"]):
            continue
        key = msg_key(p)
        prior = dedupe.get(key)
        if prior and (now - parse_ts(prior)) < timedelta(hours=MSG_WINDOW_HOURS):
            results.append(
                {
                    "plan": p["path"],
                    "action": "skipped",
                    "detail": f"already sent {prior}",
                }
            )
            continue
        if no_msg:
            results.append(
                {
                    "plan": p["path"],
                    "action": "would-send",
                    "detail": "--no-msg (dry run only)",
                }
            )
            continue
        if stuck:
            ok = False
            results.append(
                {
                    "plan": p["path"],
                    "action": "failed",
                    "detail": f"not sent: an earlier send timed out ({stuck})",
                }
            )
            continue
        sent, line = emit_msg(p, to, lane, home, plugin_root)
        if not sent and line.startswith(SEND_TIMED_OUT):
            stuck = line
        if sent:
            dedupe[key] = fmt_ts(now)
            results.append({"plan": p["path"], "action": "sent", "detail": line})
        else:
            ok = False
            results.append({"plan": p["path"], "action": "failed", "detail": line})
    if any(r["action"] == "sent" for r in results):
        out_root.mkdir(parents=True, exist_ok=True)
        dedupe_path.write_text(
            json.dumps(dedupe, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return results, ok


# ----------------------------------------------------------------------------- output


def validate(data: Any) -> list[str]:
    """Every finding against the plans-today schema; an empty list means valid."""
    f: list[str] = []
    if not isinstance(data, dict):
        return ["not an object"]
    for k in ("schema", "generated_at", "mode", "scope", "sources", "plans", "msgs"):
        if k not in data:
            f.append(f"missing {k}")
    if data.get("schema") != SCHEMA_ID:
        f.append(f"schema must be {SCHEMA_ID}")
    mode = data.get("mode")
    if mode not in ("operator", "member"):
        f.append(f"mode {mode!r} is not one of ('operator', 'member')")
    scope = data.get("scope", {})
    for k in ("kind", "member", "tickets", "assigned", "claimed"):
        if k not in scope:
            f.append(f"scope lacks {k}")
    if scope.get("kind") != ("member" if mode == "member" else "all"):
        f.append(f"scope.kind {scope.get('kind')!r} does not match mode {mode!r}")
    src = data.get("sources", {})
    for k in (
        "kb_internal_origin_main",
        "watcher_last_tick",
        "ledger_newest_row",
        "linear_snapshot",
        "goal_state_as_of",
        "pr_source",
        "pr_reads",
        "pr_read_cap",
        "team_identity",
        "linear_sidecar",
    ):
        if k not in src:
            f.append(f"sources lacks {k}")
    expected_source = "gh-pr-view" if mode == "member" else "pr-watcher"
    if src.get("pr_source") != expected_source:
        f.append(
            f"sources.pr_source {src.get('pr_source')!r} must be {expected_source!r} in mode {mode!r}"
        )
    for i, p in enumerate(data.get("plans", [])):
        for k in (
            "path",
            "title",
            "verdict",
            "rank",
            "owner",
            "hours_idle",
            "tasks",
            "next_action",
            "next_action_kind",
            "next_action_ticket",
            "priority",
            "priority_source",
            "summary",
            "summary_source",
        ):
            if k not in p:
                f.append(f"plans[{i}] lacks {k}")
        if "priority" in p and p["priority"] not in (*PRIORITIES, "unset"):
            f.append(
                f"plans[{i}].priority {p['priority']!r} is not one of {(*PRIORITIES, 'unset')}"
            )
        if "priority_source" in p and p["priority_source"] not in PRIORITY_SOURCES:
            f.append(
                f"plans[{i}].priority_source {p['priority_source']!r} is not one of {PRIORITY_SOURCES}"
            )
        if "next_action_kind" in p and p["next_action_kind"] not in NEXT_ACTION_KINDS:
            f.append(
                f"plans[{i}].next_action_kind {p['next_action_kind']!r} is not one of {NEXT_ACTION_KINDS}"
            )
        nat = p.get("next_action_ticket", "")
        if nat is not None and not (isinstance(nat, str) and TICKET_RE.fullmatch(nat)):
            f.append(f"plans[{i}].next_action_ticket must be an OMN id or null")
        if p.get("verdict") not in VERDICTS:
            f.append(
                f"plans[{i}].verdict {p.get('verdict')!r} is not one of {VERDICTS}"
            )
        if p.get("summary_source") not in ("lab", "lab-inprocess", "glm", "template"):
            f.append(f"plans[{i}].summary_source is invalid")
        h = p.get("hours_idle")
        if h is not None and not isinstance(h, (int, float)):
            f.append(f"plans[{i}].hours_idle must be a number or null")
        unread = {
            t
            for x in p.get("tasks", [])
            if x.get("in_scope") is False
            for t in x.get("tickets", [])
        }
        if nat is not None and nat in unread:
            f.append(
                f"plans[{i}].next_action_ticket {nat} is outside the scope and must not be dispatched"
            )
        for j, t in enumerate(p.get("tasks", [])):
            for k in ("label", "tickets", "prs", "owner", "in_scope"):
                if k not in t:
                    f.append(f"plans[{i}].tasks[{j}] lacks {k}")
            if t.get("in_scope") is False:
                if t.get("state") != UNREAD_STATE or t.get("prs"):
                    f.append(
                        f"plans[{i}].tasks[{j}] is outside the scope and must be unread with no PR"
                    )
            elif t.get("state") not in STATES:
                f.append(
                    f"plans[{i}].tasks[{j}].state {t.get('state')!r} is not one of {STATES}"
                )
    return f


def scope_line(scope: dict[str, Any]) -> str:
    if scope["kind"] == "all":
        return "all (the operator's mode: every plan task is read)"
    return (
        f"member {scope['member']}: {len(scope['tickets'])} tickets "
        f"({len(scope['assigned'])} assigned, {len(scope['claimed'])} on open claims); other members' tasks unread"
    )


def pr_source_line(src: dict[str, Any]) -> str:
    if src["pr_source"] == "pr-watcher":
        return f"pr-watcher state (last tick {src['watcher_last_tick']})"
    return f"gh pr view under the member's login ({src['pr_reads']} of at most {src['pr_read_cap']} reads)"


def line_for(p: dict[str, Any]) -> str:
    idle = "n/a" if p["hours_idle"] is None else f"{p['hours_idle']:g}h"
    unread = p["counts"].get(UNREAD_STATE, 0)
    other = f"  ({unread} task(s): {UNREAD_TEXT})" if unread else ""
    return (
        f"{p['rank']:>2}. {p['verdict']:<11} owner={p['owner']} idle={idle}  {p['title'][:70]}  "
        f"[{p['path'].rsplit('/', 1)[-1]}]  next: {p['next_action']}{other}"
    )


def render_detail(doc: dict[str, Any]) -> str:
    out = [f"# plans-today {doc['generated_at']}", ""]
    s = doc["sources"]
    out += [
        f"- mode: {doc['mode']}; scope: {scope_line(doc['scope'])}",
        f"- pull request source: {pr_source_line(s)}",
        f"- team identity: {s['team_identity']}",
        f"- plans repository origin/main: {s['kb_internal_origin_main']}",
        f"- pr-watcher last tick: {s['watcher_last_tick']}",
        f"- ledger newest row read: {s['ledger_newest_row']}",
        f"- linear snapshot: {s['linear_snapshot']} (sidecar {s['linear_sidecar']})",
        f"- beta/GOAL.md state_as_of: {s['goal_state_as_of']} (the week's rows live there; this list does not repeat its dropped-work sections)",
        f"- plans selected: {len(doc['plans'])}",
        "",
    ]
    for p in doc["plans"]:
        out += [
            f"## {p['rank']}. {p['verdict']}: {p['title']}",
            f"path {p['path']}; selected by {', '.join(p['selected_by'])}; owner {p['owner']}; hours idle "
            f"{p['hours_idle']}; last movement {p['last_movement']}",
            f"why: {p['verdict_reason']}",
            f"priority: {p['priority']} ({p['priority_source']}); next action ({p['next_action_kind']}, ticket {p['next_action_ticket'] or 'none'}): {p['next_action']}",
            f"summary ({p['summary_source']}): {p['summary']}",
        ]
        if p.get("delegation_runs"):
            out.append("delegation run ids: " + ", ".join(p["delegation_runs"]))
        for r in p.get("summary_rejections", []):
            out.append(f"summary rejected: {r}")
        out += [
            "",
            "| task | tickets | prs | state | owner | basis |",
            "| -- | -- | -- | -- | -- | -- |",
        ]
        for t in p["tasks"]:
            state = UNREAD_TEXT if t["state"] == UNREAD_STATE else t["state"]
            out.append(
                f"| {t['label']} | {', '.join(t['tickets']) or 'no-ticket'} | {', '.join(t['prs']) or '-'} | "
                f"{state} | {t['owner']} | {t['state_basis']} |"
            )
        out.append("")
    if doc["msgs"]:
        out += (
            ["## MSGs", ""]
            + [f"- {m['action']}: {m['plan']} ({m['detail']})" for m in doc["msgs"]]
            + [""]
        )
    return "\n".join(out)


def _plugin_root(req: ModelPlansTodayRequest) -> Path | None:
    raw = req.plugin_root or os.environ.get(PLUGIN_ROOT_ENV, "").strip()
    return Path(raw) if raw else None


def _identity_line(identity: Identity) -> str:
    source = identity.result.source
    where = (
        "no declaration" if source is None else f"{source.path} sha256={source.sha256}"
    )
    return f"{where}; privileges={identity.result.privileges} entry={identity.result.entry_id or 'none'}"


def run_plans_today(req: ModelPlansTodayRequest) -> ModelPlansTodayResult:
    """One run; a refusal is a result with exit code 2 and the reason, never an exception."""
    try:
        return _run(req)
    except (RefusedError, MemberRefusedError) as e:
        return ModelPlansTodayResult(exit_code=2, mode=None, refused=str(e), stdout="")


def _run(req: ModelPlansTodayRequest) -> ModelPlansTodayResult:
    home = omni_home(req.omni_home)
    now = parse_ts(req.now) if req.now else datetime.now(UTC).replace(microsecond=0)
    kb_raw = req.kb or os.environ.get(KB_ENV, "").strip()
    if not kb_raw:
        raise RefusedError(f"no plans repository clone: pass --kb or set {KB_ENV}")
    kb = Path(kb_raw)
    if not req.linear_snapshot and not req.no_linear:
        raise RefusedError(
            "no Linear snapshot: fetch it with the Linear MCP (SKILL.md step 1) and pass --linear-snapshot <file>; "
            "--no-linear is for dry runs and tests"
        )
    snapshot = Path(req.linear_snapshot) if req.linear_snapshot else None
    # The role is resolved here, on every invocation, so no caller passes a mode.
    identity = resolve_identity(snapshot, req.overlay_roots)
    member = identity.mode == "member"
    linear = load_linear(snapshot)
    plugin_root = _plugin_root(req)

    watcher: Watcher | None = None
    if not member:
        watcher = load_watcher(
            Path(req.watcher)
            if req.watcher
            else home / ".claude_scratch" / "pr-watcher" / "state.json",
            now,
            req.max_watcher_age_min,
        )

    sha = kb_revision(kb, fetch=not req.no_fetch)
    files = list_plan_files(kb)
    blobs = read_blobs(kb, [*files, "beta/plans/PLAN_LIFECYCLE.yaml", "beta/GOAL.md"])
    gm = re.search(r"^state_as_of:\s*(\S+)", blobs.get("beta/GOAL.md", ""), re.M)
    goal_as_of = gm.group(1) if gm else "absent"
    reg = registry_entries(blobs.get("beta/plans/PLAN_LIFECYCLE.yaml", ""), set(files))

    if req.ledger_rows_file:
        rows, newest = parse_ledger_rows(
            Path(req.ledger_rows_file).read_text(encoding="utf-8")
        )
        newest = newest or (fmt_ts(max(r.ts for r in rows)) if rows else "")
    else:
        rows, newest = read_ledger(req.window_days, plugin_root)

    scope_doc: dict[str, Any] = {
        "kind": "all",
        "member": None,
        "tickets": [],
        "assigned": [],
        "claimed": [],
    }
    pr_reads: int | None = None
    scope: frozenset[str] | None = None
    if member:
        assert identity.entry is not None
        assert identity.sidecar is not None
        ms = member_scope(identity.entry, identity.sidecar, rows)
        prs, by_ticket, pr_reads = read_member_prs(
            ms, identity.entry.github_login, run, CHILD_TIMEOUT_S
        )
        watcher = Watcher(now, prs, by_ticket)
        scope = ms.tickets
        for issue in identity.sidecar.issues:
            if issue.id in scope and issue.id not in linear and issue.status:
                linear[issue.id] = LinearIssue(issue.status, "", None)
        scope_doc = {
            "kind": "member",
            "member": identity.entry.id,
            "tickets": sorted(ms.tickets),
            "assigned": sorted(ms.assigned),
            "claimed": sorted(ms.claimed),
        }
    assert watcher is not None
    src = Sources(
        now,
        watcher,
        index_ledger(rows, newest),
        linear,
        reg,
        scope,
        "gh" if member else "watcher",
    )

    plans: list[dict[str, Any]] = []
    for path in files:
        if path not in blobs:
            continue
        if req.plan and not any(x in path for x in req.plan):
            continue
        doc = parse_plan(path, blobs[path], reg.get(path))
        p = build_plan(doc, src, req.window_days)
        if p:
            plans.append(p)
    plans = rank_plans(plans)
    out_root = (
        Path(req.out_dir) if req.out_dir else home / ".claude_scratch" / "plans-today"
    )
    add_summaries(
        plans,
        req.summaries,
        req.summary_limit,
        home,
        req.summary_timeout,
        req.workers,
        out_root / "summary-cache.json",
        req.summaries_budget,
        plugin_root,
    )
    out_dir = Path(req.out_dir) if req.out_dir else out_root / now.strftime("%Y-%m-%d")
    dedupe_root = out_root if not req.out_dir else out_dir
    msgs, msgs_ok = act(
        plans,
        dedupe_root,
        now,
        req.orchestrator,
        req.lane,
        home,
        req.no_msg,
        plugin_root,
    )

    document: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "generated_at": fmt_ts(now),
        "mode": identity.mode,
        "scope": scope_doc,
        "sources": {
            "kb_internal_origin_main": sha,
            "watcher_last_tick": "not read (member mode)"
            if member
            else fmt_ts(watcher.last_tick),
            "ledger_newest_row": newest,
            "linear_snapshot": req.linear_snapshot or "none (--no-linear)",
            "goal_state_as_of": goal_as_of,
            "pr_source": "gh-pr-view" if member else "pr-watcher",
            "pr_reads": pr_reads,
            "pr_read_cap": PR_READ_CAP if member else None,
            "team_identity": _identity_line(identity),
            "linear_sidecar": str(identity.sidecar_path)
            if identity.sidecar_path
            else "none",
        },
        "plans": plans,
        "msgs": msgs,
    }
    findings = validate(document)
    if findings:
        raise RefusedError(
            "the output does not match its schema: " + "; ".join(findings[:5])
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plans-today.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "detail.md").write_text(render_detail(document), encoding="utf-8")

    if req.json_only:
        text = json.dumps(document, indent=2)
    else:
        counts = {v: sum(1 for p in plans if p["verdict"] == v) for v in VERDICTS}
        unowned = sum(
            1 for p in plans if p["verdict"] == "STALLED" and not is_owned(p["owner"])
        )
        lines = [
            f"plans-today {fmt_ts(now)}: {len(plans)} plans; STALLED {counts['STALLED']} ({unowned} with no owner), "
            f"IN-PROGRESS {counts['IN-PROGRESS']}, NOT-STARTED {counts['NOT-STARTED']}, DONE {counts['DONE']}; "
            f"GOAL.md state_as_of {goal_as_of}",
            f"scope: {scope_line(scope_doc)}",
            f"source: {pr_source_line(document['sources'])}",
        ]
        shown = plans if req.all else plans[: req.top]
        lines += [line_for(p) for p in shown]
        if len(plans) > len(shown):
            lines.append(f"+{len(plans) - len(shown)} more (see the detail file)")
        for m in msgs:
            lines.append(
                f"MSG {m['action']}: {m['plan']}"
                + (f" {m['detail']}" if m["action"] == "failed" else "")
            )
        lines.append(f"detail: {out_dir / 'detail.md'}")
        lines.append(f"json: {out_dir / 'plans-today.json'}")
        text = "\n".join(lines)
    return ModelPlansTodayResult(
        exit_code=0 if msgs_ok else 4,
        mode=identity.mode,
        refused=None,
        stdout=text,
        document=document,
    )


class HandlerPlansToday:
    """Run plans-today for the resolved caller."""

    def handle(self, request: ModelPlansTodayRequest) -> ModelPlansTodayResult:
        return run_plans_today(request)
