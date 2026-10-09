# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the merge-throughput tick (OMN-20686).

The retired throughput_tick.py and the loops_check.py checks it called mixed these
decisions with launchctl, ps and file reads. Here the caller reads those and passes the
facts; the handler returns the finding lines and the status line, byte-identical to the
retired script for the controller, merges, floors, escalation and lab-headroom findings.

The lab-headroom finding runs only when the caller supplies `lab_headroom` facts: the
pool hosts with their limited and auth-expired marks, the runner's receipts, the live
placement markers, and, per host, whether the placement module parsed each reading
and the admission refusal it names (that parse stays in the placement module until its
own shard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omnibase_core.types import JsonType

from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelLabHeadroomFacts,
    ModelLabMark,
    ModelThroughputTickRequest,
    ModelThroughputTickResult,
)

CELLS = ("causes", "cause_leases", "cause_members", "cause_exhausted", "fixer_hold")
CONTROLLER_LABEL = "ai.omninode.landing-controller"
CONTROLLER_FIX = (
    "the controller owns this fix: read its status through /omni:landing-controller "
    "(landing_controller.py status); a fixer HOLD, a spent cause budget or no capacity is reported "
    "there and released by the operator; the session starts no fix work for it"
)
CONTROLLER_STATUS_FIX = 'bash "$OMNIBASE_INTERNAL/launchd/landing-controller/install.sh" --status, then --act --g-act-dir <dir> once G-ACT verifies (/omni:landing-controller); never launch the retired Claude drain'
CONTROLLER_REFUSAL_FIX = "env -u PYTHONPATH /opt/homebrew/bin/python3.13 ~/.omninode/landing-controller/skills/merge-drain/scripts/landing_controller.py status, then fix the refusal /omni:landing-controller names (a HASH-MISMATCH or G-ACT-MISSING is a skill PR and a re-install; QUOTA is the GitHub quota consumer); never launch the retired Claude drain"
CONTROLLER_STALE_FIX = "launchctl kickstart -k gui/$(id -u)/ai.omninode.landing-controller, then read ~/.local/state/omni/landing/landing-controller.err.log; never launch the retired Claude drain"
CONTROLLER_NOT_LOADED_FIX = 'bash "$OMNIBASE_INTERNAL/launchd/landing-controller/install.sh" --act --g-act-dir <G-ACT receipts dir> (see /omni:landing-controller); never launch the retired Claude drain'
WATCHER_FIX = (
    "launchctl kickstart -k gui/$(id -u)/ai.omninode.pr-watcher and read its err log"
)
MERGES_FIX = "the merge-throughput tick's procedure: find the cause live and dispatch ONE targeted fix lane for that cause (not a drain)"
POLICY_FIX = "python3 plugins/omni/scripts/landing_policy.py, then fix ~/.config/onex/landing_policy.yaml (or $ONEX_LANDING_POLICY)"
FLOOR_WINDOW = timedelta(hours=2)
FLOOR_FIX = (
    "find this repository's cause live (the controller's degraded list, its red required contexts, its "
    "dev head; /omni:merge-drain's merge-pulse names them) and dispatch ONE targeted fix lane for that "
    "cause (not a drain); pr_claim_registry_cli.py list first for a peer already on it"
)
PID_RE = re.compile(r'"PID"\s*=\s*(\d+)\s*;')
ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def miss(self, loop: str, why: str, fix: str) -> None:
        self.missing.append(loop)
        self.lines.append(f"MISSING {loop} | {why} | FIX: {fix}")

    def unk(self, loop: str, why: str, fix: str) -> None:
        self.unknown.append(loop)
        self.lines.append(f"UNKNOWN {loop} | {why} | FIX: {fix}")

    def note(self, text: str) -> None:
        self.lines.append(f"NOTE {text}")

    def status(self, prefix: str, bad: str) -> str:
        if not self.missing and not self.unknown:
            return f"{prefix} OK checked={','.join(self.checked)}"
        names = ", ".join(self.missing + self.unknown)
        # n counts every finding, so a run of unknowns never reads as `n=0`, which looks healthy
        return f"{prefix} {bad} n={len(self.missing) + len(self.unknown)} unknown={len(self.unknown)}: {names}"


@dataclass
class Cover:
    """One cause that owns or parks its members."""

    key: str
    repo: str
    members: set[str]
    lease_id: str | None = None
    parked_until: str | None = None

    def note(self) -> str:
        if self.lease_id:
            lease = (
                self.lease_id[1:] if self.lease_id.startswith("L") else self.lease_id
            )
            return f"owned by landing-L{lease} cause={self.key}"
        return f"parked cause {self.key} until {self.parked_until}"


def elapsed_minutes(etime: str | None) -> float | None:
    """Minutes from `ps -o etime=` ([[dd-]hh:]mm:ss); None when unreadable."""
    match = ETIME_RE.match(etime.strip()) if etime is not None else None
    if match is None:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 1440 + hours * 60 + minutes + seconds / 60


def tick_finished_at(
    tick: dict[str, Any] | None, started: datetime | None, ts: str
) -> datetime | None:
    """When the last tick ENDED: its ts and heartbeat hold the start, the end adds timing.total_s."""
    if started is None:
        return None
    total = ((tick or {}).get("timing") or {}).get("total_s")
    if (tick or {}).get("ts") == ts and isinstance(total, (int, float)) and total > 0:
        return started + timedelta(seconds=total)
    return started


def check_controller(
    rep: Report,
    request: ModelThroughputTickRequest,
    now: datetime,
    escalated: list[tuple[str, str]],
) -> None:
    rep.checked.append("controller")
    before = len(rep.missing) + len(rep.unknown)
    max_minutes = request.controller_max_minutes
    run_max_minutes = request.controller_run_max_minutes
    running: tuple[int, float] | None = None
    if request.launchctl == "unavailable":
        rep.unk(
            "controller",
            f"cannot run launchctl: {request.launchctl_error}",
            CONTROLLER_STATUS_FIX,
        )
    elif request.launchctl == "not_loaded":
        rep.miss(
            "controller",
            f"launchd job {CONTROLLER_LABEL} is not loaded",
            CONTROLLER_NOT_LOADED_FIX,
        )
    else:
        match = PID_RE.search(request.launchctl_stdout)
        minutes_up = elapsed_minutes(request.pid_etime) if match else None
        if match and minutes_up is not None:
            running = (int(match.group(1)), minutes_up)
    tick: dict[str, Any] | None = request.ticks[0] if request.ticks else None
    if tick is None:
        reason = request.ticks_error or "no complete tick line in the journal tail"
        rep.unk("controller", f"cannot read last tick: {reason}", CONTROLLER_STATUS_FIX)
    ts = ""
    at: datetime | None
    try:
        if request.heartbeat_text is None:
            raise ValueError("heartbeat unreadable")
        ts = request.heartbeat_text.strip()
        at = parse_stamp(ts)
    except ValueError:
        ts = (tick or {}).get("ts", "")
        try:
            at = parse_stamp(ts)
        except (ValueError, TypeError):
            rep.unk(
                "controller",
                "heartbeat and last tick timestamp unreadable",
                CONTROLLER_STALE_FIX,
            )
            at = None
    finished = tick_finished_at(tick, at, ts)
    minutes = (now - finished).total_seconds() / 60 if finished else None
    if running is not None and running[1] > run_max_minutes:
        rep.miss(
            "controller",
            f"tick running as pid {running[0]} for {running[1]:.0f} min (> {run_max_minutes}): stuck",
            CONTROLLER_STALE_FIX,
        )
    elif running is None and minutes is not None and minutes > max_minutes:
        rep.miss(
            "controller",
            f"no tick running and the last tick {ts} ended {minutes:.0f} min ago (> {max_minutes})",
            CONTROLLER_STALE_FIX,
        )
    if tick is not None:
        mode, status = tick.get("mode", "?"), tick.get("status", "?")
        refusal = tick.get("refusal")
        where = f"mode={mode} status={status} at tick {tick.get('tick', '?')}"
        if mode != "act":
            rep.miss("controller", f"{where}: not acting", CONTROLLER_STATUS_FIX)
        elif refusal or status not in ("OK", "DEGRADED"):
            # A refused tick (HASH-MISMATCH, G-ACT-MISSING, QUOTA, a failed read) acted on nothing.
            detail = " ".join(str(tick.get("detail") or "").split())[:200]
            why = f"{where} refusal={refusal or '-'}" + (f" {detail}" if detail else "")
            rep.miss(
                "controller",
                f"{why}: the tick acted on nothing",
                CONTROLLER_REFUSAL_FIX,
            )
        elif status == "DEGRADED":
            # A completed tick that escalated some PRs: the controller owns their fixes.
            for item in tick.get("degraded") or []:
                if isinstance(item, dict):
                    escalated.append(
                        (str(item.get("reason", "?")), str(item.get("subject", "?")))
                    )
    if before == len(rep.missing) + len(rep.unknown):
        if running is not None:
            rep.note(
                f"controller live: tick running as pid {running[0]} for {running[1]:.0f} min "
                f"(bound {run_max_minutes}); last tick {ts}"
            )
        else:
            rep.note(
                f"controller live: mode=act last tick {ts} ended {minutes:.0f} min ago"
            )


def check_merges(
    rep: Report, request: ModelThroughputTickRequest, now: datetime
) -> None:
    rep.checked.append("merges")
    path = request.watcher_path
    if path is None:
        rep.unk(
            "merges",
            "OMNI_HOME is not set and no --pr-watcher-state given",
            WATCHER_FIX,
        )
        return
    max_minutes = request.watcher_max_minutes
    minimum = request.min_merges_per_hour
    try:
        if request.watcher_read_error is not None:
            raise ValueError(request.watcher_read_error)
        data: Any = request.watcher_state
        ts = data["last_tick"]
        age = (now - parse_stamp(ts)).total_seconds() / 60
        prs = data["prs"]
        if not isinstance(prs, dict):
            raise ValueError("prs must be an object")
        opened = count = control = 0
        for pr in prs.values():
            facts = pr["facts"]
            opened += facts.get("state") == "OPEN"
            if facts.get("merged_at"):
                elapsed = now - parse_stamp(facts["merged_at"])
                count += timedelta(0) <= elapsed <= timedelta(hours=1)
                control += timedelta(0) <= elapsed <= timedelta(days=7)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        rep.unk("merges", f"cannot read watcher state {path}: {exc}", WATCHER_FIX)
        return
    if age > max_minutes:
        rep.unk(
            "merges",
            f"stale watcher: last tick {ts} is {age:.0f} min old (> {max_minutes})",
            WATCHER_FIX,
        )
    elif count == 0 and control == 0:
        rep.unk(
            "merges",
            "zero merges in 60 min AND in 7 days (positive control failed)",
            WATCHER_FIX,
        )
    elif count >= minimum:
        rep.note(f"merges last 60 min: {count} (open {opened}, watcher tick {ts})")
    elif opened == 0:
        rep.note(
            f"merges last 60 min: {count} (open 0, watcher tick {ts}); nothing to land"
        )
    else:
        rep.miss(
            "merges",
            f"{count} merges in 60 min with {opened} open (< {minimum}/h is a stall)",
            MERGES_FIX,
        )


def check_floors(
    rep: Report, request: ModelThroughputTickRequest, now: datetime
) -> None:
    rep.checked.append("floors")
    if request.floors_per_repo is None:
        rep.unk(
            "floors",
            f"cannot read the per-repo floors: {request.floors_error}",
            "fix landing_floors.json",
        )
        return
    path = request.watcher_path
    if path is None:
        rep.unk("floors", "no PR watcher state to read", WATCHER_FIX)
        return
    if request.policy_load_error is not None:
        rep.unk(
            "floors",
            f"cannot load the landing policy: {request.policy_load_error}",
            POLICY_FIX,
        )
        return
    if request.policy_error:
        rep.unk(
            "floors",
            f"cannot read the landing policy: {request.policy_error}",
            POLICY_FIX,
        )
        return
    lands_like = {login.strip().lower() for login in request.land_like_operator}
    max_minutes = request.watcher_max_minutes
    try:
        if request.watcher_read_error is not None:
            raise ValueError(request.watcher_read_error)
        data: Any = request.watcher_state
        ts = data["last_tick"]
        operator = str(data["operator"])
        age = (now - parse_stamp(ts)).total_seconds() / 60
        merged: dict[str, list[datetime]] = {}
        waiting: dict[str, int] = {}
        report_only: dict[str, int] = {}
        for pr in data["prs"].values():
            facts, repo = pr["facts"], pr["facts"].get("repo")
            if not repo:
                continue
            if facts.get("merged_at"):
                merged.setdefault(repo, []).append(parse_stamp(facts["merged_at"]))
            elif (
                facts.get("state") == "OPEN"
                and not facts.get("draft")
                and pr.get("cls") != "held-excluded"
            ):
                # The controller cannot land a report-only author's PR, so it is not waiting on it.
                login = str(facts.get("author") or "")
                if bool(
                    (operator and login == operator)
                    or bool(facts.get("author_is_bot"))
                    or (bool(login) and login.strip().lower() in lands_like)
                ):
                    waiting[repo] = waiting.get(repo, 0) + 1
                else:
                    report_only[repo] = report_only.get(repo, 0) + 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        rep.unk("floors", f"cannot read watcher state {path}: {exc}", WATCHER_FIX)
        return
    if age > max_minutes:
        rep.unk(
            "floors",
            f"stale watcher: last tick {ts} is {age:.0f} min old (> {max_minutes})",
            WATCHER_FIX,
        )
        return
    healthy = []
    for repo, floor in sorted(request.floors_per_repo.items()):
        if floor <= 0 or not waiting.get(repo):
            continue
        stamps = merged.get(repo, [])
        if not stamps:
            rep.unk(
                f"floor:{repo}",
                f"no merged {repo} PR in the watcher state (positive control failed): "
                "a zero here would be a guess",
                WATCHER_FIX,
            )
            continue
        recent = sum(timedelta(0) <= now - at <= FLOOR_WINDOW for at in stamps)
        rate = recent / (FLOOR_WINDOW.total_seconds() / 3600)
        since = (now - max(stamps)).total_seconds() / 3600
        if rate < floor:
            rep.miss(
                f"floor:{repo}",
                f"{repo} merged {recent} in 2h ({rate:g}/h, floor {floor:g}/h) with "
                f"{waiting[repo]} PRs waiting; last merge {since:.1f}h ago",
                FLOOR_FIX,
            )
        else:
            healthy.append(f"{repo} {rate:g}/h")
    if healthy:
        rep.note("floors met: " + ", ".join(healthy))
    if report_only:
        rep.note(
            "floors do not count report-only authors' PRs (landing policy): "
            + ", ".join(f"{repo} {n}" for repo, n in sorted(report_only.items()))
        )


def _bare_repo(text: str) -> str:
    """`Owner/repo#n`, `cause:Owner/repo:hex`, `Owner/repo` or `repo` to `repo`."""
    text = str(text).removeprefix("cause:").split("#")[0].split(":")[0]
    return text.rsplit("/", 1)[-1]


def _members(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        pr
        for m in value
        if isinstance(pr := m.get("pr") if isinstance(m, dict) else m, str)
    }


def _cause_records(value: object) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [
            dict(rec, key=rec.get("key") or key)
            for key, rec in value.items()
            if isinstance(rec, dict)
        ]
    return (
        [rec for rec in value if isinstance(rec, dict)]
        if isinstance(value, list)
        else []
    )


def _stamp_after(value: object, now: datetime) -> bool:
    try:
        return parse_stamp(str(value)) > now
    except (ValueError, TypeError):
        return False


def read_covers(
    state_json: JsonType | None, ticks: list[dict[str, Any]], now: datetime
) -> list[Cover]:
    """The causes covering PRs, from state.json and the newest receipt. Unreadable or pre-cause
    files give no cover."""
    covers: list[Cover] = []
    has_cause_state = False
    try:
        state: Any = state_json
        causes = _cause_records(state.get("causes"))
        has_cause_state = isinstance(state.get("causes"), (dict, list))
        by_key = {str(rec.get("key")): rec for rec in causes}
        leases = state.get("leases")
        for lease in leases.values() if isinstance(leases, dict) else leases or []:
            if not isinstance(lease, dict):
                continue
            key = str(lease.get("cause") or lease.get("pr") or "")
            if not key.startswith("cause:"):
                continue
            if lease.get("revoked") is True or lease.get("result_recorded_at"):
                continue
            rec = by_key.get(key, {})
            covers.append(
                Cover(
                    key,
                    _bare_repo(rec.get("repo") or key),
                    _members(lease.get("members") or rec.get("members")),
                    lease_id=str(lease.get("lease_id", "?")),
                )
            )
        for rec in causes:
            if rec.get("key") and _stamp_after(rec.get("parked_until"), now):
                key = str(rec["key"])
                covers.append(
                    Cover(
                        key,
                        _bare_repo(rec.get("repo") or key),
                        _members(rec.get("members")),
                        parked_until=str(rec["parked_until"]),
                    )
                )
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    # State owns current coverage, including an empty cause list after a release. Receipts
    # retain history for the two-tick escalation check, but cannot resurrect old coverage.
    for tick in ticks[:1] if not has_cause_state else []:
        causes_field = tick.get("causes")
        records = (
            causes_field.get("records")
            if isinstance(causes_field, dict)
            else causes_field
        )
        for rec in _cause_records(records):
            if not rec.get("key"):
                continue
            parked = rec.get("parked_until")
            if rec.get("lease_id") or (parked and _stamp_after(parked, now)):
                covers.append(
                    Cover(
                        str(rec["key"]),
                        _bare_repo(rec.get("repo") or rec["key"]),
                        _members(rec.get("members")),
                        lease_id=str(rec["lease_id"]) if rec.get("lease_id") else None,
                        parked_until=str(parked) if parked else None,
                    )
                )
    return covers


def fixer_cells(tick: dict[str, Any]) -> str:
    summary = tick.get("causes")
    fields = {
        "causes": "count",
        "cause_leases": "leases",
        "cause_members": "members",
        "cause_exhausted": "exhausted",
    }
    out = []
    for name in CELLS:
        value = (
            summary.get(fields[name], "unread")
            if isinstance(summary, dict) and name in fields
            else tick.get(name, "unread")
        )
        if name == "fixer_hold" and isinstance(value, list):
            value = ",".join(str(scope) for scope in value) or "none"
        out.append(
            f"{name}={len(value) if name == 'causes' and isinstance(value, list) else value}"
        )
    return " ".join(out)


def _degraded_subjects(tick: dict[str, Any]) -> set[str]:
    return {
        str(i.get("subject")) for i in tick.get("degraded") or [] if isinstance(i, dict)
    }


def report_escalated(
    rep: Report,
    escalated: list[tuple[str, str]],
    covers: list[Cover],
    ticks: list[dict[str, Any]],
) -> None:
    for reason, subject in escalated:
        cover = next(
            (c for c in covers if subject == c.key or subject in c.members), None
        )
        newest = ticks[0] if ticks else {}
        if cover is not None:
            rep.note(f"escalated:{subject} | {cover.note()}")
        elif len(ticks) > 1 and subject in _degraded_subjects(ticks[1]):
            rep.miss(
                f"escalated:{subject}",
                f"the controller escalated it ({reason}) at ticks {ticks[1].get('tick', '?')} and "
                f"{newest.get('tick', '?')} and no cause lease or park covers it; {fixer_cells(newest)}",
                CONTROLLER_FIX,
            )
        else:
            rep.note(
                f"escalated:{subject} | escalated at tick {newest.get('tick', '?')} ({reason}) and no cause "
                "covers it yet: the controller has one more tick to cover it"
            )


def report_floors(
    rep: Report, floor_rep: Report, covers: list[Cover], ticks: list[dict[str, Any]]
) -> None:
    """Merge the floor findings into the tick, replacing their fix-lane advice."""
    rep.checked.extend(floor_rep.checked)
    rep.unknown.extend(floor_rep.unknown)
    cells = fixer_cells(ticks[0] if ticks else {})
    for line in floor_rep.lines:
        head, _, rest = line.partition(" | ")
        if not head.startswith("MISSING floor:"):
            rep.lines.append(line)
            continue
        loop = head.removeprefix("MISSING ")
        cover = next((c for c in covers if c.repo == loop.removeprefix("floor:")), None)
        if cover is not None:
            rep.note(f"{loop} | {rest.rpartition(' | FIX: ')[0]} | {cover.note()}")
        else:
            rep.miss(loop, f"{rest.rpartition(' | FIX: ')[0]}; {cells}", CONTROLLER_FIX)


READING_REFUSALS = ("UNREADABLE(", "ADMISSION-REFUSED(", "LANE-REFUSED(")
READING_WINDOW = timedelta(minutes=15)
READING_FIELD_RE = re.compile(r"(?:[:,])([a-z_]+)=([^,]+)")


def _mark_until(mark: ModelLabMark) -> datetime | None:
    """The mark's `until` stamp; None for an absent mark; ValueError/TypeError when unreadable."""
    if mark.state == "absent":
        return None
    if mark.state == "unreadable" or mark.until is None:
        raise ValueError("unreadable mark")
    return parse_stamp(mark.until)


def _receipt_summary(
    facts: ModelLabHeadroomFacts,
) -> tuple[dict[str, int], dict[str, tuple[datetime, str]]]:
    running: dict[str, int] = {}
    latest: dict[str, tuple[datetime, str]] = {}
    for receipt in facts.receipts:
        try:
            at = parse_stamp(receipt.started_at)
        except (ValueError, TypeError):
            continue  # An interrupted receipt is not evidence of free capacity.
        host = receipt.host
        if host and not receipt.final and receipt.status in ("preparing", "running"):
            if not receipt.pid_valid:
                continue
            if receipt.pid_alive is None or receipt.pid_alive:
                running[host] = running.get(host, 0) + 1
        for reading in receipt.readings:
            name, _, _ = reading.partition(":")
            if name not in latest or at > latest[name][0]:
                latest[name] = (at, reading)
    return running, latest


def check_lab_headroom(
    rep: Report, facts: ModelLabHeadroomFacts, now: datetime
) -> None:
    """Free lane slots in the runner's recent placement receipts; never probes or places."""
    rep.checked.append("lab-headroom")
    if facts.module_unavailable:
        rep.note("lab-headroom: placement module unavailable")
        return
    for name in facts.unavailable_hosts:
        rep.note(f"lab-headroom:{name} | unavailable in host table; no dispatch")
    if facts.placement_error is not None:
        rep.note(f"lab-headroom: cannot read placement state ({facts.placement_error})")
        return
    running, latest = _receipt_summary(facts)
    placed: dict[str, int] = {}
    for name in facts.live_marker_hosts:
        placed[name] = placed.get(name, 0) + 1
    for host in facts.hosts:
        if host.local:
            continue
        label = f"lab-headroom:{host.name}"
        try:
            until = _mark_until(host.limited_mark)
        except (ValueError, TypeError):
            rep.note(f"{label} | limited mark unreadable; no dispatch")
            continue
        if until is not None and until > now:
            rep.note(
                f"{label} | limited until {until.strftime('%Y-%m-%dT%H:%M:%SZ')}; no dispatch"
            )
            continue
        try:
            auth_until = _mark_until(host.auth_mark)
            if host.auth_mark.state == "present" and host.auth_mark.at is None:
                raise ValueError("auth mark lacks `at`")
        except (ValueError, TypeError):
            rep.note(f"{label} | auth-expired mark unreadable; no dispatch")
            continue
        if auth_until is not None and auth_until > now:
            rep.note(
                f"{label} | claude auth expired since {host.auth_mark.at}; log in again; no dispatch"
            )
            continue
        cached = latest.get(host.name)
        if cached is None or not timedelta(0) <= now - cached[0] <= READING_WINDOW:
            rep.note(f"{label} | no recent placement reading; no dispatch")
            continue
        reading = cached[1]
        if any(reason in reading for reason in READING_REFUSALS):
            rep.note(f"{label} | unhealthy: {reading}; no dispatch")
            continue
        parse = host.parses.get(reading)
        if parse is None or not parse.parsed:
            rep.note(f"{label} | unreadable placement reading; no dispatch")
            continue
        fields = dict(READING_FIELD_RE.findall(reading))
        try:
            cap = max(0, host.cap if host.cap is not None else int(fields["cap"]))
            # The snapshot precedes placement. Markers and live receipts include that new lane;
            # max avoids charging it twice and also counts landing workers in the same pool.
            count = max(placed.get(host.name, 0), running.get(host.name, 0))
        except (KeyError, ValueError):
            rep.note(f"{label} | incomplete placement reading; no dispatch")
            continue
        if parse.admission_refusal is not None:
            rep.note(
                f"{label} | admission refused: {parse.admission_refusal}; "
                f"running lanes {count} of cap {cap}, free slots 0"
            )
            continue
        try:
            observed = int(fields["placed"])
            free = max(0, min(cap - count, int(fields["slots"]) + observed - count))
        except (KeyError, ValueError):
            rep.note(f"{label} | incomplete placement reading; no dispatch")
            continue
        if free:
            rep.miss(
                label,
                f"running lanes {count} of cap {cap}, free slots {free}",
                f"dispatch up to {free} lanes of the session's pillar work to {host.name} "
                "through the remote-lane runner; never pin to a host without headroom",
            )
        else:
            rep.note(f"{label} | running lanes {count} of cap {cap}, free slots 0")


class HandlerThroughputTickDecision:
    """Stateless compute: the tick's findings from the facts the caller read."""

    def handle(self, request: ModelThroughputTickRequest) -> ModelThroughputTickResult:
        now = parse_stamp(request.now)
        ticks: list[dict[str, Any]] = [dict(t) for t in request.ticks]
        rep = Report()
        if request.heartbeat_write_error is not None:
            rep.unk(
                "tick-heartbeat",
                f"cannot write {request.heartbeat_write_error}",
                "make the state directory writable (OMNI_SESSION_START_STATE_DIR names another)",
            )
        escalated: list[tuple[str, str]] = []
        check_controller(rep, request, now, escalated)
        check_merges(rep, request, now)
        covers = read_covers(request.state_json, ticks, now)
        floor_rep = Report()
        check_floors(floor_rep, request, now)
        report_floors(rep, floor_rep, covers, ticks)
        report_escalated(rep, escalated, covers, ticks)
        if request.lab_headroom is not None:
            check_lab_headroom(rep, request.lab_headroom, now)
        return ModelThroughputTickResult(
            lines=rep.lines,
            status_line=rep.status("THROUGHPUT", "STALL"),
            missing=rep.missing,
            unknown=rep.unknown,
            checked=rep.checked,
            exit_code=0 if not rep.missing and not rep.unknown else 1,
        )
