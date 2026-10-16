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

OMN-20840 (2026-10-09, 10 merges an hour with 182 open and every problem a NOTE): a park that holds
a repository with zero merges past `park_max_hours` is MISSING with the manual-unblock fix; a lab
host with no recent reading is UNKNOWN, never a quiet NOTE; a runner receipt that ended without
running (no-host, host-limited, a guard limit, failed) is MISSING until a later receipt of its
brief runs; and an open count rising across the last three tick runs is MISSING.

OMN-20864 (operator ruling 2026-10-10T04:08:23Z): idle lab capacity is never left idle while PRs are
open. Given `pr_land` facts, a free-slot FIX also names the pr-land lanes to dispatch through the
lab-fill effect for parked, escalated or unowned-red PRs, decided by the rule lab-fill selection runs
(omnimarket.handlers.rules_lab_fill_pr_land); free slots with none of them say why, and an unreadable
hold source is UNKNOWN and names no lane.

OMN-17427 (2026-10-12): Codex headroom is read apart from Claude's. A host the lab host table lets run
Codex (`codex_allowed`) gets a Codex reading from its newest placement reading (`codex=login/...`,
`CODEX-LIMITED-UNTIL=`, its slots); a Claude-limited or Claude-auth-expired host with free Codex slots is a
MISSING lab-headroom line naming them instead of "limited; no dispatch". The Claude marks never gate Codex
and the `.codex-sandbox-<host>` marks (landing workers only) are not judged.

OMN-20864 (2026-10-13, h202 read 3 free slots while placement refused it on swap traffic): when the caller
sets `live_admission`, each host's free slots are the runner's live pool admission (`admission`), per engine,
and the receipts, marks and parses decide nothing. A refused engine is named with its rule and the other engine
keeps its slots; Claude and Codex lanes draw from one shared slot budget, so the pr-land fallback counts the
host's slots once; a host the probe could not read is UNKNOWN.

OMN-20851: a lane the ledger records as dispatched with no placement receipt for more than
`unplaced_lane_alarm_minutes`, and a cross-host dispatch-venv divergence older than
`venv_divergence_alarm_minutes`, are ALARM verdicts (typed in `alarms`, exit code 1), a younger one a NOTE;
a running receipt counts as a running lane only when a CLAIM carrying `host=` and a run id backs it.

OMN-19998 (operator RULING 2026-10-15T00:42:51Z lane=orchestrator-bb1a): given `repo_status` facts, every
repository with open non-draft PRs and no merge in the window gets one typed status verdict, read from the
watcher state, state.json and tick lines already in the request (merging normally, waiting on CI, base branch
red, merge queue ejecting, shared red unowned, parked or escalated). An unowned shared red or an ejecting
queue is a MISSING `repo-status:<repo>` line whose FIX names one cause lane or the controller; every other
verdict is a NOTE. A verdict event is due only when no unit is open for the repository or its verdict changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omnibase_core.types import JsonType

from omnimarket.handlers.rules_lab_fill_pr_land import plan_pr_land_fallback
from omnimarket.models.lab_fill import ModelLabFillPrLandFacts, ModelLabFillPrLandPlan
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelCodexHeadroom,
    ModelLabAdmission,
    ModelLabHeadroomFacts,
    ModelLabHost,
    ModelLabMark,
    ModelLabReceipt,
    ModelRepoStatusFacts,
    ModelRepoStatusVerdict,
    ModelThroughputTickRequest,
    ModelThroughputTickResult,
    ModelTickAlarmVerdict,
    RepoStatusVerdict,
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
# RULING 2026-10-09T04:10:56Z lane=orchestrator-9f8a: while the controller cannot re-dispatch a stuck cause
# itself, the orchestrator sends a Codex or Opus fix lane for it. A park lasting hours over a repository that
# has stopped landing is that case (OMN-20840: three repositories read as parked notes for 8 to 12 hours).
PARK_FIX = (
    "nothing lands in {repo} until the park ends at {until}: under RULING 2026-10-09T04:10:56Z "
    "lane=orchestrator-9f8a the orchestrator dispatches ONE Codex or Opus fix lane for cause {key} now "
    "(read the cause through /omni:landing-controller status first, and pr_claim_registry_cli.py list for a "
    "peer already on it); gates still decide, no bypass; the tick dispatches nothing"
)
UNREAD_HOST_FIX = (
    "read {host}'s live capacity with `python3 <omni plugin>/scripts/onex_lab_run.py --hosts` (/omni:lab-run; "
    "it runs nothing); a free slot "
    "there takes a lane through the remote-lane runner, whose placement receipt refreshes this line; a host "
    "that cannot be read is a lab fault to name to the operator, never free and never full"
)
NEVER_RAN = (
    "no-host",
    "host-limited",
    "guard-memory-max",
    "guard-runtime-max",
    "failed",
)
RAN = ("running", "done")
DISPATCH_FIX = (
    "the lane never ran: report it as {status}, never as running; re-dispatch its brief ({brief}) through "
    "the remote-lane runner once a lab-headroom line shows a free slot, or tell the operator it is waiting; "
    "the tick dispatches nothing"
)
OPEN_TREND_FIX = (
    "inflow exceeds landing: read the largest blocker class (/omni:queue-status, /omni:merge-drain's "
    "merge-pulse) and dispatch ONE fix lane for that class under RULING 2026-10-09T04:10:56Z "
    "lane=orchestrator-9f8a; the tick dispatches nothing"
)
UNPLACED_LANE_FIX = (
    "the lane has no placement receipt and no CLAIM-backed run: never report it as running; read "
    "~/.local/state/omni/remote-lanes/<lane>/rlane-*.json for its status, then re-dispatch its brief "
    "through the remote-lane runner once a lab-headroom line shows a free slot, or tell the operator it "
    "never ran; the tick dispatches nothing"
)
VENV_DIVERGENCE_FIX = (
    "bring the dispatch venvs back in step: reconcile the lagging host's venv from the dev head, read "
    "both hosts' venvs again, and until they agree place no lane on either host that needs the diverged "
    "package set; the tick dispatches nothing"
)
SHARED_RED_FIX = (
    "dispatch ONE cause lane for check {check} in {repo} (red on {prs}; no cause lease or park owns it), "
    "pr_claim_registry_cli.py list first for a peer already on it; gates still decide; the tick dispatches nothing"
)
EJECT_FIX = (
    "the merge queue ejected {prs} for the base-branch red {checks}: the controller re-admits them once the base "
    "is green (/omni:landing-controller status); with no cause lease on it, dispatch ONE cause lane for {checks} "
    "on the {repo} base branch; the tick dispatches nothing"
)
EJECT_OWNED_FIX = "the controller owns it ({owner}): read /omni:landing-controller status; the session starts no fix work for it"
BASE_RED = frozenset({"failure", "timed_out", "startup_failure"})
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
    alarms: list[ModelTickAlarmVerdict] = field(default_factory=list)
    codex: list[ModelCodexHeadroom] = field(default_factory=list)
    # floor:<repo> -> (merges in the window, PRs waiting), for the park rule (OMN-20840)
    floor_stats: dict[str, tuple[int, int]] = field(default_factory=dict)

    def miss(self, loop: str, why: str, fix: str) -> None:
        self.missing.append(loop)
        self.lines.append(f"MISSING {loop} | {why} | FIX: {fix}")

    def unk(self, loop: str, why: str, fix: str) -> None:
        self.unknown.append(loop)
        self.lines.append(f"UNKNOWN {loop} | {why} | FIX: {fix}")

    def alarm(
        self, loop: str, since: str, age_minutes: float, why: str, fix: str
    ) -> None:
        """An ALARM is its own verdict class: typed for the caller's evidence file, never also a
        MISSING or UNKNOWN finding (OMN-20851)."""
        self.alarms.append(
            ModelTickAlarmVerdict(
                loop=loop, since=since, age_minutes=age_minutes, why=why, fix=fix
            )
        )
        self.lines.append(f"ALARM {loop} | {why} | FIX: {fix}")

    def note(self, text: str) -> None:
        self.lines.append(f"NOTE {text}")

    def status(self, prefix: str, bad: str) -> str:
        if self.alarms:
            names = ", ".join(
                [alarm.loop for alarm in self.alarms] + self.missing + self.unknown
            )
            n = len(self.alarms) + len(self.missing) + len(self.unknown)
            return f"{prefix} ALARM n={n} unknown={len(self.unknown)}: {names}"
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
) -> int | None:
    """The merges finding; returns the open count when the watcher state is fresh, else None."""
    rep.checked.append("merges")
    path = request.watcher_path
    if path is None:
        rep.unk(
            "merges",
            "OMNI_HOME is not set and no --pr-watcher-state given",
            WATCHER_FIX,
        )
        return None
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
        return None
    if age > max_minutes:
        rep.unk(
            "merges",
            f"stale watcher: last tick {ts} is {age:.0f} min old (> {max_minutes})",
            WATCHER_FIX,
        )
        return None
    if count == 0 and control == 0:
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
    return opened


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
            rep.floor_stats[f"floor:{repo}"] = (recent, waiting[repo])
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


def _long_park(
    cover: Cover, stats: tuple[int, int] | None, now: datetime, max_hours: float
) -> float | None:
    """Hours left on a park that holds a repository with PRs waiting and zero merges in the window,
    when they exceed `max_hours`; else None (a lease, a short park or a landing repository)."""
    if cover.lease_id or not cover.parked_until or stats is None:
        return None
    recent, waiting = stats
    try:
        left = (parse_stamp(cover.parked_until) - now).total_seconds() / 3600
    except (ValueError, TypeError):
        return None
    return left if recent == 0 and waiting > 0 and left > max_hours else None


def report_floors(
    rep: Report,
    floor_rep: Report,
    covers: list[Cover],
    ticks: list[dict[str, Any]],
    now: datetime | None = None,
    park_max_hours: float = 2.0,
) -> None:
    """Merge the floor findings into the tick, replacing their fix-lane advice. A cause lease covers a
    floor; a park covers it only while it ends within `park_max_hours`, or while the repository still
    lands (OMN-20840)."""
    rep.checked.extend(floor_rep.checked)
    rep.unknown.extend(floor_rep.unknown)
    cells = fixer_cells(ticks[0] if ticks else {})
    for line in floor_rep.lines:
        head, _, rest = line.partition(" | ")
        if not head.startswith("MISSING floor:"):
            rep.lines.append(line)
            continue
        loop = head.removeprefix("MISSING ")
        repo = loop.removeprefix("floor:")
        cover = next((c for c in covers if c.repo == repo), None)
        left = (
            _long_park(cover, floor_rep.floor_stats.get(loop), now, park_max_hours)
            if cover is not None and now is not None
            else None
        )
        if cover is not None and left is not None:
            rep.miss(
                loop,
                f"{rest.rpartition(' | FIX: ')[0]}; {cover.note()}, {left:.1f}h from now "
                f"(> {park_max_hours:g}h)",
                PARK_FIX.format(repo=repo, until=cover.parked_until, key=cover.key),
            )
        elif cover is not None:
            rep.note(f"{loop} | {rest.rpartition(' | FIX: ')[0]} | {cover.note()}")
        else:
            rep.miss(loop, f"{rest.rpartition(' | FIX: ')[0]}; {cells}", CONTROLLER_FIX)


READING_REFUSALS = ("UNREADABLE(", "ADMISSION-REFUSED(", "LANE-REFUSED(")
READING_WINDOW = timedelta(minutes=15)
READING_FIELD_RE = re.compile(r"(?:[:,])([a-z_]+)=([^,]+)")
CODEX_LIMITED_RE = re.compile(r",CODEX-LIMITED-UNTIL=([^,]+)")


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
            # A running lane needs a CLAIM carrying `host=` and a run id (OMN-20851).
            claimed = bool(receipt.claim_host and receipt.claim_run_id)
            if claimed and (receipt.pid_alive is None or receipt.pid_alive):
                running[host] = running.get(host, 0) + 1
        for reading in receipt.readings:
            name, _, _ = reading.partition(":")
            if name not in latest or at > latest[name][0]:
                latest[name] = (at, reading)
    return running, latest


def _cap_count(
    host: ModelLabHost,
    fields: dict[str, str],
    running: dict[str, int],
    placed: dict[str, int],
) -> tuple[int, int]:
    """(cap, running lanes) of a host from its reading's fields; KeyError/ValueError when the reading lacks
    them. The snapshot precedes placement: markers and live receipts include that new lane, so max avoids
    charging it twice and also counts landing workers in the same pool."""
    cap = max(0, host.cap if host.cap is not None else int(fields["cap"]))
    return cap, max(placed.get(host.name, 0), running.get(host.name, 0))


def _free_slots(fields: dict[str, str], cap: int, count: int) -> int:
    """Further lanes a host can take: its reading's slots, with the placed units the reading already counts."""
    observed = int(fields["placed"])
    return max(0, min(cap - count, int(fields["slots"]) + observed - count))


def _codex_headroom(
    host: ModelLabHost,
    latest: dict[str, tuple[datetime, str]],
    running: dict[str, int],
    placed: dict[str, int],
    now: datetime,
) -> ModelCodexHeadroom | None:
    """The host's Codex headroom from its newest placement reading, apart from Claude's: the reading names the
    Codex login (`codex=login/<sandbox>`) and a Codex usage limit (`CODEX-LIMITED-UNTIL=`), and its slots are
    the host's admission slots. None: the shared host table does not let this host run Codex. The sandbox word
    is not judged: the `.codex-sandbox-<host>` marks gate landing workers only, and a Codex lane probes its
    own sandbox before Codex starts."""
    if not host.codex_allowed:
        return None

    def none(why: str, **cells: Any) -> ModelCodexHeadroom:
        return ModelCodexHeadroom(host=host.name, free=0, refusal=why, **cells)

    cached = latest.get(host.name)
    if cached is None or not timedelta(0) <= now - cached[0] <= READING_WINDOW:
        return none(
            f"no placement reading in the last {READING_WINDOW.total_seconds() / 60:.0f} min"
        )
    reading = cached[1]
    parse = host.parses.get(reading)
    if any(reason in reading for reason in READING_REFUSALS):
        return none("unhealthy placement reading")
    if parse is None or not parse.parsed:
        return none("unreadable placement reading")
    fields = dict(READING_FIELD_RE.findall(reading))
    login = fields.get("codex", "").partition("/")[0]
    if login != "login":
        return none("no Codex login" if login == "nologin" else "Codex login unread")
    limited = CODEX_LIMITED_RE.search(reading)
    if limited is not None:
        try:
            if parse_stamp(limited.group(1)) > now:
                return none(f"Codex usage limit until {limited.group(1)}")
        except ValueError:
            return none("Codex usage limit mark unreadable")
    try:
        cap, count = _cap_count(host, fields, running, placed)
        if parse.admission_refusal is not None:
            return none(
                f"admission refused: {parse.admission_refusal}", cap=cap, running=count
            )
        free = _free_slots(fields, cap, count)
    except (KeyError, ValueError):
        return none("incomplete placement reading")
    if free == 0:
        return none("no free Codex slots", cap=cap, running=count)
    return ModelCodexHeadroom(host=host.name, free=free, cap=cap, running=count)


def _claude_blocked_host(
    rep: Report,
    label: str,
    host: ModelLabHost,
    note: str,
    blocked: str,
    codex: ModelCodexHeadroom | None,
) -> None:
    """A host Claude lanes cannot use. Free Codex slots there are MISSING capacity (2026-10-12: the tick said
    `limited until 22:01Z; no dispatch` all day over 11 free Codex slots); otherwise the host is a NOTE that
    names why Codex cannot use it either."""
    if codex is None:
        rep.note(f"{label} | {note}; no dispatch")
        return
    claimed = codex.model_copy(update={"claude_blocked": blocked})
    rep.codex.append(claimed)
    if claimed.free:
        rep.miss(
            label,
            f"{blocked}; codex free slots {claimed.free} "
            f"(running lanes {claimed.running} of cap {claimed.cap})",
            f"dispatch up to {claimed.free} Codex lanes (--model codex) to {host.name} through the "
            "remote-lane runner; never pin to a host without headroom",
        )
    else:
        rep.note(f"{label} | {note}; no dispatch (codex: {claimed.refusal})")


LIVE_UNREAD_FIX = (
    "rerun the tick; if the probe stays unreadable, read {host}'s live capacity with "
    "`python3 <omni plugin>/scripts/onex_lab_run.py --hosts` (/omni:lab-run; it runs nothing); a host that "
    "cannot be read is a lab fault to name to the operator, never free and never full"
)
PR_LAND_FIX = "; with no session work, land these through the lab-fill effect as pr-land lanes: {prs}"
HOLD_SOURCE_FIX = (
    "read the hold source again and rerun the tick; the pr-land fallback names no lane while its holds "
    "are unread"
)


def check_lab_headroom(
    rep: Report,
    facts: ModelLabHeadroomFacts,
    now: datetime,
    pr_land: ModelLabFillPrLandFacts | None = None,
) -> ModelLabFillPrLandPlan | None:
    """Free lane slots in the runner's recent placement receipts; never probes or places. With PR-land
    facts, the free slots' FIX names the pr-land fallback's lanes (OMN-20864)."""
    rep.checked.append("lab-headroom")
    if facts.module_unavailable:
        rep.note("lab-headroom: placement module unavailable")
        return None
    for name in facts.unavailable_hosts:
        rep.note(f"lab-headroom:{name} | unavailable in host table; no dispatch")
    if facts.placement_error is not None:
        rep.note(f"lab-headroom: cannot read placement state ({facts.placement_error})")
        return None
    if facts.live_admission:
        return _pr_land_fallback(rep, _check_live_admission(rep, facts), now, pr_land)
    free_lines: list[
        tuple[int, int]
    ] = []  # (index in rep.lines, free slots), host order
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
            _claude_blocked_host(
                rep,
                label,
                host,
                f"limited until {until.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                f"claude limited until {until.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                _codex_headroom(host, latest, running, placed, now),
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
            _claude_blocked_host(
                rep,
                label,
                host,
                f"claude auth expired since {host.auth_mark.at}; log in again",
                f"claude auth expired since {host.auth_mark.at}",
                _codex_headroom(host, latest, running, placed, now),
            )
            continue
        cached = latest.get(host.name)
        if cached is None or not timedelta(0) <= now - cached[0] <= READING_WINDOW:
            # An unread host is neither free nor full (OMN-20840: h101 and h201 read as quiet notes
            # while one lab lane ran).
            newest = cached[0].strftime("%Y-%m-%dT%H:%M:%SZ") if cached else "none"
            rep.unk(
                label,
                f"no placement reading in the last {READING_WINDOW.total_seconds() / 60:.0f} min "
                f"(newest {newest}): its capacity is unread",
                UNREAD_HOST_FIX.format(host=host.name),
            )
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
        codex = _codex_headroom(host, latest, running, placed, now)
        if codex is not None:
            rep.codex.append(codex)
        try:
            cap, count = _cap_count(host, fields, running, placed)
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
            free = _free_slots(fields, cap, count)
        except (KeyError, ValueError):
            rep.note(f"{label} | incomplete placement reading; no dispatch")
            continue
        if free:
            free_lines.append((len(rep.lines), free))
            rep.miss(
                label,
                f"running lanes {count} of cap {cap}, free slots {free}",
                f"dispatch up to {free} lanes of the session's pillar work to {host.name} "
                "through the remote-lane runner; never pin to a host without headroom",
            )
        else:
            rep.note(f"{label} | running lanes {count} of cap {cap}, free slots 0")
    return _pr_land_fallback(rep, free_lines, now, pr_land)


def _live_fix(host: str, admission: ModelLabAdmission) -> str:
    """The FIX of a host with admitted slots: only an admitted engine is named, and the engines share one budget."""
    lanes = []
    if admission.claude_slots:
        lanes.append(f"up to {admission.claude_slots} Claude lanes")
    if admission.codex_slots:
        lanes.append(f"up to {admission.codex_slots} Codex lanes (--model codex)")
    return (
        f"dispatch {' or '.join(lanes)} to {host} through the remote-lane runner, sharing "
        f"{admission.slots} slots; never pin to a host without headroom"
    )


def _check_live_admission(
    rep: Report, facts: ModelLabHeadroomFacts
) -> list[tuple[int, int]]:
    """One finding per host from the runner's live admission (OMN-20864): free slots are what placement would
    admit now, per engine, never the cap minus the lanes a receipt shows. A refused engine is named with the rule
    that refused it and the other engine keeps its slots; a host the probe could not read is UNKNOWN. Returns
    (index in rep.lines, free slots) of each MISSING line, host order."""
    free_lines: list[tuple[int, int]] = []
    for host in facts.hosts:
        label = f"lab-headroom:{host.name}"
        adm = host.admission
        if adm is None or adm.probe_error is not None:
            why = "no live admission reading" if adm is None else adm.probe_error
            rep.unk(
                label,
                f"live admission probe unreadable ({why}): its capacity is unread",
                LIVE_UNREAD_FIX.format(host=host.name),
            )
            continue
        rep.codex.append(
            ModelCodexHeadroom(
                host=host.name,
                free=adm.codex_slots,
                cap=adm.cap,
                running=adm.lanes,
                refusal=None if adm.codex_slots else adm.codex_refusal,
                claude_blocked=None if adm.claude_slots else adm.claude_refusal,
            )
        )
        summary = (
            f"running lanes {adm.lanes}, placed units {adm.placed} of cap {adm.cap}, "
            f"free Claude slots {adm.claude_slots}, free Codex slots {adm.codex_slots} "
            f"(shared slots {adm.slots})"
        )
        refused = "".join(
            f"; {engine} refused: {reason}"
            for engine, reason in (
                ("Claude", adm.claude_refusal),
                ("Codex", adm.codex_refusal),
            )
            if reason
        )
        free = max(adm.claude_slots, adm.codex_slots)
        if free:
            free_lines.append((len(rep.lines), free))
            rep.miss(label, summary + refused, _live_fix(host.name, adm))
        else:
            rep.note(f"{label} | {summary}{refused}; no dispatch")
    return free_lines


def _pr_land_fallback(
    rep: Report,
    free_lines: list[tuple[int, int]],
    now: datetime,
    pr_land: ModelLabFillPrLandFacts | None,
) -> ModelLabFillPrLandPlan | None:
    """The idle-slot pr-land fallback over the free-slot lines (OMN-20864)."""
    if pr_land is None:
        return None
    plan = plan_pr_land_fallback(
        pr_land, sum(free for _, free in free_lines), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    if plan.failure is not None:
        rep.unk("lab-headroom:pr-land", plan.reason, HOLD_SOURCE_FIX)
        return plan
    lanes = [f"{d.pr} ({d.pr_class})" for d in plan.dispatch]
    for index, free in free_lines:
        mine, lanes = lanes[:free], lanes[free:]
        if mine:
            rep.lines[index] += PR_LAND_FIX.format(prs=", ".join(mine))
    if free_lines and not plan.dispatch:
        rep.note(f"lab-headroom:pr-land | {plan.reason}")
    return plan


def _receipt_reason(receipt: ModelLabReceipt) -> str:
    text = receipt.reason or (
        "readings: " + " ".join(receipt.readings)
        if receipt.readings
        else "no reason recorded"
    )
    text = " ".join(text.split())
    return text if len(text) <= 200 else text[:197] + "..."


def check_dispatches(
    rep: Report, facts: ModelLabHeadroomFacts, now: datetime, window_hours: float
) -> None:
    """A dispatched lane whose receipt ended without running, until its brief runs (OMN-20840: six lanes
    ended no-host within a minute and were reported as running for hours)."""
    rep.checked.append("dispatches")
    if facts.module_unavailable or facts.placement_error is not None:
        why = facts.placement_error or "placement module unavailable"
        rep.unk(
            "dispatches",
            f"the runner's receipts are unread ({why})",
            "read ~/.local/state/omni/remote-lanes/<lane>/rlane-*.json by hand for status no-host or failed",
        )
        return
    window = timedelta(hours=window_hours)
    dated: list[tuple[datetime, ModelLabReceipt]] = []
    for receipt in facts.receipts:
        try:
            dated.append((parse_stamp(receipt.started_at), receipt))
        except (ValueError, TypeError):
            continue
    newest: dict[str, tuple[datetime, ModelLabReceipt]] = {}
    for at, receipt in dated:
        if receipt.status not in NEVER_RAN or not timedelta(0) <= now - at <= window:
            continue
        lane = receipt.lane or receipt.run_id or receipt.path or "?"
        if lane not in newest or at > newest[lane][0]:
            newest[lane] = (at, receipt)
    for lane, (at, receipt) in sorted(newest.items(), key=lambda item: item[1][0]):
        superseded = any(
            later > at
            and other.status in RAN
            and (
                (receipt.brief and other.brief == receipt.brief)
                or (receipt.lane and other.lane == receipt.lane)
            )
            for later, other in dated
        )
        if superseded:
            continue
        rep.miss(
            f"dispatch:{lane}",
            f"dispatched {receipt.started_at}, ended {receipt.status} ({_receipt_reason(receipt)}); "
            f"receipt {receipt.path or receipt.run_id or '?'}; no later receipt of its brief is running or done",
            DISPATCH_FIX.format(
                status=receipt.status, brief=receipt.brief or "unrecorded"
            ),
        )


def check_open_trend(
    rep: Report, request: ModelThroughputTickRequest, now: datetime, opened: int | None
) -> None:
    """The open count across the last three tick runs: up by more than `open_rise_pct` is MISSING."""
    rep.checked.append("open-trend")
    if opened is None:
        return  # the merges finding already names an unread or stale watcher
    max_age = timedelta(hours=request.open_history_max_age_hours)
    points: list[tuple[str, int]] = []
    for point in request.open_history or []:
        try:
            if timedelta(0) <= now - parse_stamp(point.at) <= max_age:
                points.append((point.at, point.open))
        except (ValueError, TypeError):
            continue
    series = [*points[-2:], (request.now, opened)]
    counts = " -> ".join(str(n) for _, n in series)
    if len(series) < 3:
        rep.note(
            f"open-trend | open {counts}; {len(series)} of 3 tick runs recorded in the last "
            f"{request.open_history_max_age_hours:g}h"
        )
        return
    first = series[0][1]
    rise = (opened - first) / first * 100 if first else (100.0 if opened else 0.0)
    if rise > request.open_rise_pct:
        rep.miss(
            "open-trend",
            f"open count {counts} across the last three tick runs (since {series[0][0]}, "
            f"+{rise:.1f}%, > {request.open_rise_pct:g}%)",
            OPEN_TREND_FIX,
        )
    else:
        rep.note(
            f"open-trend | open {counts} across the last three tick runs ({rise:+.1f}%)"
        )


def _minutes(value: float) -> str:
    return f"{round(value, 1):g}"


def check_unplaced_lanes(
    rep: Report, request: ModelThroughputTickRequest, now: datetime
) -> None:
    """A lane recorded as dispatched with no placement receipt for longer than the bound is an ALARM
    and is never counted as running; a younger one is a NOTE (OMN-20851)."""
    rep.checked.append("unplaced-lanes")
    bound = request.unplaced_lane_alarm_minutes
    aged: list[tuple[datetime, str, float]] = []
    for lane in request.dispatched_lanes or []:
        if lane.placement_receipt:
            continue
        at = parse_stamp(lane.dispatched_at)
        aged.append((at, lane.lane, max(0.0, (now - at).total_seconds() / 60)))
    for at, name, age in sorted(aged):
        loop = f"unplaced-lane:{name}"
        since = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if age > bound:
            rep.alarm(
                loop,
                since,
                age,
                f"dispatched {since}, {_minutes(age)} min ago with no placement receipt (> {bound}); "
                "not counted as running",
                UNPLACED_LANE_FIX,
            )
        else:
            rep.note(
                f"{loop} | dispatched {since}, {_minutes(age)} min ago with no placement receipt yet "
                f"(alarm after {bound})"
            )


def check_venv_divergence(
    rep: Report, request: ModelThroughputTickRequest, now: datetime
) -> None:
    """A cross-host dispatch-venv divergence older than the bound is an ALARM; a younger one is a NOTE
    (OMN-20851)."""
    rep.checked.append("venv-divergence")
    bound = request.venv_divergence_alarm_minutes
    aged = sorted(
        (
            (parse_stamp(d.diverged_since), d.surface, d)
            for d in request.venv_divergences or []
        ),
        key=lambda item: (item[0], item[1]),
    )
    for at, _, divergence in aged:
        age = max(0.0, (now - at).total_seconds() / 60)
        loop = f"venv-divergence:{divergence.surface}"
        since = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        hosts = ", ".join(divergence.hosts)
        detail = f": {divergence.detail}" if divergence.detail else ""
        if age > bound:
            rep.alarm(
                loop,
                since,
                age,
                f"{hosts} have differed since {since} ({_minutes(age)} min, > {bound}){detail}",
                VENV_DIVERGENCE_FIX,
            )
        else:
            rep.note(
                f"{loop} | {hosts} have differed since {since} ({_minutes(age)} min, "
                f"alarm after {bound}){detail}"
            )


def _pr_short(text: object) -> str:
    """`Owner/repo#n` or `repo#n` to `repo#n`."""
    return str(text).rsplit("/", 1)[-1]


def _base_red(data: dict[str, Any], repo: str) -> list[str]:
    """Checks whose newest run on the repository's default branch failed, sorted."""
    repos = data.get("repos") or {}
    base = (repos.get(repo) or {}).get("default_branch") or "main"
    branch = (data.get("branches") or {}).get(f"{repo}@{base}") or {}
    rows = (branch.get("checks") or {}).get("rows") or []
    newest: dict[str, list[Any]] = {}
    for row in rows:
        if isinstance(row, list) and len(row) >= 4:
            name = str(row[0])
            if name not in newest or str(row[3]) > str(newest[name][3]):
                newest[name] = row
    return sorted(
        name
        for name, row in newest.items()
        if row[1] == "completed" and row[2] in BASE_RED
    )


def _repo_verdict(
    repo: str,
    prs: list[tuple[str, dict[str, Any]]],
    data: dict[str, Any],
    covers: list[Cover],
    ticks: list[dict[str, Any]],
    now: datetime,
) -> tuple[RepoStatusVerdict, str | None, str, float | None, list[str]]:
    """(verdict, check, reason, queue wait, prs) for one repository, most urgent first."""
    keys = [key for key, _ in prs]
    repo_covers = [
        c
        for c in covers
        if c.repo == repo or {_pr_short(m) for m in c.members} & set(keys)
    ]
    leased = [c for c in repo_covers if c.lease_id]
    reds: dict[str, list[str]] = {}
    for key, pr in prs:
        for name in (pr.get("ci") or {}).get("red") or []:
            reds.setdefault(str(name), []).append(key)
    shared = sorted(
        ((name, sorted(on)) for name, on in reds.items() if len(on) > 1),
        key=lambda item: (-len(item[1]), item[0]),
    )
    if shared and not leased and not any(c.parked_until for c in repo_covers):
        name, on = shared[0]
        return "shared_red_unowned", name, f"{name} red on {len(on)} open PRs", None, on
    newest = ticks[0] if ticks else {}
    blocked = newest.get("base_blocked")
    if not isinstance(blocked, dict):
        blocked = {}
    ejected = sorted(
        _pr_short(s)
        for s in blocked.get("prs") or []
        if _pr_short(s).partition("#")[0] == repo
    )
    if ejected:
        checks = sorted(str(c) for c in (blocked.get("checks") or []))
        return (
            "merge_queue_ejecting",
            ", ".join(checks) or None,
            f"{len(ejected)} PRs ejected for base-branch red {', '.join(checks) or 'unread'}",
            None,
            ejected,
        )
    base = _base_red(data, repo)
    if base:
        return (
            "base_branch_red",
            base[0],
            f"base branch red on {', '.join(base)}",
            None,
            [],
        )
    degraded = {_pr_short(s) for s in _degraded_subjects(newest)} & set(keys)
    parked = sorted(
        key
        for key, pr in prs
        if str(pr.get("reason") or "").startswith(f"repo {repo} is parked")
    )
    if repo_covers or degraded or parked:
        if repo_covers:
            reason = "; ".join(sorted({c.note() for c in repo_covers}))
        elif degraded:
            reason = f"the controller escalated {', '.join(sorted(degraded))}"
        else:
            reason = f"repo {repo} is parked"
        return "parked_or_escalated", None, reason, None, sorted(degraded) or parked
    waits = []
    for _, pr in prs:
        ci = pr.get("ci") or {}
        pending = set(ci.get("pending") or [])
        for row in ci.get("detail") or []:
            if isinstance(row, list) and len(row) >= 3 and row[0] in pending:
                try:
                    waits.append((now - parse_stamp(str(row[2]))).total_seconds() / 60)
                except ValueError:
                    continue
    waiting = sorted(key for key, pr in prs if (pr.get("ci") or {}).get("pending"))
    if waiting:
        wait = round(max(waits), 1) if waits else None
        return (
            "waiting_on_ci",
            None,
            f"{len(waiting)} PRs with checks pending",
            wait,
            waiting,
        )
    queued = sorted(key for key, pr in prs if pr.get("queued"))
    reason = (
        f"{len(queued)} PRs in the merge queue"
        if queued
        else "no blocker in the watcher, controller or base reads"
    )
    return "merging_normally", None, reason, None, queued


def check_repo_status(
    rep: Report,
    request: ModelThroughputTickRequest,
    facts: ModelRepoStatusFacts,
    covers: list[Cover],
    ticks: list[dict[str, Any]],
    now: datetime,
) -> list[ModelRepoStatusVerdict]:
    """One verdict per repository with open non-draft PRs and no merge in the window (OMN-19998)."""
    rep.checked.append("repo-status")
    window = timedelta(minutes=facts.window_minutes)
    try:
        if request.watcher_read_error is not None:
            raise ValueError(request.watcher_read_error)
        if request.watcher_path is None:
            raise ValueError("no PR watcher state to read")
        data: Any = request.watcher_state
        ts = data["last_tick"]
        age = (now - parse_stamp(ts)).total_seconds() / 60
        open_prs: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        merged: dict[str, int] = {}
        for key, pr in data["prs"].items():
            pr_facts = pr["facts"]
            repo = pr_facts.get("repo")
            if not repo:
                continue
            if pr_facts.get("merged_at"):
                elapsed = now - parse_stamp(pr_facts["merged_at"])
                merged[repo] = merged.get(repo, 0) + (timedelta(0) <= elapsed <= window)
            elif (
                pr_facts.get("state") == "OPEN"
                and not pr_facts.get("draft")
                and pr.get("cls") != "held-excluded"
            ):
                open_prs.setdefault(repo, []).append((_pr_short(key), pr))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        rep.unk(
            "repo-status",
            f"cannot read watcher state {request.watcher_path}: {exc}",
            WATCHER_FIX,
        )
        return []
    if age > request.watcher_max_minutes:
        rep.unk(
            "repo-status",
            f"stale watcher: last tick {ts} is {age:.0f} min old (> {request.watcher_max_minutes})",
            WATCHER_FIX,
        )
        return []
    units = {u.repo: u for u in facts.open_units}
    verdicts = []
    for repo in sorted(open_prs):
        if merged.get(repo):
            continue
        prs = sorted(open_prs[repo])
        verdict, check, reason, wait, named = _repo_verdict(
            repo, prs, data, covers, ticks, now
        )
        unit = units.get(repo)
        event = unit is None or unit.verdict != verdict
        result = ModelRepoStatusVerdict(
            repo=repo,
            verdict=verdict,
            open_prs=len(prs),
            merges_in_window=0,
            window_minutes=facts.window_minutes,
            check=check,
            reason=reason,
            queue_wait_minutes=wait,
            prs=named,
            opened_at=request.now if event or unit is None else unit.opened_at,
            event=event,
        )
        verdicts.append(result)
        loop = f"repo-status:{repo}"
        why = (
            f"{repo}: {len(prs)} open, 0 merged in {facts.window_minutes} min; {verdict}: {reason}"
            + (f" (waiting {wait:g} min)" if wait is not None else "")
        )
        if verdict == "shared_red_unowned":
            rep.miss(
                loop,
                why,
                SHARED_RED_FIX.format(check=check, repo=repo, prs=", ".join(named)),
            )
        elif verdict == "merge_queue_ejecting":
            owner = next((c for c in covers if c.lease_id and c.repo == repo), None)
            fix = (
                EJECT_OWNED_FIX.format(owner=owner.note())
                if owner is not None
                else EJECT_FIX.format(
                    prs=", ".join(named), checks=check or "unread", repo=repo
                )
            )
            rep.miss(loop, why, fix)
        else:
            rep.note(f"{loop} | {why}")
    return verdicts


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
        opened = check_merges(rep, request, now)
        covers = read_covers(request.state_json, ticks, now)
        floor_rep = Report()
        check_floors(floor_rep, request, now)
        report_floors(rep, floor_rep, covers, ticks, now, request.park_max_hours)
        report_escalated(rep, escalated, covers, ticks)
        if request.open_history is not None:
            check_open_trend(rep, request, now, opened)
        pr_land = None
        if request.lab_headroom is not None:
            pr_land = check_lab_headroom(
                rep, request.lab_headroom, now, request.pr_land
            )
            if request.dispatch_window_hours is not None:
                check_dispatches(
                    rep, request.lab_headroom, now, request.dispatch_window_hours
                )
        if request.dispatched_lanes is not None:
            check_unplaced_lanes(rep, request, now)
        if request.venv_divergences is not None:
            check_venv_divergence(rep, request, now)
        repo_status = (
            check_repo_status(rep, request, request.repo_status, covers, ticks, now)
            if request.repo_status is not None
            else []
        )
        return ModelThroughputTickResult(
            lines=rep.lines,
            status_line=rep.status("THROUGHPUT", "STALL"),
            missing=rep.missing,
            unknown=rep.unknown,
            checked=rep.checked,
            alarms=rep.alarms,
            codex_headroom=rep.codex,
            exit_code=0
            if not rep.missing and not rep.unknown and not rep.alarms
            else 1,
            open_count=opened,
            pr_land=pr_land,
            repo_status=repo_status,
        )
