# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab is never idle (OMN-17427; RULING 2026-10-14T21:46:25Z, operator 2026-10-18).

From about 19:45Z to 21:25Z on 2026-10-18 every throughput tick printed MISSING lab-headroom with 13 to 29 free
slots; the merge sweep planned 0 lanes (it plans only head_cap conflict fixes and superseded closes) and the
orchestrator left the slots idle, though standing rulings said to fill them. Idleness can no longer depend on that
judgement:

``HandlerIdleSlotFill`` (operation fill) is what the throughput tick sends by itself. The free slots are the Claude
slots of every host the tick's node reported as ``MISSING lab-headroom:<host>`` and whose probe read, less the lanes
the merge sweep places this tick, with Codex sharing that budget and dedicated Codex capacity counted separately.
The contract's model policy ranks all admitted engines. Slots are filled from
the standing pillar backlog, in this order: a repair lane for each broken lab piece the tick names
(``running-vs-merged:``, ``host-tick:``, ``throughput-node``, ``dispatch-replace:``), a fix-forward lane for each red
landing branch (``repo-status:<repo>`` naming base_branch_red, or merge_queue_ejecting with no controller owner: RULING
2026-10-14T03:57:46Z, OMN-20730, fix forward on dev, never revert, and the alarm starts the fix), the tick's own unowned
process-fix PRs (``MISSING process-fix:<repo#n>``:
an owned one is a NOTE by then), its unowned shared reds (``MISSING repo-status:<repo>`` naming shared_red_unowned),
then the backlog file's items (M4 board delegation reds, then queued experiment designs, then any other kind;
the file's order within a kind). Once the file backlog is spent the fill generates items from live state: the
operator's open delegation-pillar and M4-carrier tickets with no live CLAIM and no open PR, from the session's Linear
snapshot plans-today keeps; tickets whose triage TERMINAL is blocked on a missing readback; the checked-in delegation
experiment designs, least recently sent first. It skips retired and out-of-scope repositories, other owners,
deploy-repository and dashboard work (RULING 2026-10-01T10:53:36Z); OMNI_LAB_FILL_GENERATE=off turns it off. A key sent
inside the window is skipped, so the tick sends one lane per item, not one per tick. A repository the merge sweep's one
scope setting rules out (``sweep_scope.json``: parked, and retired by RULING 2026-10-18T05:25:04Z) is no candidate of any
kind; with that setting unread, no lane that names a repository is sent (an unread scope is never an empty one). A
``host-tick:`` repair is sent only into a free Claude slot of the launching host (the ``local: true`` row), and its
brief carries ``Required host:``:
the tick judged that host's own units and files; 2026-10-20 a repair placed on another host could not reach them (OMN-17427).

OMN-20864 (2026-10-25): which tickets the generator admits is omnimarket's rule (``plan_ticket_backlog``, omnimarket#4384,
RULING 2026-10-20T10:45:21Z), called through the omnimarket checkout; every drop it makes is counted on the generator line.
A lane carries its item's own ticket (a generated ticket or readback, a process-fix PR's title ticket, an item's ``ticket``)
or its class's standing child declared in the backlog file (``standing_tickets``: {kind: ticket}), never the umbrella
OMN-17427, which the runner refuses; an item with neither is skipped and says so. A stored item whose named PR is merged,
closed or gone from the PR watcher state, or whose fact is older than 48 hours, a stored item a fill lane answered
(ended done or handed-off) at or after its fact until its fact is refreshed (OMN-20910), and an experiment design with a
completed receipt in the last 24 hours are dropped, each with its reason on ``NOTE lab-fill-dropped`` and in
lab-fill.dropped.json.

``HandlerLabFill`` is the tick's effect around that decision: it reads the backlog file (pillar-backlog.json in the
lab fill's one state directory, ``lab_fill_state_dir()``) and the keys already sent (lab-fill.sent.json there), sends
each chosen lane through the remote-lane runner
detached, and returns the tick's findings: one ``NOTE lab-fill | sent=<n> ... idle_after=<k>: ...`` line naming what
it sent, ``MISSING lab-fill-backlog`` when slots stay free with the backlog spent, and ``UNKNOWN
lab-fill-dispatch:<lane>`` for a runner refusal (sent again next tick). The planned lanes come from the tick's own
``sweep: launch ... planned=<n> ... max_lanes=<m>`` line.

``HandlerIdleTurnGuard`` (operation turn-guard) is the orchestrator's Stop guard: a turn whose tool output showed
free headroom (``MISSING lab-headroom:``), an ``UNOWNED`` red or a broken lab piece (running-vs-merged drift, a
host tick not firing, a tick node that cannot run, a failed relaunch or fill dispatch), and whose tool calls dispatched
nothing (no Agent,
Workflow, lane skill or remote-lane runner call), is refused with the lines it read. A tick whose own ``lab-fill``
line sent lanes into every free slot (``idle_after=0``) answers the headroom by itself.

RULING 2026-10-28T13:32:09Z: Claude fills use Sonnet. Explicit Opus requirements
(a fill carries no escalation_reason) are skipped with the ruling in the existing backlog report, without consuming a slot.

OMN-21396: before selecting any candidate kind, the effect reads scope=fixer
HOLDs from the declared ledger and its archives. Held work is reported with
RULING 2026-10-26T17:22:56Z on lab-fill-dropped; an unread ledger blocks
repository work. Generation being off or the stored backlog being full does
not bypass the read. RELEASE and expiry share the landing controller's rule.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_lab_fill_tickets import plan_ticket_backlog
from omnimarket.handlers.rules_lane_requirements import claude_required, declarations
from omnimarket.handlers.work_ledger_fixer_holds import fixer_hold_reasons
from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.models.lab_fill import ModelLabFillTicketFacts
from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.models.model_backlog_item import (
    REPO_KINDS_CONTEXT_KEY,
    backlog_item_refusal,
)
from omnimarket.nodes.node_idle_slot_fill_effect import overlay
from omnimarket.nodes.node_idle_slot_fill_effect.models import (
    ModelBacklogSources,
    ModelIdleSlotFillRequest,
    ModelIdleSlotFillResult,
    ModelIdleTurnGuardRequest,
    ModelIdleTurnGuardResult,
    ModelLabFillRequest,
    ModelLabFillResult,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    load_policy,
    resolve_lane_model,
)

from .handler_ruling_recovery import HandlerRulingRecovery

RULING = "RULING 2026-10-14T21:46:25Z lane=orchestrator-bb1a (OMN-17427)"
UMBRELLA_TICKETS = frozenset({"OMN-17427"})
TICKET_ID = re.compile(r"OMN-[0-9]+")
TICKET_RULE = "omnimarket.handlers.rules_lab_fill_tickets.plan_ticket_backlog (omnimarket#4384, RULING 2026-10-20T10:45:21Z)"
FACT_MAX_AGE = timedelta(hours=48)
EXPERIMENT_RERUN_AFTER = timedelta(hours=24)
EFFORT = "high"
GENERATED_KINDS = ("ticket", "readback", "experiment-design")
DASHBOARD_RULING = "RULING 2026-10-01T10:53:36Z lane=dashboard-stayout (OMN-19969)"
DASHBOARD_CRITERIA = frozenset({"C31", "C32"})
DASHBOARD_TICKETS = frozenset({"OMN-19969"})
LIVE_CLAIM = timedelta(
    minutes=120
)  # a remote lane runs up to 90 minutes and the runner closes its CLAIM

_HEADROOM = re.compile(r"^MISSING lab-headroom:(?P<host>\S+) ")
_PROCESS_FIX = re.compile(r"^MISSING process-fix:(?P<repo>[\w.-]+)#(?P<n>\d+) \| ")
_RED = re.compile(r"^MISSING repo-status:(?P<repo>[\w.-]+) \| (?=.*shared_red_unowned)")
# a red landing branch (OMN-20730): the tick's verdict for it, NOTE or MISSING; one the controller owns is not backlog
_DEV_RED = re.compile(
    r"^(?:MISSING|NOTE) repo-status:(?P<repo>[\w.-]+) \| [^|]*; (?:base_branch_red|merge_queue_ejecting): "
    r"(?!.*\| FIX: the controller owns it)"
)
DEV_RED_RULING = "RULING 2026-10-14T03:57:46Z lane=orchestrator-31d9 (OMN-20730)"
RETIRED_RULING = "RULING 2026-10-26T17:22:56Z lane=orchestrator-31d9"

BACKLOG_ORDER = ("ruling-relaunch", "m4-board-red", "experiment-design")
_REPAIR = re.compile(
    r"^(?:MISSING|UNKNOWN) (?P<loop>running-vs-merged:\S+|host-tick:\S+|throughput-node|dispatch-replace:\S+) \| "
)
KIND_SLUG = {
    "ruling-relaunch": "ruled-retry",
    "lab-repair": "repair",
    "dev-red": "dev-red",
    "process-fix": "pf-drive",
    "cause-red": "cause-red",
    "m4-board-red": "m4",
    "experiment-design": "exp",
    "ticket": "ticket",
    "readback": "readback",
}


def kind_work(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The work sentence of each kind; the out-of-scope repository names come from the idle_slot_fill overlay."""
    out = "/".join(sorted(overlay.always_out(environ)))
    return {
        "ticket": (
            f"build what is left on this ticket in one PR in its owning repository (read the ticket's own criteria "
            f"first; if it is superseded, already built, or {out}/cloud or dashboard work, change nothing "
            "and say so), then /omni:pr-handoff it."
        ),
        "readback": (
            "take the readback the triage verdict says is missing: run its probe live on the lab, record the "
            "result as a STATUS row with readback= and verdict= cells, then build or file only what the readback "
            f"shows ({out}/cloud or dashboard work: change nothing and say so); never mark the ticket Done "
            "from a re-read."
        ),
        "lab-repair": (
            "repair this broken lab piece the tick names: read the line's FIX and the host's own files, make the "
            "repair through its sanctioned path (an installer, a reprobe, a PR for a code cause), prove the tick "
            "no longer prints the line, and report it. Never prod, never a credential change."
        ),
        "dev-red": (
            "fix forward the red landing branch the line names ("
            + DEV_RED_RULING
            + ': "I\'m fine with fixing forward for dev"): '
            "write a CLAIM naming cause=<repo>:<check> for a check the line names, find the merge that turned the "
            "branch red, fix it in one PR against that branch (or name the open PR already fixing it), and "
            "/omni:pr-handoff it. Fix forward: never revert the merge, and never open a revert PR."
        ),
        "process-fix": (
            "carry this process-fix PR to merged: bring its head current, fix the red it names on its own "
            "branch, then /omni:pr-handoff --needs land. The landing controller stays the only lander."
        ),
        "cause-red": (
            "own this shared red: write a CLAIM naming cause=<repo>:<check>, find the one root cause across the "
            "PRs it reds, fix it in one PR at its root (or name the PR already fixing it), and /omni:pr-handoff it."
        ),
        "m4-board-red": (
            "clear this M4 board red: read the board criterion's own probe, find why it reads red, and fix "
            f"the cause in one PR in its owning repository (no {out} work in M4, RULING "
            "2026-10-17T20:42:15Z), then /omni:pr-handoff it."
        ),
        "experiment-design": (
            "run this queued experiment design on the lab as written, record the measurement it names "
            "in the report file, and open a PR only when the design says to."
        ),
    }


def _stamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")[:60]


# The remote-lane runner's --lane rule (onex_remote_lane.LANE_RE): a lane it refuses as "must be a plain token" is never sent.
LANE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
LANE_MAX = 81


def lane_name(kind: str, name: str, now: str) -> str:
    """The fill's lane name, ``fill-<kind>-<name>-<YYYYMMDDTHHMM>Z``, always a token the runner takes (OMN-17427, 2026-10-21T11:13Z:
    'fill-dropped-c-beta-plans-2026-10-16-private-repo-batch-train-plan.md-20261021T1113Z' is 84 characters and the runner refused
    it). A name too long for the token is cut and keeps a short digest of the whole, so two long keys stay two lanes."""
    head = f"fill-{_slug(KIND_SLUG.get(kind, kind))}-"
    tail = f"-{now[:13].replace('-', '')}{now[14:16]}Z".replace(":", "")
    body = _slug(name).replace(":", "") or "item"
    if len(head) + len(body) + len(tail) > LANE_MAX:
        digest = hashlib.sha256(body.encode()).hexdigest()[:6]
        body = (
            body[: LANE_MAX - len(head) - len(tail) - len(digest) - 1].rstrip("-.")
            + "-"
            + digest
        )
    return head + body + tail


def _priority(kind: str, repo: str) -> str:
    if (
        kind in ("process-fix", "lab-repair", "ruling-relaunch")
        or repo in overlay.process_fix_repos()
    ):
        return "process-fix"
    mvp = (
        kind in ("dev-red", "cause-red", "m4-board-red", "ticket", "readback")
        and repo in overlay.mvp_repos()
    )
    return "mvp-landing" if mvp else "other"


def read_fixer_holds(now: str, env: Mapping[str, str]) -> tuple[dict[str, str], str]:
    """Read the declared ledger and its rolls, including HOLDs older than the claim window.

    Unlike backlog generation, this read runs even when generation is off or the
    stored backlog fills every slot. An unread ledger never means no HOLDs.
    """
    path = env.get("ONEX_LEDGER_PATH")
    root = env.get("ONEX_REGISTRY_ROOT") or env.get("OMNI_HOME")
    ledger = (
        Path(path).expanduser()
        if path
        else (
            Path(root).expanduser() / "docs/tracking/ROLLING_WORK_LEDGER.md"
            if root
            else None
        )
    )
    if ledger is None:
        return {}, "configure ONEX_LEDGER_PATH or a registry root before filling"
    try:
        paths = [
            ledger,
            *sorted((ledger.parent / "archive").glob("ROLLING_WORK_LEDGER*.md")),
        ]
        rows = []
        for source in paths:
            for text in split_ledger_rows(source.read_text(encoding="utf-8")):
                stamp, rtype, _ = text.split(" | ", 2)
                if rtype in {"HOLD", "RELEASE"}:
                    rows.append(ModelLandingLedgerRow(ts=stamp, rtype=rtype, text=text))
        clock = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return fixer_hold_reasons(sorted(rows, key=lambda row: row.ts), clock), ""
    except (OSError, ValueError) as exc:
        return {}, f"{ledger}: {type(exc).__name__}: {exc}"


def fixer_hold_reason(item: Mapping[str, Any], holds: Mapping[str, str]) -> str | None:
    """Apply the HOLD to all candidate kinds, including tickets lacking an explicit repo."""
    repo = str(item.get("repo") or "").split("/", 1)[-1]
    text = " ".join(str(item.get(key) or "") for key in ("title", "fact", "brief"))
    for held, row in holds.items():
        if (
            (held == "all" and (repo or item.get("kind") != "lab-repair"))
            or held == repo
            or re.search(rf"(?<![\w-]){re.escape(held)}(?![\w-])", text, re.IGNORECASE)
        ):
            return (
                f"fixer HOLD repo={held} scope=fixer; {RETIRED_RULING}; source: {row}"
            )
    return None


def candidates(
    lines: list[str],
    backlog: list[dict[str, Any]],
    generated: Iterable[dict[str, Any]] = (),
    *,
    out_of_scope: Collection[str] = (),
) -> list[dict[str, Any]]:
    """The standing pillar backlog in fill order: the tick's unowned process-fix PRs, its unowned shared reds, then
    the backlog file's items. An item names kind, key, repo and title; the tick's own line is its fact. An item of a
    repository in ``out_of_scope`` (the merge sweep's scope setting, retired repositories included) is none."""
    out: list[dict[str, Any]] = []
    for line in lines:  # a broken lab piece first: it refuses or misplaces every other lane (MSG 2026-10-18T21:39:09Z)
        m = _REPAIR.match(line)
        if m:
            out.append(
                {
                    "kind": "lab-repair",
                    "key": f"repair:{m['loop']}",
                    "repo": "",
                    "name": _slug(m["loop"]),
                    "fact": line,
                }
            )
    for line in (
        lines
    ):  # a red landing branch next: every PR of its repository lands on it (OMN-20730)
        m = _DEV_RED.match(line)
        if m:
            out.append(
                {
                    "kind": "dev-red",
                    "key": f"dev-red:{m['repo']}",
                    "repo": m["repo"],
                    "name": m["repo"],
                    "fact": line,
                }
            )
    for line in lines:
        m = _PROCESS_FIX.match(line)
        if m:
            out.append(
                {
                    "kind": "process-fix",
                    "key": f"{m['repo']}#{m['n']}",
                    "repo": m["repo"],
                    "name": f"{m['repo']}-{m['n']}",
                    "fact": line,
                }
            )
    for line in lines:
        m = _RED.match(line)
        if m:
            out.append(
                {
                    "kind": "cause-red",
                    "key": f"repo-status:{m['repo']}",
                    "repo": m["repo"],
                    "name": m["repo"],
                    "fact": line,
                }
            )
    rank = {kind: i for i, kind in enumerate(BACKLOG_ORDER)}
    ordered = sorted(
        (i for i in backlog if isinstance(i, dict)),
        key=lambda i: rank.get(str(i.get("kind")), len(rank)),
    )
    generated = list(generated)
    seen = {i["key"] for i in out}
    # what is re-verified before it is sent (OMN-20864): a stored item every rule, a generated design its receipts only
    for item, verify in [
        # These are freshly derived from ledger state, including old staged
        # work. A stored backlog row of this kind still passes every stored-row
        # check; only the live detector's output takes the generated path.
        *(
            (i, "")
            for i in generated
            if isinstance(i, dict) and i.get("kind") == "ruling-relaunch"
        ),
        *((i, "stored") for i in ordered),
        *(
            (i, "receipt" if i.get("kind") == "experiment-design" else "")
            for i in generated
            if isinstance(i, dict) and i.get("kind") != "ruling-relaunch"
        ),
    ]:
        if (
            isinstance(item, dict)
            and item.get("key")
            and item.get("kind")
            and str(item["key"]) not in seen
        ):
            seen.add(str(item["key"]))
            out.append(
                {
                    **item,
                    "_verify": verify,
                    "kind": str(item["kind"]),
                    "key": str(item["key"]),
                    "repo": str(item.get("repo") or ""),
                    "name": _slug(str(item["key"]).split(":", 1)[-1]),
                    "fact": str(item.get("fact") or item.get("title") or item["key"]),
                    "brief": str(item.get("brief") or ""),
                    "risk_class": item.get("risk_class"),
                    "required_engine": item.get("required_engine"),
                }
            )
    return [
        item for item in out if not item["repo"] or item["repo"] not in out_of_scope
    ]


_PR_REF = re.compile(r"(?<![\w.-])(?:[\w.-]+/)?(?P<repo>[A-Za-z0-9_.-]+)#(?P<n>\d+)\b")
_FACT_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?\b")


def _fact_time(stamp: str) -> datetime | None:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
        stamp += "T23:59:59Z"
    return _stamp(stamp)


def _fact_newest(item: Mapping[str, Any]) -> tuple[datetime, str] | None:
    explicit = str(item.get("fact_at") or "")
    stamps = (
        [explicit]
        if explicit
        else _FACT_TIME.findall(f"{item.get('fact') or ''} {item.get('title') or ''}")
    )
    return max(
        ((at, stamp) for stamp in stamps if (at := _fact_time(stamp)) is not None),
        default=None,
    )


def stale_reason(
    item: Mapping[str, Any],
    *,
    now: datetime,
    watcher_state: Mapping[str, Any] | None,
    receipts: Mapping[str, str],
) -> str | None:
    """Why a stored item is no longer work, or None (OMN-20864): a PR it names merged, closed or no longer open in the PR watcher
    state; its fact (``fact_at``, else the newest date stamp in its fact and title) older than 48 hours; a stored item a fill
    lane answered (done or handed-off) at or after its fact; an experiment design with a completed receipt in the last
    24 hours. A generated item (``_verify`` receipt) is checked for the receipt only. No I/O."""
    if item.get("_verify") == "stored" and watcher_state is not None:
        prs, repos = watcher_state.get("prs") or {}, watcher_state.get("repos") or {}
        text = " ".join(str(item.get(k) or "") for k in ("pr", "key", "title", "fact"))
        for match in _PR_REF.finditer(text):
            ref = f"{match['repo']}#{match['n']}"
            record = prs.get(ref)
            facts = (record.get("facts") or {}) if isinstance(record, Mapping) else {}
            state = facts.get("state")
            if state in ("MERGED", "CLOSED"):
                return f"pr-{state.lower()} {ref}"
            if ref not in prs and match["repo"] in repos:
                return f"pr-not-open {ref} (absent from the PR watcher state, which holds every open PR of the repository)"
    now = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
    newest = _fact_newest(item)
    if (
        item.get("_verify") == "stored"
        and newest is not None
        and now - newest[0] > FACT_MAX_AGE
    ):
        return f"fact-stale {newest[1]} (older than 48h)"
    if (
        item.get("_verify") == "stored"
        and item.get("kind") != "experiment-design"
        and item.get("key") in receipts
    ):
        return f"answered {receipts[item['key']]}"
    if item.get("kind") == "experiment-design" and item.get("key") in receipts:
        return f"experiment-done {receipts[item['key']]}"
    return None


def completed_experiments(
    items: Collection[Mapping[str, Any]],
    sent: Mapping[str, Any],
    *,
    now: str,
    lanes: Path,
) -> dict[str, str]:
    """Read successful receipts for a design's fill lanes and its recorded lane (OMN-17427)."""
    at_now = _stamp(now)
    completed: dict[str, str] = {}
    if at_now is None:
        return completed
    try:
        directories = [p for p in lanes.iterdir() if p.is_dir()]
    except OSError:
        directories = []
    for item in items:
        key = str(item.get("key") or "")
        if item.get("kind") != "experiment-design" or not key.startswith("exp:"):
            continue
        prefix = f"fill-exp-{_slug(key[4:])}-"
        recorded = str((sent.get(key) or {}).get("lane") or "")
        paths = {p for p in directories if p.name.startswith(prefix)}
        if LANE_TOKEN.fullmatch(recorded):
            paths.add(lanes / recorded)
        newest: tuple[datetime, str] | None = None
        for path in sorted(paths):
            try:
                receipts = list(path.glob("*.json"))
            except OSError:
                continue
            for receipt in receipts:
                if receipt.name.endswith(".job.json"):
                    continue
                try:
                    doc = _json(receipt)
                except (OSError, ValueError):
                    continue
                if (
                    not isinstance(doc, dict)
                    or doc.get("final") is not True
                    or doc.get("status") != "done"
                    or doc.get("outcome", "done") not in ("done", "handed-off")
                ):
                    continue
                ended = _stamp(str(doc.get("ended_at") or ""))
                if (
                    ended is not None
                    and timedelta(0) <= at_now - ended <= EXPERIMENT_RERUN_AFTER
                ):
                    candidate = (ended, f"{path.name} at {doc['ended_at']}")
                    newest = max(newest, candidate) if newest else candidate
        if newest:
            completed[key] = newest[1]
    return completed


def answered_items(
    items: Collection[Mapping[str, Any]],
    sent: Mapping[str, Any],
    *,
    lanes: Path,
) -> dict[str, str]:
    """Stored items other than experiment designs whose fill lane ended done or handed-off at or after their fact:
    key -> "<lane dir name> at <ended_at>"; a refreshed fact sends the item again (OMN-20910)."""
    answered: dict[str, str] = {}
    try:
        directories = [p for p in lanes.iterdir() if p.is_dir()]
    except OSError:
        directories = []
    for item in items:
        key, kind = item.get("key"), item.get("kind")
        if (
            not isinstance(key, str)
            or not isinstance(kind, str)
            or kind == "experiment-design"
        ):
            continue
        fact = _fact_newest(item)
        if fact is None:
            continue
        prefix = (
            lane_name(kind, _slug(key.split(":", 1)[-1]), "2000-01-01T00:00:00Z")[:-15]
            + "-"
        )
        recorded = str((sent.get(key) or {}).get("lane") or "")
        paths = {
            p
            for p in directories
            if p.name.startswith(prefix)
            and re.fullmatch(r"\d{8}T\d{4}Z", p.name[len(prefix) :])
        }
        if LANE_TOKEN.fullmatch(recorded):
            paths.add(lanes / recorded)
        newest: tuple[datetime, str] | None = None
        for path in sorted(paths):
            try:
                receipts = list(path.glob("*.json"))
            except OSError:
                continue
            for receipt in receipts:
                if receipt.name.endswith(".job.json"):
                    continue
                try:
                    doc = _json(receipt)
                except (OSError, ValueError):
                    continue
                if (
                    not isinstance(doc, dict)
                    or doc.get("final") is not True
                    or doc.get("status") != "done"
                    or doc.get("outcome", "done") not in ("done", "handed-off")
                ):
                    continue
                ended = _stamp(str(doc.get("ended_at") or ""))
                if ended is not None and ended >= fact[0]:
                    candidate = (ended, f"{path.name} at {doc['ended_at']}")
                    newest = max(newest, candidate) if newest else candidate
        if newest:
            answered[key] = newest[1]
    return answered


def brief(
    item: dict[str, Any],
    lane: str,
    model: str,
    parent: str | None,
    ticket: str,
    *,
    host: str | None = None,
) -> str:
    work = item.get("brief") or kind_work().get(
        item["kind"], "do the work the fact below names and report it."
    )
    engine = (
        f"Required engine: {item['required_engine']}\n"
        if item.get("required_engine")
        else ""
    )
    # the runner's brief reader pins a column-zero Required host line, and keeps it when a lane-queue pin ages out
    required = (
        (
            f"Required host: {host}\nThe tick that printed the line below runs on {host}; this repair reads and "
            "changes that host's own files, so it runs there.\n"
        )
        if host
        else ""
    )
    return (
        f"# Lane {lane}: {item['kind']} {item['key']} ({ticket})\n"
        f"ROUTE: model={model} effort={EFFORT}\n"
        f"{engine}"
        f"Parent: {parent or 'orchestrator'}. Ticket: {ticket}. Repo: {item['repo'] or 'none'}.\n"
        f"{required}"
        f'Authority: {RULING}: "fill those 27 shots and get things fixed"; the throughput tick dispatched this lane '
        "by itself into a free lab slot (the lab is never idle).\n"
        f"Fact at dispatch (the tick's own line, verbatim):\n{item['fact']}\n"
        f"Kind {item['kind']}: {work}\n"
        "Read PRs only with pr_state_local.py; never merge, arm, rerun or poll CI. If another lane already owns this "
        "(a live CLAIM or controller lease), end LANE_RESULT outcome=skipped-owned naming it.\n"
        "Report once at terminal: what you changed, the PR, and what is left.\n"
    )


def admitted_slots(host: Mapping[str, Any], engine: str, now: str) -> int:
    """Honor the pool admission and its engine marks without reading account secrets."""
    admission = host.get("admission") or {}
    if admission.get("probe_error") or admission.get(f"{engine}_refusal"):
        return 0
    marks = (
        ("limited_mark", "auth_mark") if engine == "claude" else ("codex_limited_mark",)
    )
    for key in marks:
        mark = host.get(key) or {}
        if mark.get("state", "absent") == "absent":
            continue
        until = _stamp(str(mark.get("until") or ""))
        observed = _stamp(now)
        if (
            mark.get("state") != "present"
            or until is None
            or observed is None
            or until > observed
        ):
            return 0
    slots = admission.get(f"{engine}_slots")
    return slots if isinstance(slots, int) and slots > 0 else 0


class HandlerIdleSlotFill:
    def handle(self, request: ModelIdleSlotFillRequest) -> ModelIdleSlotFillResult:
        free_hosts = {
            m["host"] for line in request.lines if (m := _HEADROOM.match(line))
        }
        claude = codex_only = glm_only = codex_shared = 0
        local_host, local_left = (
            None,
            0,
        )  # the launching host: the only place a host-tick repair can run
        for host in request.hosts:
            admission = host.get("admission") or {}
            if host.get("local"):
                local_host = host.get("name")
            g = admission.get("glm_slots", 0)
            glm_headroom = (
                isinstance(g, int)
                and g > 0
                and not admission.get("claude_slots")
                and not admission.get("codex_slots")
            )
            if (
                host.get("name") not in free_hosts and not glm_headroom
            ) or admission.get("probe_error"):
                continue
            c, x = (
                admitted_slots(host, engine, request.now)
                for engine in ("claude", "codex")
            )
            local_left = c if host.get("local") else local_left
            claude += c
            codex_only += x if c == 0 else 0
            codex_shared += min(c, x)
            glm_only += g if isinstance(g, int) and g > 0 and c == 0 and x == 0 else 0
        # OMN-17427 (2026-10-21): the pool read counts a lane only once it is placed, so a lane the fill sent that still waits for
        # placement reserves its slot here; without it each tick sent the free count again and ~150 runners queued on placement.
        placing = request.placing
        claude_left = max(
            0,
            claude
            - max(0, request.sweep_planned)
            - placing.get("claude", 0)
            - request.queue_reserved.get("claude", 0),
        )
        codex_left = max(
            0,
            codex_only
            - placing.get("codex", 0)
            - request.queue_reserved.get("codex", 0),
        )
        # A Codex-capable host can also admit Claude. Its slots are one budget:
        # a ruled retry consumes a shared slot once, without inventing headroom.
        shared_reserved = max(
            0,
            placing.get("codex", 0)
            + request.queue_reserved.get("codex", 0)
            - codex_only,
        )
        claude_left = max(0, claude_left - shared_reserved)
        shared_left = min(claude_left, max(0, codex_shared - shared_reserved))
        glm_left = max(0, glm_only - placing.get("glm", 0))
        now = _stamp(request.now)
        window = timedelta(hours=request.window_hours)
        sends: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        dropped: list[dict[str, str]] = []
        backlog_mismatched = 0
        item_context = {REPO_KINDS_CONTEXT_KEY: tuple(request.repo_required_kinds)}
        for item in candidates(
            request.lines,
            request.backlog,
            request.generated,
        ):
            # OMN-21475 (T-S3, SM-00): an item that is not a backlog item, a prose engine above all, is
            # counted and skipped; the fill never picks an engine for it.
            if why := backlog_item_refusal(item, context=item_context):
                backlog_mismatched += 1
                skipped.append(
                    {"key": item["key"], "why": f"backlog-item-invalid: {why}"}
                )
                continue
            if item["kind"] == "ruling-relaunch" and not item["repo"]:
                skipped.append(
                    {
                        "key": item["key"],
                        "why": "repo-unread: cannot judge the recovery item's repository scope",
                    }
                )
                continue
            if why := fixer_hold_reason(item, request.fixer_holds):
                dropped.append({"key": item["key"], "why": why})
                continue
            if request.fixer_holds_unread and (
                item["repo"] or item["kind"] != "lab-repair"
            ):
                dropped.append(
                    {
                        "key": item["key"],
                        "why": f"fixer HOLD ledger unread: {request.fixer_holds_unread}; {RETIRED_RULING}",
                    }
                )
                continue
            if item["repo"] in request.out_of_scope:
                dropped.append(
                    {
                        "key": item["key"],
                        "why": f"out-of-scope: {request.out_of_scope[item['repo']]}",
                    }
                )
                continue
            if (
                item.get("_verify")
                and now is not None
                and (
                    why := stale_reason(
                        item,
                        now=now,
                        watcher_state=request.watcher_state,
                        receipts=request.completed,
                    )
                )
            ):
                dropped.append({"key": item["key"], "why": why})
                continue
            if request.scope_unread and item["repo"]:
                skipped.append(
                    {
                        "key": item["key"],
                        "why": f"repo scope unread: {request.scope_unread}",
                    }
                )
                continue
            sent = request.dispatched.get(item["key"])
            at = _stamp(str((sent or {}).get("at") or ""))
            if sent and at is not None and now is not None and now - at < window:
                skipped.append(
                    {
                        "key": item["key"],
                        "why": f"sent as {sent.get('lane')} at {sent.get('at')}",
                    }
                )
                continue
            # the lane's ticket (OMN-20864): its own, a process-fix PR's title ticket, else its class's declared standing child;
            # never the umbrella, which the runner refuses (REMOTE-LANE-UMBRELLA-REFUSED)
            own = item.get("ticket")
            if item["kind"] == "process-fix" and not own:
                record = ((request.watcher_state or {}).get("prs") or {}).get(
                    item["key"]
                )
                title = (
                    ((record.get("facts") or {}).get("title") or "")
                    if isinstance(record, Mapping)
                    else ""
                )
                own = next(
                    (
                        t
                        for t in re.findall(r"\bOMN-[0-9]+\b", str(title))
                        if t not in UMBRELLA_TICKETS
                    ),
                    None,
                )
            standing = request.standing_tickets.get(item["kind"])
            ticket = next(
                (
                    t
                    for t in (own, standing)
                    if isinstance(t, str)
                    and TICKET_ID.fullmatch(t)
                    and t not in UMBRELLA_TICKETS
                ),
                None,
            )
            if ticket is None:
                declaration = f"standing_tickets[{item['kind']!r}]"
                why = (
                    f"{declaration} names the umbrella {standing}"
                    if standing in UMBRELLA_TICKETS
                    else f"{item['kind']} has no ticket of its own and no {declaration} in the backlog file"
                )
                skipped.append(
                    {
                        "key": item["key"],
                        "why": f"no child ticket: {why}; never the umbrella OMN-17427",
                    }
                )
                continue
            pin = None
            required = (
                str(item.get("required_engine") or "").strip().lower().replace("_", "-")
            )
            requirements = [
                (match[1].lower(), match[2])
                for line in declarations(item.get("brief", ""))
                if (
                    match := re.fullmatch(
                        r"Required (engine|model):\s*(.*?)\s*", line, re.IGNORECASE
                    )
                )
            ]
            required_model = next(
                (value for kind, value in requirements if kind == "model"), None
            )
            brief_engine = next(
                (value for kind, value in requirements if kind == "engine"), None
            )
            policy_facts = {
                "lane_kind": item["kind"],
                "risk_class": item.get("risk_class"),
                "required_engine": required or brief_engine,
                "required_model": required_model,
            }
            preflights = [resolve_lane_model(**policy_facts)]
            preflights.extend(
                resolve_lane_model(**{**policy_facts, f"required_{kind}": value})
                for kind, value in requirements
            )
            if refusal := next(
                (answer.reason for answer in preflights if not answer.allowed), None
            ):
                skipped.append({"key": item["key"], "why": refusal})
                continue
            pinned = required in (
                "claude",
                "sonnet",
                "claude-sonnet",
            ) or claude_required(item.get("brief", ""))
            if required == "codex":
                if pinned:
                    skipped.append(
                        {
                            "key": item["key"],
                            "why": "engine-conflict: Codex item carries a Claude-required brief",
                        }
                    )
                    continue
                shared_left = min(shared_left, claude_left)
                if codex_left <= 0 and shared_left <= 0:
                    skipped.append(
                        {
                            "key": item["key"],
                            "why": "codex-required: waiting for admitted Codex capacity",
                        }
                    )
                    continue
            elif item["key"].startswith("repair:host-tick:"):
                if local_left <= 0 or claude_left <= 0:
                    skipped.append(
                        {
                            "key": item["key"],
                            "why": (
                                f"a host-tick repair runs only on the launching host {local_host}, which has no free Claude slot"
                                if local_host
                                else (
                                    "a host-tick repair runs only on the launching host, and no local host row was read"
                                )
                            ),
                        }
                    )
                    continue
                pin = local_host
            elif pinned and claude_left <= 0:
                skipped.append(
                    {
                        "key": item["key"],
                        "why": "claude-required: waiting for Claude quota",
                    }
                )
                continue
            # Both engines can spend a shared host's one budget. Let the declared
            # policy rank them even when the item has no explicit engine pin.
            shared_left = min(shared_left, claude_left)
            available = {
                "claude": claude_left,
                "codex": codex_left + shared_left,
                "glm": glm_left,
            }
            decision = resolve_lane_model(
                **policy_facts,
                requested_engine="claude" if pin or pinned else "auto",
                available_engines=tuple(
                    engine for engine, count in available.items() if count > 0
                ),
            )
            if decision.model is None:
                skipped.append({"key": item["key"], "why": "no free slot"})
                continue
            model = decision.model
            constraints = [
                resolve_lane_model(
                    **{
                        **policy_facts,
                        "requested_engine": model,
                        "available_models": (model,),
                        f"required_{kind}": value,
                    }
                )
                for kind, value in requirements
            ]
            if refusal := next(
                (answer.reason for answer in constraints if not answer.allowed), None
            ):
                skipped.append({"key": item["key"], "why": refusal})
                continue
            if decision.engine == "claude":
                claude_left -= 1
                if pin:
                    local_left -= 1
            elif decision.engine == "codex":
                if codex_left > 0:
                    codex_left -= 1
                else:
                    shared_left -= 1
                    claude_left -= 1
            elif decision.engine == "glm":
                glm_left -= 1
            lane = lane_name(item["kind"], item["name"], request.now)
            sends.append(
                {
                    "kind": item["kind"],
                    "key": item["key"],
                    "lane": lane,
                    "model": model,
                    "effort": EFFORT,
                    "repo": item["repo"],
                    "priority_class": _priority(item["kind"], item["repo"]),
                    "risk_class": item.get("risk_class"),
                    "ticket": ticket,
                    "brief": brief(item, lane, model, request.parent, ticket, host=pin),
                    **({"host": pin} if pin else {}),
                }
            )
        return ModelIdleSlotFillResult(
            claude,
            codex_only,
            sends,
            skipped,
            claude_left + codex_left + glm_left,
            glm_only,
            dropped,
            backlog_mismatched,
        )


# ---- the orchestrator's Stop guard ------------------------------------------------------------------------------------

_UNOWNED = re.compile(r"\bUNOWNED\b")
# a broken lab piece the tick names (MSG 2026-10-18T21:39:09Z-orchestrator-bb1a): a host or runtime drifted from main, a
# launchd tick not firing, a tick node that cannot run, a lane the runner could not relaunch, start or fill
_BROKEN = re.compile(
    r"^(?:MISSING|UNKNOWN) (?:running-vs-merged:|host-tick:|throughput-node\b|dispatch-replace|"
    r"dispatch-queue-expired:|lab-fill-dispatch:|lab-headroom:\S+ \| .*\b(?:probe|unread))"
)
_READER = re.compile(
    r"python[\d.]*\s+(?:-\S+\s+)*[\"']?\S*\b(?:throughput_tick|queue_status)\.py[\"']?(?:\s|$)"
)
_FILL = re.compile(r"^NOTE lab-fill \| sent=\d+ .*\bidle_after=(?P<idle>\d+)\b")
_DISPATCH_SKILLS = frozenset(
    {
        "omni:remote-lane",
        "omni:fan-out",
        "omni:lane-brief",
        "onex:lane_dispatch",
        "onex:dispatch_engine",
        "onex:dispatch_worker",
        "onex:multi_agent",
        "onex:epic_team",
    }
)
_DISPATCH_BASH = re.compile(
    r"onex_remote_lane\.py\s+(?:\S+\s+)*?run\b|onex-remote-lane\s+run\b|\bonex delegate\b"
)
SHOWN = 6


def dispatched(tool_uses: list[dict[str, Any]]) -> bool:
    for use in tool_uses:
        name = use.get("name")
        raw = use.get("input")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if name in ("Agent", "Workflow", "Task"):
            return True
        if name == "Skill" and str(data.get("skill") or "") in _DISPATCH_SKILLS:
            return True
        if name == "Bash" and _DISPATCH_BASH.search(str(data.get("command") or "")):
            return True
    return False


class HandlerIdleTurnGuard:
    def handle(self, request: ModelIdleTurnGuardRequest) -> ModelIdleTurnGuardResult:
        headroom: list[str] = []
        unowned: list[str] = []
        broken: list[str] = []
        filled = False
        readers = {
            str(u.get("id"))
            for u in request.tool_uses
            if u.get("name") == "Bash"
            and _READER.search(str((u.get("input") or {}).get("command") or ""))
        }
        for result in request.tool_results:
            if str(result.get("id")) not in readers:
                continue  # only the tick's and the pulse's own output: a grep over their source is not a reading
            for line in str(result.get("text") or "").splitlines():
                line = line.strip()
                if _BROKEN.match(line):
                    broken.append(line)
                elif _HEADROOM.match(line):
                    headroom.append(line)
                elif _UNOWNED.search(line):
                    unowned.append(line)
                m = _FILL.match(line)
                if m:
                    filled = m["idle"] == "0"
        if filled:
            headroom = []
        shown = [*headroom, *unowned, *broken]
        if not shown or dispatched(request.tool_uses):
            return ModelIdleTurnGuardResult(False, "", shown)
        what = " and ".join(
            part
            for part in (
                f"{len(headroom)} free lab headroom line(s)" if headroom else "",
                f"{len(unowned)} UNOWNED red(s)" if unowned else "",
                f"{len(broken)} broken lab piece(s)" if broken else "",
            )
            if part
        )
        reason = "\n".join(
            [
                f"This turn read {what} and dispatched nothing for it (OMN-17427). The lab is never idle and a red with no "
                f'owner gets one: {RULING}, "fill those 27 shots and get things fixed", and operator 2026-10-18 "The '
                'lab is never to be idle". Dispatch a lane for each free slot from the pillar backlog (the throughput '
                "tick's lab-fill line names what it already sent), a cause lane for each UNOWNED red and a repair lane for each "
                "broken lab piece, through the "
                "remote-lane runner, an Agent or a Workflow, then end the turn. Flagging it is not a dispatch.",
                *(f"  - {line[:300]}" for line in shown[:SHOWN]),
                *(
                    [f"  ... and {len(shown) - SHOWN} more"]
                    if len(shown) > SHOWN
                    else []
                ),
            ]
        )
        return ModelIdleTurnGuardResult(True, reason, shown)


# ---- the throughput tick's effect: read the backlog, send, record (OMN-17427) -------------------------------------

BACKLOG_FILE = "pillar-backlog.json"
SENT_FILE = "lab-fill.sent.json"
DROPPED_FILE = "lab-fill.dropped.json"
LOCK_FILE = "lab-fill.lock"
LOCK_WAIT_S = 120.0
# The one place the lab fill keeps its backlog, its sent record and its briefs, whatever invoked the tick (OMN-17427, 2026-10-19):
# the launchd tick's OMNI_SESSION_START_STATE_DIR is its own directory and the session's tick used another, so the two read
# different backlogs and sent the same item twice. The tick's state directory never names it; OMNI_LAB_FILL_STATE_DIR does.
LAB_FILL_STATE_ENV = "OMNI_LAB_FILL_STATE_DIR"
LAB_FILL_STATE_SUBPATH = Path(".local") / "state" / "omni" / "throughput-tick"
REMOTE_LANE_DIR_ENV = "ONEX_REMOTE_LANE_DIR"  # the runner's receipt directory (onex_remote_lane.state_dir)
REMOTE_LANE_SUBPATH = Path(".local") / "state" / "omni" / "remote-lanes"
UNSTARTED_GRACE_S = (
    600.0  # a lane sent this recently with no receipt yet is still starting
)
DISPATCH_TIMEOUT_S = 90
_LAUNCH = re.compile(
    r"^sweep: launch .*?\bplanned=(?P<planned>\d+)\b.*?\bmax_lanes=(?P<max>\d+)\b"
)


def lab_fill_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """The single directory of the lab fill's state: OMNI_LAB_FILL_STATE_DIR when set, else ~/.local/state/omni/throughput-tick."""
    env = os.environ if env is None else env
    pinned = (env.get(LAB_FILL_STATE_ENV) or "").strip()
    return (
        Path(pinned).expanduser()
        if pinned
        else Path(env.get("HOME") or Path.home()) / LAB_FILL_STATE_SUBPATH
    )


@contextlib.contextmanager
def fill_lock(state: Path) -> Iterator[bool]:
    """Hold the fill's lock while it reads the sent record, sends and writes it back, so two ticks (the launchd one and a
    session's) never both send a key; False when another tick still holds it after LOCK_WAIT_S."""
    try:
        state.mkdir(parents=True, exist_ok=True)
        handle = (state / LOCK_FILE).open("a")
    except OSError:
        yield True
        return
    with handle:
        deadline = time.monotonic() + LOCK_WAIT_S
        held = False
        while not held:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        try:
            yield held
        finally:
            if held:
                fcntl.flock(handle, fcntl.LOCK_UN)


def remote_lane_dir(env: Mapping[str, str] | None = None) -> Path:
    """The remote-lane runner's receipt directory, as the runner resolves it."""
    env = os.environ if env is None else env
    pinned = (env.get(REMOTE_LANE_DIR_ENV) or "").strip()
    return (
        Path(pinned)
        if pinned
        else Path(env.get("HOME") or Path.home()) / REMOTE_LANE_SUBPATH
    )


def engine_of(model: str) -> str:
    policy = load_policy()
    name = model.strip().lower().replace("_", "-")
    return policy.engines.get(policy.aliases.get(name, name), "claude")


def placing_lanes(
    sent: Mapping[str, Any], now: str, lanes: Path, window_hours: float
) -> dict[str, int]:
    """The fill's own lanes, sent inside the window, that no host holds yet, by engine (claude, codex, glm): the newest receipt
    reads ``placing`` and is not final, or none is written yet and the lane was sent under UNSTARTED_GRACE_S ago. A lane that
    placed, ended or ended no-host is not counted (OMN-17427)."""
    at_now = _stamp(now)
    out: dict[str, int] = {}
    if at_now is None:
        return out
    for entry in sent.values():
        if not isinstance(entry, Mapping):
            continue
        lane, at = str(entry.get("lane") or ""), _stamp(str(entry.get("at") or ""))
        if (
            not LANE_TOKEN.match(lane)
            or at is None
            or not timedelta(0) <= at_now - at < timedelta(hours=window_hours)
        ):
            continue
        try:
            receipts = [
                p
                for p in (lanes / lane).glob("*.json")
                if not p.name.endswith(".job.json")
            ]
            newest = (
                max(receipts, key=lambda p: p.stat().st_mtime) if receipts else None
            )
            doc = _json(newest) if newest is not None else None
        except (OSError, ValueError):
            continue
        if doc is None:
            waiting = (at_now - at).total_seconds() < UNSTARTED_GRACE_S
            model = str(entry.get("model") or "")
        else:
            waiting = (
                isinstance(doc, dict)
                and doc.get("status") == "placing"
                and not doc.get("final")
            )
            model = str(
                (doc.get("model") if isinstance(doc, dict) else "")
                or entry.get("model")
                or ""
            )
        if waiting and model:
            out[engine_of(model)] = out.get(engine_of(model), 0) + 1
    return out


def free_slots(request: Mapping[str, Any]) -> dict[str, int]:
    """Free lane slots per engine over the hosts the live probe read; an unread probe has none."""
    free = {"claude": 0, "codex": 0}
    for host in (request.get("lab_headroom") or {}).get("hosts") or []:
        admission = host.get("admission") or {}
        if admission.get("probe_error"):
            continue
        for engine in free:
            slots = admission.get(f"{engine}_slots")
            free[engine] += slots if isinstance(slots, int) and slots > 0 else 0
    return free


def queue_reservations(
    entries: Collection[Mapping[str, Any]],
    started: Collection[str],
    request: Mapping[str, Any],
    pin_of: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, int]:
    """The slots the dispatch queue holds against this tick's lab_headroom snapshot, by engine, so the lab fill sends only
    what is left (OMN-21373: free lab slots go to the queue first). A queued lane holds one when it started this tick (its
    entry may already be gone, so ``entries`` is the queue as read before the starts) or when its engine has a free slot, on
    its pinned host while ``pin_of`` gives one. Capped at the engine's free slots; the fill's own lanes (``fill-``) are
    counted through ``placing`` instead."""
    capacity = free_slots(request)
    hosts = (request.get("lab_headroom") or {}).get("hosts") or []
    reserved = {"claude": 0, "codex": 0}
    for entry in entries:
        lane = str(entry["lane"])
        if lane.startswith("fill-"):
            continue
        # The queue snapshot reserves Claude/Codex pools; keep its existing client accounting.
        engine = (
            "codex" if engine_of(str(entry.get("model") or "")) == "codex" else "claude"
        )
        pin = pin_of(entry)
        available = (
            free_slots(
                {"lab_headroom": {"hosts": [h for h in hosts if h.get("name") == pin]}}
            )
            if pin
            else capacity
        )
        if lane in started or available[engine] > 0:
            reserved[engine] = min(capacity[engine], reserved[engine] + 1)
    return reserved


def read_sent(path: Path) -> dict[str, Any]:
    """The keys already sent (key -> {lane, at, kind}); an absent or unreadable record is empty."""
    try:
        doc = _json(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_sent(path: Path, sent: Mapping[str, Any]) -> None:
    """Replace the sent record through its resolved path: an atomic replace on a symlink's own path turns the link into a
    regular file and splits the record in two."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(sent, sort_keys=True) + "\n", encoding="utf-8")
    scratch.replace(target)


def sweep_planned(launch: str | None) -> int:
    """The lanes a `sweep: launch` line places (its planned count, at most its max_lanes); any other line places none:
    a held lease's lanes are already placed and counted by the pool read."""
    m = _LAUNCH.match(launch or "")
    return min(int(m["planned"]), int(m["max"])) if m else 0


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_backlog(path: Path) -> tuple[list[dict[str, Any]], dict[str, str], str | None]:
    """Read items and standing_tickets ({kind: child ticket}) from the backlog (OMN-17427).
    An absent file is an empty backlog; an unreadable one says why."""
    if not path.is_file():
        return [], {}, None
    try:
        doc = _json(path)
    except (OSError, ValueError) as exc:
        return [], {}, f"{path}: {exc}"
    items = doc.get("items") if isinstance(doc, dict) else doc
    standing = doc.get("standing_tickets", {}) if isinstance(doc, dict) else {}
    standing = (
        {k: v for k, v in standing.items() if isinstance(v, str)}
        if isinstance(standing, dict)
        else {}
    )
    return (
        ([i for i in items if isinstance(i, dict)] if isinstance(items, list) else []),
        standing,
        None,
    )


def read_experiments(load: Callable[[], Any]) -> list[dict[str, Any]]:
    """Keep designs with an id, title and runnable design; a loader that raises (no overlay) raises (OMN-17427)."""
    doc = load()
    designs = doc.get("designs") if isinstance(doc, dict) else doc
    return (
        [
            d
            for d in designs
            if isinstance(d, dict)
            and all(
                isinstance(d.get(k), str) and d[k].strip()
                for k in ("id", "title", "design")
            )
        ]
        if isinstance(designs, list)
        else []
    )


def _issue(issue: dict[str, Any]) -> dict[str, Any]:
    """Normalize the session snapshot's string and GraphQL cells (OMN-17427)."""
    status = issue.get("status") or issue.get("state") or ""
    assignee = issue.get("assigneeId") or issue.get("assignee")
    labels = issue.get("labels") or []
    labels = labels.get("nodes", []) if isinstance(labels, dict) else labels
    project = issue.get("project") or ""
    return {
        "id": issue.get("id") or issue.get("identifier") or "",
        "title": issue.get("title") or "",
        "status": status.get("name", "") if isinstance(status, dict) else status,
        "statusType": issue.get("statusType")
        or (status.get("type") if isinstance(status, dict) else ""),
        "assignee": (assignee.get("id") or assignee.get("name"))
        if isinstance(assignee, dict)
        else assignee,
        "labels": [
            label.get("name", "") if isinstance(label, dict) else label
            for label in labels
            if isinstance(label, (str, dict))
        ]
        if isinstance(labels, list)
        else [],
        "project": project.get("name", "") if isinstance(project, dict) else project,
        "repo": issue.get("repo") or "",
        "pillar": issue.get("pillar"),
        "description": str(issue.get("description") or issue.get("body") or ""),
    }


def _closed(issue: dict[str, Any]) -> bool:
    return issue["statusType"] in {"completed", "canceled", "cancelled"} or str(
        issue["status"]
    ).lower() in {"done", "canceled", "cancelled", "duplicate"}


def _scope_reason(
    issue: dict[str, Any], criteria: list[str], out_of_scope: set[str]
) -> str | None:
    """Repository exclusions precede the dashboard stay-out ruling (OMN-17427)."""
    text = " ".join(
        str(v) for v in (issue["title"], *issue["labels"], issue["project"])
    ).lower()
    excluded = out_of_scope | overlay.always_out()
    if issue["repo"] in excluded or any(
        re.search(rf"(?<![\w-]){re.escape(repo.lower())}(?![\w-])", text)
        for repo in excluded
    ):
        return "out-of-scope"
    if (
        issue["id"] in DASHBOARD_TICKETS
        or DASHBOARD_CRITERIA.intersection(criteria)
        or issue["repo"] == "omnidash"
        or "dashboard" in text
        or "omnidash" in text
    ):
        return "dashboard"
    return None


def generate_backlog(
    issues: list[dict[str, Any]],
    operator: str | None,
    *,
    m4: dict[str, list[str]],
    out_of_scope: set[str],
    claimed: set[str],
    open_pr: set[str],
    readbacks: dict[str, str],
    experiments: list[dict[str, Any]],
    sent: dict[str, dict[str, Any]],
    rule: Callable[[dict[str, Any]], dict[str, Any]] | None,
    today: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Use omnimarket's ticket plan, then missing readbacks and rotating designs (OMN-17427)."""
    normalized = [_issue(i) for i in issues if isinstance(i, dict)]
    tickets: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []
    designs: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    valid: list[dict[str, Any]] = []
    for i in normalized:
        if not isinstance(i["id"], str) or not TICKET_ID.fullmatch(i["id"]):
            skip("bad-id")
            continue
        valid.append(
            {
                **{k: v for k, v in i.items() if k != "statusType"},
                "status_type": i["statusType"],
            }
        )
    if rule is None:
        if issues:
            skipped["rule-unavailable"] = len(issues)
    else:
        plan = rule(
            {
                "today": today,
                "operator": operator,
                "issues": valid,
                "m4_carriers": m4,
                "claimed": sorted(claimed),
                "open_pr": sorted(open_pr),
                "out_of_scope": sorted(out_of_scope | overlay.always_out()),
            }
        )
        for drop in plan["drops"]:
            skip(drop["reason"])
        for i in plan["items"]:
            tid, carriers = i["id"], i["carriers"]
            carrier = "; M4 carrier of " + ", ".join(carriers) if carriers else ""
            tickets.append(
                {
                    "kind": "ticket",
                    "key": f"ticket:{tid}",
                    "ticket": tid,
                    "repo": i["repo"],
                    "title": i["title"],
                    "fact": f"{tid} {i['status']}: {i['title']} ({i['source']}{carrier}; "
                    f"no live CLAIM and no open PR; admitted by {TICKET_RULE})",
                }
            )
    by_id = {
        i["id"]: i
        for i in normalized
        if isinstance(i["id"], str) and TICKET_ID.fullmatch(i["id"])
    }
    criteria = {tid: [c for c, ids in m4.items() if tid in ids] for tid in by_id}
    for tid, row in sorted(readbacks.items()):
        known = by_id.get(tid)
        reason = "claimed" if tid in claimed else "open-pr" if tid in open_pr else None
        if reason is None and known is not None:
            reason = (
                "closed"
                if _closed(known)
                else _scope_reason(known, criteria[tid], out_of_scope)
            )
            if known["assignee"] and (
                not operator or known["assignee"] not in (operator, "me")
            ):
                reason = reason or "other-owner"
        if reason:
            skip(reason)
            continue
        tickets = [t for t in tickets if t["key"] != f"ticket:{tid}"]
        reads.append(
            {
                "kind": "readback",
                "key": f"readback:{tid}",
                "ticket": tid,
                "repo": known["repo"] if known else "",
                "title": known["title"] if known else tid,
                "fact": f"{tid}: the triage verdict is blocked on a missing readback: {row}",
            }
        )
    for d in sorted(
        experiments,
        key=lambda d: (
            f"exp:{d['id']}" in sent,
            str((sent.get(f"exp:{d['id']}") or {}).get("at") or ""),
        ),
    ):
        repo = d.get("repo") or ""
        if repo in out_of_scope | overlay.always_out() | {"omnidash"}:
            skip("out-of-scope")
            continue
        fact = f"Delegation experiment design {d['id']}: {d['title']}. Design: {d['design']}"
        if d.get("measure"):
            fact += f" Measure: {d['measure']}."
        designs.append(
            {
                "kind": "experiment-design",
                "key": f"exp:{d['id']}",
                "repo": repo,
                "title": d["title"],
                "fact": fact,
            }
        )
    return [*tickets, *reads, *designs], skipped


def read_generated(
    sources: ModelBacklogSources,
    watcher_state: dict[str, Any] | None,
    sent: dict[str, dict[str, Any]],
    now: str,
) -> tuple[list[dict[str, Any]], str]:
    """Read each source locally; unread ownership suppresses tickets and readbacks (OMN-17427)."""
    unread: list[str] = []

    def read(
        name: str,
        path: Any,
        load: Callable[[Any], Any] = _json,
        default: Any = None,
    ) -> Any:
        if path is None:
            return default
        try:
            return load(path)
        except (Exception, SystemExit) as exc:
            detail = " ".join(str(exc).splitlines())
            # Overlay errors name required keys and how to supply them; keep both.
            if not isinstance(exc, NodeOverlayError):
                detail = detail[:160]
            unread.append(f"{name} unread: {detail}")
            return default

    snapshot_path = sources.linear_snapshot
    compact = snapshot_path.with_suffix(".txt") if snapshot_path is not None else None
    if (
        snapshot_path is not None
        and compact is not None
        and not snapshot_path.is_file()
        and compact.is_file()
    ):
        unread.append(
            f"linear_snapshot unread: only {compact.name}, the compact form with no assignee, labels or "
            f"title, so no ticket is generated; write {snapshot_path.name} with them"
        )
        snapshot: Any = {}
    else:
        snapshot = read("linear_snapshot", sources.linear_snapshot, default={})
    operator = snapshot.get("operator") if isinstance(snapshot, dict) else None
    issues = snapshot.get("issues", []) if isinstance(snapshot, dict) else snapshot
    issues = issues if isinstance(issues, list) else []
    carriers = read("m4_carriers", sources.m4_carriers, default={})
    m4 = carriers.get("criteria", {}) if isinstance(carriers, dict) else {}
    m4 = (
        {c: ids for c, ids in m4.items() if isinstance(ids, list)}
        if isinstance(m4, dict)
        else {}
    )
    scope = read("scope", sources.scope, default={})
    excluded = scope.get("out_of_scope", {}) if isinstance(scope, dict) else {}
    excluded = set(excluded) if isinstance(excluded, dict) else set()
    experiments = read("experiments", sources.experiments, read_experiments, [])
    claimed: set[str] = set()
    readbacks: dict[str, str] = {}
    try:
        if sources.ledger_tickets is None:
            raise ValueError("no ledger reader")
        claimed, readbacks = sources.ledger_tickets()
    except (Exception, SystemExit) as exc:
        unread.append(f"ledger unread: {' '.join(str(exc).splitlines())[:160]}")
        issues, readbacks = [], {}
    open_pr: set[str] = set()
    prs = watcher_state.get("prs", {}) if isinstance(watcher_state, dict) else {}
    for record in prs.values() if isinstance(prs, dict) else []:
        facts = record.get("facts") if isinstance(record, dict) else None
        if not isinstance(facts, dict) or facts.get("state") != "OPEN":
            continue
        text = f"{facts.get('title') or ''} {facts.get('head_ref') or ''}"
        text += " " + " ".join(
            re.findall(
                r"\bCloses\s+(OMN-\d+)\b", str(facts.get("body") or ""), re.IGNORECASE
            )
        )
        open_pr.update(
            t.upper() for t in re.findall(r"\bOMN-\d+\b", text, re.IGNORECASE)
        )
    rule = sources.ticket_rule
    if rule is None and issues:
        unread.append("ticket rule unread: no ticket rule supplied")

    def checked_rule(facts: dict[str, Any]) -> dict[str, Any]:
        if rule is None:
            return {"items": [], "drops": []}
        try:
            return rule(facts)
        except Exception as exc:
            unread.append(
                f"ticket rule unread: {' '.join(str(exc).splitlines())[:160]}"
            )
            return {"items": [], "drops": []}

    try:
        items, skipped = generate_backlog(
            issues,
            operator,
            m4=m4,
            out_of_scope=excluded,
            claimed=claimed,
            open_pr=open_pr,
            readbacks=readbacks,
            experiments=experiments,
            sent=sent,
            rule=checked_rule if rule is not None else None,
            today=now[:10],
        )
    except Exception as exc:
        unread.append(f"generator unread: {' '.join(str(exc).splitlines())[:160]}")
        items, skipped = [], {}
    t, r, e = (sum(i["kind"] == kind for i in items) for kind in GENERATED_KINDS)
    line = (
        f"lab-fill-generator | tickets={t} readbacks={r} experiments={e} rule={TICKET_RULE} skipped: "
        f"{', '.join(f'{k}={v}' for k, v in sorted(skipped.items())) or 'none'}"
    )
    return items, line + (f"; unread: {'; '.join(unread)}" if unread else "")


def _cell(text: str, name: str) -> str:
    m = re.search(rf"(?:^|\|)\s*{name}=([^|]*)", text)
    return m[1].strip() if m else ""


def ledger_tickets(
    ledger: Path, drain_map: Path, registry_root: Path, now: str
) -> tuple[set[str], dict[str, str]]:
    """Live CLAIM tickets and unsettled missing-readback TERMINAL subjects; failures raise (OMN-17427)."""
    spec = importlib.util.spec_from_file_location("_lab_fill_drain_map", drain_map)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load drain_map")
    dm = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dm
    spec.loader.exec_module(dm)
    now_dt = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )  # drain_map reads aware stamps
    grammar = dm.load_grammar(registry_root)
    rows, _ = dm.load_rows(registry_root, 2.0, now_dt, grammar, ledger)
    opened = dm.open_claims(rows, grammar, now_dt - timedelta(days=2))
    last: dict[str, datetime] = {}
    for row in rows:
        stamp = _stamp(row.ts[:20])
        if row.lane and stamp is not None:
            stamp = stamp.replace(tzinfo=UTC)
            last[row.lane] = max(last.get(row.lane, stamp), stamp)
    claimed = {
        tid
        for row in opened
        if row.lane in last and now_dt - last[row.lane] <= LIVE_CLAIM
        for tid in re.findall(r"\bOMN-\d+\b", row.text)
    }
    readbacks: dict[str, str] = {}
    for row in rows:
        if row.rtype != "TERMINAL":
            continue
        # a fill lane rides on the carrier ticket and names the generated one only in its lane (fill-readback-OMN-n-...)
        ids = next(
            (
                ids
                for text in (
                    *(
                        _cell(row.text, name)
                        for name in ("subject", "related", "blocker")
                    ),
                    row.lane,
                    _cell(row.text, "ticket"),
                )
                if (
                    ids := [
                        t.upper()
                        for t in re.findall(r"\bOMN-\d+\b", text or "", re.IGNORECASE)
                    ]
                )
            ),
            [],
        )
        if not ids:
            continue
        # a fill lane that blocked on the readback too settles it: sent again, it would end the same way
        if (
            _cell(row.text, "outcome") == "blocked"
            and "readback" in _cell(row.text, "blocker").lower()
            and not row.lane.startswith("fill-")
        ):
            readbacks[ids[0]] = row.text[:300]
        else:
            readbacks.pop(ids[0], None)
    return claimed, readbacks


def omnimarket_ticket_rule() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The ticket plan rule of this package (omnimarket#4384), called in process with the facts as a JSON-shaped mapping."""

    def rule(facts: dict[str, Any]) -> dict[str, Any]:
        plan = plan_ticket_backlog(ModelLabFillTicketFacts.model_validate(facts))
        return plan.model_dump(mode="json")

    return rule


PLUGIN_ROOT_ENVS = ("OMNI_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")
REGISTRY_ROOT_ENV = "ONEX_REGISTRY_ROOT"


def default_sources(now: str, env: Mapping[str, str]) -> ModelBacklogSources | None:
    """Use the session snapshot, landing carriers, installed scope, ledger and designs (OMN-17427)."""
    if env.get("OMNI_LAB_FILL_GENERATE") == "off":
        return None
    plugin_root = next(
        (Path(env[name]) for name in PLUGIN_ROOT_ENVS if env.get(name)), None
    )

    def skill_file(relative: str) -> Path:
        """The skill file under the plugin root the environment names; a path that does not exist when it names none."""
        if plugin_root is None:
            return Path(f"<{'/'.join(PLUGIN_ROOT_ENVS)} unset>") / "skills" / relative
        paths = [
            plugin_root / "plugins/omni/skills" / relative,
            plugin_root / "skills" / relative,
        ]
        return next((p for p in paths if p.exists()), paths[0])

    registry_value = env.get(REGISTRY_ROOT_ENV) or env.get("OMNI_HOME")
    registry_root = Path(registry_value) if registry_value else None
    ledger = (
        Path(env["ONEX_LEDGER_PATH"])
        if env.get("ONEX_LEDGER_PATH")
        else (
            registry_root / "docs/tracking/ROLLING_WORK_LEDGER.md"
            if registry_root
            else None
        )
    )
    drain_map = skill_file("merge-drain/scripts/drain_map.py")

    def tickets() -> tuple[set[str], dict[str, str]]:
        if ledger is None or registry_root is None:
            raise ValueError(f"{REGISTRY_ROOT_ENV} or ledger is unavailable")
        return ledger_tickets(ledger, drain_map, registry_root, now)

    return ModelBacklogSources(
        linear_snapshot=registry_root
        / ".claude_scratch/plans-today"
        / f"linear-{now[:10]}.json"
        if registry_root
        else None,
        m4_carriers=Path(env.get("HOME") or Path.home())
        / ".local/state/omni/landing/m4-carriers.json",
        scope=skill_file("merge-sweep/sweep_scope.json"),
        experiments=lambda: overlay.experiment_designs(env),
        ledger_tickets=tickets,
        ticket_rule=omnimarket_ticket_rule(),
    )


def read_scope(load: Callable[[], Mapping[str, str]]) -> dict[str, str] | str:
    """The merge sweep's one scope setting ({repo: ruling}: parked, and retired by RULING 2026-10-18T05:25:04Z) as the
    caller's loader reads it, or the reason it cannot be read: an unread scope is never an empty one (OMN-17427)."""
    try:
        return dict(load())
    except (
        ValueError,
        OSError,
        ImportError,
        SyntaxError,
        AttributeError,
        TypeError,
    ) as exc:
        return " ".join(str(exc).split())[-160:] or type(exc).__name__


def fill_argv(
    runner: Path, brief_path: Path, send: dict[str, Any], parent: str | None
) -> list[str]:
    argv = [
        sys.executable,
        "-B",
        str(runner),
        "run",
        "--detach",
        "--no-placement-wait",
        "--brief",
        str(brief_path),
        "--lane",
        send["lane"],
        "--ticket",
        send["ticket"],
        "--model",
        send["model"],
        "--effort",
        send["effort"],
        "--priority-class",
        send["priority_class"],
    ]
    return [
        *argv,
        *(["--repo", send["repo"]] if send["repo"] else []),
        *(["--parent", parent] if parent else []),
        *(["--risk-class", send["risk_class"]] if send.get("risk_class") else []),
    ]


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else "no output")[-200:]


def _send(argv: list[str]) -> tuple[bool, str]:
    """Start the lane detached, without waiting for placement (OMN-17427: a placement wait cost the tick a minute per fill
    while the lab's accounts were limited); a lane the runner detached (exit 0, exit 3 still placing, or a DETACHED line before
    the timeout) is sent. A placement that later ends no-host is the tick's `MISSING dispatch:<lane>` re-placement, as for
    any other dispatch."""
    try:
        ran = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=DISPATCH_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
        return ran.returncode in (
            0,
            3,
        ), f"exit {ran.returncode}: {_tail(ran.stdout + chr(10) + ran.stderr)}"
    except subprocess.TimeoutExpired as exc:
        text = "\n".join(
            o.decode(errors="replace") if isinstance(o, bytes) else o or ""
            for o in (exc.stdout, exc.stderr)
        )
        return "DETACHED lane=" in text, _tail(text or str(exc))
    except OSError as exc:
        return False, _tail(str(exc))


TICK_ACTOR = "script:throughput-tick"
TICK_LANE = "throughput-tick"
TICK_ROW_TEXT_CUT = 200


def tick_row_recorder(
    ledger: Path | None, resolver: Path, timeout: float = 60.0
) -> Callable[[str, str, str], str | None]:
    """The writer of one action row (OMN-19854): ``record(action, subject, detail)`` appends one STATUS row, ``actor=script:
    throughput-tick``, through the plugin's row writer and returns None, or the reason it was refused. The ruling audit counts
    a mechanism acting only by a row whose actor= names it, and the tick's lane dispatches carried the runner's name. It never
    raises: the action it records has already happened."""

    def record(action: str, subject: str, detail: str) -> str | None:
        if ledger is None:
            return "no ledger to append to"
        words = " ".join(
            f"throughput tick {action}: {subject}: {detail}".replace("|", "/").split()
        )[:TICK_ROW_TEXT_CUT]
        cmd = [
            sys.executable,
            str(resolver),
            "row",
            "STATUS",
            "--ledger",
            str(ledger),
            "--lane",
            TICK_LANE,
        ]
        for key, value in (
            ("actor", TICK_ACTOR),
            ("action", action),
            ("subject", subject),
        ):
            cmd += ["--kv", f"{key}={value}"]
        try:
            # The ledger CLI requires a detail directory even for successful writes; hosted runners have no registry root.
            with tempfile.TemporaryDirectory(
                prefix="throughput-tick-row-"
            ) as detail_dir:
                done = subprocess.run(
                    [*cmd, "--detail-dir", detail_dir, "--text", words],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"{type(exc).__name__}: {exc}"[:200]
        if done.returncode != 0:
            why = (done.stdout.strip() or done.stderr.strip()).splitlines()[-1:] or [
                "no output"
            ]
            return f"the row writer exited {done.returncode}: {why[0][:200]}"
        return None

    return record


class HandlerLabFill:
    def __init__(self, ruling_recovery: HandlerRulingRecovery | None = None) -> None:
        self._ruling_recovery = ruling_recovery or HandlerRulingRecovery()

    def handle(self, request: ModelLabFillRequest) -> ModelLabFillResult:
        if request.read_only:
            return self._fill(request)
        with fill_lock(request.state_dir) as held:
            if held:
                return self._fill(request)
        return ModelLabFillResult(
            [
                (
                    "unknown",
                    "lab-fill-sent",
                    (
                        f"another tick still holds {request.state_dir / LOCK_FILE} after {LOCK_WAIT_S:g}s; the fill sent nothing this tick"
                    ),
                    "the next tick fills; a lock held for minutes is a fill still sending",
                )
            ]
        )

    def _fill(self, request: ModelLabFillRequest) -> ModelLabFillResult:
        state = request.state_dir
        backlog_path = (
            Path(request.backlog).expanduser()
            if request.backlog
            else state / BACKLOG_FILE
        )
        backlog, standing, unread = read_backlog(backlog_path)
        sent_path = state / SENT_FILE
        sent = read_sent(sent_path)
        sent_error = ""
        if sent_path.exists():
            try:
                if not isinstance(_json(sent_path), dict):
                    raise ValueError("dispatch history must be a mapping")
            except (OSError, ValueError) as exc:
                sent_error = str(exc)
        # Every tick reads the ruling checks, even with no headroom, a spent
        # backlog, generation disabled, or a read-only caller. Recovery uses the
        # same scope, capacity, brief validation and runner gates as other work.
        recovery = self._ruling_recovery.handle(
            now=request.now,
            state_dir=state,
            sent=sent,
            parent=request.parent,
            read_only=request.read_only,
            record=request.record,
        )
        if sent_error:
            return ModelLabFillResult(
                [
                    *recovery.found,
                    (
                        "unknown",
                        "lab-fill-sent",
                        f"dispatch history unread: {sent_error}; no lane sent",
                        "restore the sent record before dispatch; unread history never proves an item unsent",
                    ),
                ]
            )
        planned = sweep_planned(request.launch)
        placing = placing_lanes(
            sent,
            request.now,
            request.lanes_dir or remote_lane_dir(),
            request.window_hours,
        )
        watcher = request.request.get("watcher_state")
        lanes = request.lanes_dir or remote_lane_dir()
        completed = completed_experiments(backlog, sent, now=request.now, lanes=lanes)
        completed.update(answered_items(backlog, sent, lanes=lanes))
        scope = request.out_of_scope
        scope_unread = scope if isinstance(scope, str) else ""
        holds, holds_unread = read_fixer_holds(request.now, os.environ)
        decision = HandlerIdleSlotFill().handle(
            ModelIdleSlotFillRequest(
                now=request.now,
                hosts=list(
                    (request.request.get("lab_headroom") or {}).get("hosts") or []
                ),
                lines=list(request.lines),
                sweep_planned=planned,
                backlog=backlog,
                generated=recovery.items,
                dispatched=sent,
                window_hours=request.window_hours,
                parent=request.parent,
                out_of_scope=dict(scope) if isinstance(scope, Mapping) else {},
                scope_unread=scope_unread,
                placing=placing,
                queue_reserved=request.queue_reserved,
                standing_tickets=standing,
                watcher_state=watcher,
                completed=completed,
                fixer_holds=holds,
                fixer_holds_unread=holds_unread,
                repo_required_kinds=overlay.repo_required_kinds(),
            )
        )
        found: list[tuple[str, str, str, str]] = list(recovery.found)
        if holds_unread:
            found.append(
                (
                    "unknown",
                    "lab-fill-fixer-holds",
                    f"fixer HOLD ledger unread; repository work is not sent: {holds_unread}",
                    "restore the declared ledger and its archive reads",
                )
            )
        if unread is not None:
            found.append(
                (
                    "unknown",
                    "lab-fill-backlog",
                    f"the pillar backlog is unreadable: {unread}",
                    'fix the file\'s JSON: {"items": [{kind, key, repo, title}]}',
                )
            )
        if scope_unread:
            found.append(
                (
                    "unknown",
                    "lab-fill-scope",
                    f"the merge sweep's scope setting is unreadable, so the fill sends "
                    f"no lane that names a repository: {scope_unread}",
                    'fix merge-sweep/sweep_scope.json ({"out_of_scope": {repo: ruling}})',
                )
            )
        sources = request.sources
        if decision.idle_after > 0 and sources is None:
            sources = default_sources(request.now, os.environ)
        if decision.idle_after > 0 and sources is not None:
            generated, line = read_generated(sources, watcher, sent, request.now)
            found.append(("note", "lab-fill-generator", line, ""))
            if generated:
                completed.update(
                    completed_experiments(generated, sent, now=request.now, lanes=lanes)
                )
                decision = HandlerIdleSlotFill().handle(
                    ModelIdleSlotFillRequest(
                        now=request.now,
                        hosts=list(
                            (request.request.get("lab_headroom") or {}).get("hosts")
                            or []
                        ),
                        lines=list(request.lines),
                        sweep_planned=planned,
                        backlog=backlog,
                        dispatched=sent,
                        window_hours=request.window_hours,
                        parent=request.parent,
                        generated=[*recovery.items, *generated],
                        out_of_scope=dict(scope) if isinstance(scope, Mapping) else {},
                        scope_unread=scope_unread,
                        placing=placing,
                        queue_reserved=request.queue_reserved,
                        standing_tickets=standing,
                        watcher_state=watcher,
                        completed=completed,
                        fixer_holds=holds,
                        fixer_holds_unread=holds_unread,
                        repo_required_kinds=overlay.repo_required_kinds(),
                    )
                )
        if decision.dropped:
            drops = decision.dropped
            detail = "; ".join(f"{d['key']} {d['why']}" for d in drops[:6])
            if len(drops) > 6:
                detail += f"; and {len(drops) - 6} more"
            found.append(
                (
                    "note",
                    "lab-fill-dropped",
                    f"lab-fill-dropped | n={len(drops)}: {detail}",
                    "",
                )
            )
            if not request.read_only:
                path = state / DROPPED_FILE
                try:
                    recorded = _json(path) if path.is_file() else {}
                    recorded = recorded if isinstance(recorded, dict) else {}
                    recorded.update(
                        {d["key"]: {"why": d["why"], "at": request.now} for d in drops}
                    )
                    write_sent(path, recorded)
                except (OSError, ValueError) as exc:
                    found.append(
                        (
                            "unknown",
                            "lab-fill-dropped",
                            f"cannot record dropped items: {path}: {exc}",
                            f"make {state} writable ({LAB_FILL_STATE_ENV} names another)",
                        )
                    )
        if decision.claude_free + decision.codex_only_free + decision.glm_only_free < 1:
            return ModelLabFillResult(found)
        names: list[str] = []
        counts = {"claude": 0, "codex": 0, "glm": 0}
        for send in decision.sends:
            if request.read_only:
                names.append(
                    f"{send['lane']} would be sent ({send['kind']} {send['key']}, {send['model']})"
                )
                continue
            brief_path = state / "lab-fill-briefs" / f"{send['lane']}.brief.md"
            try:
                brief_path.parent.mkdir(parents=True, exist_ok=True)
                brief_path.write_text(send["brief"], encoding="utf-8")
                ok, detail = _send(
                    fill_argv(request.runner, brief_path, send, request.parent)
                )
            except OSError as exc:
                ok, detail = False, _tail(str(exc))
            if ok:
                sent[send["key"]] = {
                    "lane": send["lane"],
                    "at": request.now,
                    "kind": send["kind"],
                    "model": send["model"],
                }
                counts[engine_of(send["model"])] += 1
                names.append(
                    f"{send['lane']} ({send['kind']} {send['key']}, {send['model']})"
                )
                if (
                    request.record is not None
                    and (
                        why := request.record(
                            "lab-fill",
                            send["lane"],
                            f"{send['kind']} {send['key']}, {send['model']}",
                        )
                    )
                    is not None
                ):
                    found.append(
                        (
                            "note",
                            "tick-row",
                            f"the tick row for lab-fill {send['lane']} was not recorded: {why}",
                            "",
                        )
                    )
            else:
                found.append(
                    (
                        "unknown",
                        f"lab-fill-dispatch:{send['lane']}",
                        f"the runner refused {send['kind']} {send['key']}: {detail}",
                        "the next tick sends it again; exit 75 is no lab host free",
                    )
                )
        n = sum(counts.values())
        idle = decision.idle_after + (
            0 if request.read_only else len(decision.sends) - n
        )
        found.append(
            (
                "note",
                "lab-fill",
                (
                    f"lab-fill | sent={n} (claude={counts['claude']} codex={counts['codex']}"
                    + (f" glm={counts['glm']}" if decision.glm_only_free else "")
                    + ") "
                    f"free={decision.claude_free}+{decision.codex_only_free}"
                    + (f"+{decision.glm_only_free}" if decision.glm_only_free else "")
                    + f" sweep_planned={planned}"
                    + (
                        f" backlog_mismatched={decision.backlog_mismatched}"
                        if decision.backlog_mismatched
                        else ""
                    )
                    + (
                        f" placing={'+'.join(f'{k}:{v}' for k, v in sorted(placing.items()))}"
                        if placing
                        else ""
                    )
                    + (
                        " queue_reserved="
                        + "+".join(
                            f"{k}:{v}"
                            for k, v in sorted(request.queue_reserved.items())
                            if v
                        )
                        if any(request.queue_reserved.values())
                        else ""
                    )
                    + f" idle_after={idle}: "
                    f"{', '.join(names) or 'nothing to send'}"
                ),
                "",
            )
        )
        if idle > 0 and not request.read_only:
            done = "; ".join(
                f"{s['key']} {s['why']}"
                for s in decision.skipped[:4]
                if s["why"] != "no free slot"
            )
            found.append(
                (
                    "missing",
                    "lab-fill-backlog",
                    (
                        f"{idle} free lab slot(s) left idle with the pillar backlog spent"
                        f"{' (already sent: ' + done + ')' if done else ''}"
                    ),
                    (
                        f'add work to {backlog_path} (M4 board reds, experiment designs: {{"items": [{{kind, key, repo, title}}]}}) '
                        + (
                            'declare standing_tickets in the backlog file ({kind: "OMN-<child>"}) for classes without their own ticket; '
                            if any(
                                s["why"].startswith("no child ticket")
                                for s in decision.skipped
                            )
                            else ""
                        )
                        + "or dispatch a lane by hand in this turn; the lab is never idle"
                    ),
                )
            )
        if n:
            try:
                write_sent(sent_path, sent)
            except OSError as exc:
                found.append(
                    (
                        "unknown",
                        "lab-fill-sent",
                        f"cannot record what the fill sent: {sent_path}: {exc}",
                        f"make {state} writable ({LAB_FILL_STATE_ENV} names another)",
                    )
                )
        return ModelLabFillResult(found)


# ---- the Stop hook: queue_status.py --idle-guard calls guard_main (OMN-17427) --------------------------------------


class IdleGuardError(Exception):
    """The guard could not check; the hook exits 2 with this on stderr."""


def turn(transcript: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The tool calls ({id, name, input}) and tool results ({id, text}) since the last operator prompt."""
    uses: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = (
            [b for b in content if isinstance(b, dict)]
            if isinstance(content, list)
            else []
        )
        if entry.get("type") == "user":
            if isinstance(content, str) or any(b.get("type") == "text" for b in blocks):
                uses, results = [], []  # a new operator prompt starts a new turn
            for b in blocks:
                if b.get("type") == "tool_result":
                    body = b.get("content")
                    text = (
                        body
                        if isinstance(body, str)
                        else "\n".join(
                            str(c.get("text", ""))
                            for c in body or []
                            if isinstance(c, dict)
                        )
                    )
                    results.append({"id": b.get("tool_use_id"), "text": text})
        elif entry.get("type") == "assistant":
            uses += [
                {"id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
                for b in blocks
                if b.get("type") == "tool_use"
            ]
    return uses, results


def guard(raw: str) -> str:
    """The hook's stdout for one Stop payload: a block decision, or '' to pass."""
    if (
        os.environ.get("CLAUDE_CODE_ENTRYPOINT") != "cli"
        and os.environ.get("ONEX_IDLE_GUARD") != "force"
    ):
        return ""  # a headless lane is not the orchestrator; its slots are the tick's to fill
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise IdleGuardError(
            f"idle-guard: the hook payload is not JSON ({exc})"
        ) from exc
    transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(transcript, str) or not Path(transcript).is_file():
        raise IdleGuardError(
            "idle-guard: the Stop payload carries no readable transcript_path; nothing was checked"
        )
    verdict = HandlerIdleTurnGuard().handle(
        ModelIdleTurnGuardRequest(*turn(Path(transcript)))
    )
    return (
        json.dumps({"decision": "block", "reason": verdict.reason})
        if verdict.refuse
        else ""
    )


def guard_main() -> int:
    """Print a block decision to refuse and exit 0; exit 2 with the reason on stderr when it cannot check."""
    try:
        out = guard(sys.stdin.read())
    except Exception as exc:  # a hook that cannot check refuses loudly, never passes
        message = (
            exc
            if isinstance(exc, IdleGuardError)
            else f"idle-guard: failed ({type(exc).__name__}: {exc}); "
            "nothing was checked"
        )
        sys.stderr.write(f"{message}\n")
        return 2
    if out:
        sys.stdout.write(f"{out}\n")
    return 0
