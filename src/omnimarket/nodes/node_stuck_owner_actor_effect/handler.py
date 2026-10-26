"""A stall the tick detects gets an actor, not an owner on paper (OMN-17427).

2026-10-19: a plugin repository's main was red (validate-plugin, validator-tests) for about 10 hours and its plugin
version bump #1019 sat open 565, 598 and 625 minutes on successive queue-status runs. Every throughput tick printed
``the controller owns it (owned by landing-L11740 / landing-L11815 cause=...)`` while the controller's decisions.jsonl
read ``claim-refused`` ``cause-held-same-base ... no worker until it moves`` after an attempt that never ran
(workers/11542/result.json kind=blocked reason=lab_capacity_unavailable). Nothing acted until a person noticed.

The rule (``judge``): a controller-owned cause or item is OWNED only while the controller's newest decisions.jsonl row
for it is a live lease or a worker spawned in the last 30 minutes. A claim-refused, a held (deferred) row, a refused
cause, or a spawn whose worker result never ran (``NEVER_RAN``) is UNOWNED-NOT-ACTING. ``HandlerStuckOwnerActor``
rewrites the tick's owned lines by that rule and dispatches ONE cause lane per repository, through the remote-lane
runner with ``--cause``, for a red base branch or a stuck plugin version bump past two hours whose owner is not acting,
recorded by cause and base so it is never dispatched twice. ``HandlerDriverGate`` keeps a process-fix driver lane off a
PR whose red is a base-inherited pull_request_target check, or whose head an earlier driver already handed off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers.rules_merge_sweep import lane_work_lease
from omnimarket.models.plugin_version_bump import BUMP_BRANCH
from omnimarket.nodes.node_cause_claim_ownership_effect.handler import claim_names_cause

from .models import (
    ACTING_WINDOW,
    LANE_PREFIX,
    NEVER_RAN,
    STATE_FILE,
    Dispatch,
    Finding,
    ModelDriverGateRequest,
    ModelDriverGateResult,
    ModelOwnerActingRequest,
    ModelOwnerActingResult,
    ModelStuckOwnerRequest,
    ModelStuckOwnerResult,
)

TICKET = "OMN-17427"
DECISIONS_TAIL_BYTES = 8 << 20
LEDGER_TAIL_BYTES = 4 << 20
CLAIM_LIVE = timedelta(
    minutes=45
)  # drain_map's DEAD_AFTER: a lane with no row this long owns nothing
BASE_RED = ("failure", "timed_out", "startup_failure")
OWNED_RE = re.compile(r"owned by landing-L(?P<lease>\d+) cause=(?P<key>[^\s);|]+)")
CONTROLLER_OWNS_RE = re.compile(
    r"the controller owns it \(owned by landing-L\d+ cause=[^)]*\)"
)
RUNNER_TIMEOUT_S = 150
DRIVER = "driver lane pf-drive-"
PROCESS_FIX_LOOP = "process-fix:"


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _short(subject: str) -> str:
    """``Owner/repo#n`` -> ``repo#n``; a cause key is its own name."""
    return (
        subject.rsplit("/", 1)[-1]
        if "#" in subject and not subject.startswith("cause:")
        else subject
    )


def cause_repo(key: str) -> str:
    """``cause:<owner>/<repo>:<signature>`` -> ``repo``."""
    return key.removeprefix("cause:").split(":", 1)[0].rsplit("/", 1)[-1]


def _tail_lines(path: Path, size: int) -> list[str]:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
        stream.seek(max(0, end - size))
        data = stream.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if end > size else lines


def read_decisions(landing: Path) -> list[dict[str, Any]]:
    """decisions.jsonl's tail rows in file (time) order; a missing file is a controller that decided nothing."""
    try:
        lines = _tail_lines(landing / "decisions.jsonl", DECISIONS_TAIL_BYTES)
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def lease_released(lease: Mapping[str, Any], state_dir: Path) -> bool:
    """A revoked lease whose release was confirmed (``released_at`` on the entry or on its lease.json). A revoked
    lease still in state.json with no confirmed release may still have a live process: it still owns."""
    if not lease.get("revoked"):
        return False
    if lease.get("released_at"):
        return True
    try:
        data = json.loads(
            (
                state_dir / "workers" / str(lease.get("lease_id")) / "lease.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("released_at"))


_READ: dict[
    str, tuple[tuple[Any, ...], list[dict[str, Any]], dict[str, dict[str, Any]]]
] = {}


def acting(
    landing: Path,
    subject: str,
    now: datetime | None = None,
    lease_id: int | None = None,
) -> tuple[bool, str]:
    """``judge`` over the controller's files, read once per change of decisions.jsonl and state.json; files that cannot
    be read are no acting owner, and the evidence says they were unread."""
    try:
        sign = tuple(
            (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
            for p in (landing / "decisions.jsonl", landing / "state.json")
        )
        if (_READ.get(str(landing)) or ((),))[0] != sign:
            _READ[str(landing)] = (sign, read_decisions(landing), read_leases(landing))
        _, rows, leases = _READ[str(landing)]
    except (OSError, ValueError) as exc:
        return False, f"the controller's files unread ({type(exc).__name__})"
    return judge(rows, leases, landing, subject, now or datetime.now(UTC), lease_id)


def read_leases(landing: Path) -> dict[str, dict[str, Any]]:
    """state.json's leases by lease id; raises when it cannot be read, so an owner nobody read is never an answer."""
    state = json.loads((landing / "state.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("leases"), list):
        raise ValueError("state.json holds no leases list")
    return {
        str(le["lease_id"]): le
        for le in state["leases"]
        if isinstance(le, dict) and le.get("lease_id") is not None
    }


def worker_result(landing: Path, lease_id: object) -> dict[str, Any] | None:
    try:
        data = json.loads(
            (landing / "workers" / str(lease_id) / "result.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def judge(
    rows: list[dict[str, Any]],
    leases: Mapping[str, Mapping[str, Any]],
    landing: Path,
    subject: str,
    now: datetime,
    lease_id: int | None,
    window: timedelta = ACTING_WINDOW,
) -> tuple[bool, str]:
    """(acting, evidence) from the controller's newest decision for ``subject`` (or, with none, for ``lease_id``)."""
    key = _short(subject)
    mine = [
        r for r in rows if _short(str(r.get("subject") or r.get("cause") or "")) == key
    ]
    if not mine and lease_id is not None:
        mine = [r for r in rows if str(r.get("lease_id")) == str(lease_id)]
    if not mine:
        return False, f"no decisions.jsonl row for {key}"
    row = mine[-1]
    at = _when(row.get("ts"))
    age = None if at is None else int((now - at).total_seconds() // 60)
    where = f"tick {row.get('tick', '?')} {row.get('ts', '?')} {row.get('kind', '?')} {row.get('outcome', '?')}"
    if row.get("kind") == "dispatch_worker" and row.get("outcome") == "spawned":
        lid = row.get("lease_id")
        res = worker_result(landing, lid)
        never = next(
            (
                str(v)
                for v in (
                    (res or {}).get(k) for k in ("kind", "reason", "blocker_kind")
                )
                if v in NEVER_RAN
            ),
            None,
        )
        if never:
            return False, (
                f"{where}: workers/{lid}/result.json kind={(res or {}).get('kind')} {never}: the attempt never ran"
            )
        lease = leases.get(str(lid))
        if res is None and lease is not None and not lease.get("revoked"):
            return True, f"{where}: live lease landing-L{lid}"
        if age is not None and timedelta(minutes=age) <= window:
            return True, f"{where} ({age}m ago)"
        return False, (
            f"{where} {'?' if age is None else age}m ago, "
            f"{'result kind=' + str(res.get('kind')) if res else 'no live lease'}"
        )
    detail = " ".join(str(row.get("detail") or "").split())[:200]
    return False, where + (f": {detail}" if detail else "")


class HandlerOwnerActing:
    """One subject's verdict, for a reader that holds no rows of its own (queue-status)."""

    def handle(self, request: ModelOwnerActingRequest) -> ModelOwnerActingResult:
        try:
            rows, leases = read_decisions(request.landing), read_leases(request.landing)
        except (OSError, ValueError) as exc:
            return ModelOwnerActingResult(
                False, f"controller files unread ({type(exc).__name__})"
            )
        acting, evidence = judge(
            rows,
            leases,
            request.landing,
            request.subject,
            request.now,
            request.lease_id,
            request.window,
        )
        return ModelOwnerActingResult(acting, evidence)


def _cells(line: str) -> tuple[datetime, str, dict[str, str]] | None:
    parts = [p.strip() for p in line.split(" | ")]
    at = _when(parts[0]) if len(parts) > 2 else None
    if at is None:
        return None
    cells: dict[str, str] = {}
    for part in parts[2:]:
        name, eq, value = part.partition("=")
        if eq and re.fullmatch(r"[a-z_-]+", name) and name not in cells:
            cells[name] = value
    return at, parts[1], cells


def live_cause_claims(
    ledger: Path | None, now: datetime, repo: str, check: str
) -> list[str]:
    """Lanes whose open CLAIM names ``repo:check`` and which wrote within 45 minutes.

    A silent lane is stale unless its lane holds a lane-work lease, OMN-21428.
    """
    if ledger is None:
        return []
    try:
        lines = _tail_lines(ledger, LEDGER_TAIL_BYTES)
    except OSError:
        return []
    last: dict[str, datetime] = {}
    claims: dict[str, str] = {}
    leased: set[str] = set()
    for line in lines:
        row = _cells(line)
        if row is None:
            continue
        at, kind, cells = row
        lane = cells.get("lane", "")
        if not lane:
            continue
        last[lane] = max(at, last.get(lane, at))
        if kind == "CLAIM":
            if lane_work_lease(line) is not None:
                leased.add(
                    lane
                )  # the lease CLAIM names no cause: keep the lane's cause CLAIM
            else:
                claims[lane] = line.split(" | ", 1)[1] if " | " in line else line
        elif kind == "TERMINAL":
            claims.pop(lane, None)
            leased.discard(lane)
    return sorted(
        lane
        for lane, text in claims.items()
        if (lane in leased or now - last[lane] <= CLAIM_LIVE)
        and claim_names_cause(text, repo, check)
    )


def required_contexts(
    watcher: Mapping[str, Any] | None, repo: str, base: str
) -> set[str]:
    """The base branch's required contexts (branch protection and rulesets) from the watcher's settings read."""
    branch = (
        (((watcher or {}).get("settings") or {}).get(repo) or {}).get("branches") or {}
    ).get(base) or {}
    return {
        str(c)
        for c in [*(branch.get("contexts") or []), *(branch.get("rule_contexts") or [])]
    }


def base_reds(
    watcher: Mapping[str, Any] | None,
) -> dict[str, tuple[str, str, dict[str, str]]]:
    """repo -> (base, base sha, {red required check: its failing run's completion}) from the watcher's default-branch
    rows. Only a required context counts: a scheduled or advisory workflow red on the base blocks no merge (on
    2026-10-20 eight repositories' bases carried such reds; one, a plugin repository's validate-plugin, was required)."""
    out: dict[str, tuple[str, str, dict[str, str]]] = {}
    for branch in ((watcher or {}).get("branches") or {}).values():
        if not isinstance(branch, dict) or not branch.get("default"):
            continue
        required = required_contexts(
            watcher, str(branch.get("repo")), str(branch.get("branch") or "main")
        )
        newest: dict[str, list[Any]] = {}
        for row in (branch.get("checks") or {}).get("rows") or []:
            if (
                isinstance(row, list)
                and len(row) >= 4
                and (
                    str(row[0]) not in newest
                    or str(row[3]) > str(newest[str(row[0])][3])
                )
            ):
                newest[str(row[0])] = row
        red = {
            name: str(row[4] if len(row) > 4 and row[4] else row[3])
            for name, row in newest.items()
            if row[1] == "completed" and row[2] in BASE_RED and name in required
        }
        if red:
            out[str(branch.get("repo"))] = (
                str(branch.get("branch") or "main"),
                str(branch.get("sha") or ""),
                red,
            )
    return out


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"red_since": {}, "dispatched": {}}
    return {
        "red_since": dict(data.get("red_since") or {}),
        "dispatched": dict(data.get("dispatched") or {}),
    }


def _save_state(path: Path, state: Mapping[str, Any]) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return f"{path}: {exc}"
    return None


def lane_name(repo: str, now: datetime) -> str:
    return f"{LANE_PREFIX}{repo}-{now:%Y%m%dT%H%MZ}"


def brief(
    lane: str, repo: str, items: list[dict[str, Any]], now: datetime, parent: str | None
) -> str:
    facts = "\n".join(f"- {i['why']}" for i in items)
    causes = ", ".join(c for i in items for c in i["causes"])
    return (
        f"# Lane {lane}: {repo} stuck past {int(items[0]['bound_h'])}h with no owner acting\n\n"
        f"ROUTE: model=opus effort=high\nParent: {parent or 'orchestrator'}. Ticket: {TICKET}. Repo: {repo}. "
        "Model: opus. Effort: high.\n\n"
        f"Dispatched by the throughput tick's stuck-owner actor (node_stuck_owner_actor_effect) at {_stamp(now)}, "
        f"for the causes {causes}.\n\n## Facts\n\n{facts}\n\n## Task\n\n"
        "Read the controller's decisions.jsonl rows and worker results for these causes, find each cause at its "
        f"source and fix it in one pull request on {repo}, test-first, then /omni:pr-handoff. If a live lane "
        "already works the cause, end skipped-owned and name it.\n\n"
        "OUT OF SCOPE: merging or arming any PR by hand; prod; creating, rotating or moving credentials.\n"
    )


def runner_dispatch(runner: Path, briefs: Path, parent: str | None) -> Dispatch:
    """Start the cause lane detached through the remote-lane runner, one ``--cause`` per cause."""

    def run(lane: str, repo: str, causes: list[str], text: str) -> tuple[bool, str]:
        path = briefs / f"{lane}.brief.md"
        argv = [
            sys.executable,
            str(runner),
            "run",
            "--brief",
            str(path),
            "--lane",
            lane,
            "--ticket",
            TICKET,
            "--model",
            "opus",
            "--effort",
            "high",
            "--repo",
            repo,
            *(arg for cause in causes for arg in ("--cause", cause)),
            *(["--parent", parent] if parent else []),
            "--placement-wait-s",
            "60",
            "--detach",
        ]
        try:
            briefs.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=RUNNER_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            out = [
                o.decode(errors="replace") if isinstance(o, bytes) else o or ""
                for o in (getattr(exc, "stdout", None), getattr(exc, "stderr", None))
            ]
            detached = next(
                (
                    ln
                    for o in out
                    for ln in o.splitlines()
                    if ln.startswith("DETACHED lane=")
                ),
                "",
            )
            return bool(
                detached
            ), detached or f"cannot run the remote-lane runner: {exc}"
        tail = [
            ln for ln in (done.stdout + "\n" + done.stderr).splitlines() if ln.strip()
        ]
        return done.returncode in (
            0,
            3,
        ), f"exit {done.returncode}: {tail[-1] if tail else 'no output'}"

    return run


class HandlerStuckOwnerActor:
    """Judge every controller-owned line by the acting rule, and dispatch the stuck items' cause lanes."""

    def handle(self, request: ModelStuckOwnerRequest) -> ModelStuckOwnerResult:
        now, landing = request.now, request.landing
        try:
            rows, leases = read_decisions(landing), read_leases(landing)
        except (OSError, ValueError) as exc:
            # the tick's own controller check says the files are unreadable; an owned line is not judged acting
            owned = any(OWNED_RE.search(line) for line in request.lines)
            why = f"stuck-owner | the controller's state.json unread ({type(exc).__name__}): no owner judged, no lane dispatched"
            return ModelStuckOwnerResult(
                list(request.lines),
                [],
                ([("note", "stuck-owner", why, "")] if owned else [])
                + self.gated(request),
            )
        bound_h = request.stuck_after.total_seconds() / 3600
        path = request.state_dir / STATE_FILE
        state = _load_state(path)
        found: list[Finding] = []
        items: dict[str, list[dict[str, Any]]] = {}

        def owner_of(repo: str, checks: Iterable[str]) -> tuple[str | None, list[str]]:
            """(the acting owner, the evidence of each owner found not acting)."""
            idle: list[str] = []
            for lid, le in leases.items():
                key = str(le.get("cause") or le.get("pr") or "")
                if (
                    not key.startswith("cause:")
                    or cause_repo(key) != repo
                    or le.get("revoked")
                ):
                    continue
                if le.get("checks") and not set(map(str, le["checks"])) & set(checks):
                    continue
                acting, evidence = judge(
                    rows, leases, landing, key, now, int(lid), request.window
                )
                if acting:
                    return f"landing-L{lid} cause={key} ({evidence})", idle
                idle.append(f"landing-L{lid} cause={key}: {evidence}")
            for check in checks:
                if lanes := live_cause_claims(request.ledger, now, repo, check):
                    return f"live cause CLAIM {', '.join(lanes)}", idle
            return None, idle

        reds = base_reds(request.watcher)
        seen = set()
        for repo, (base, sha, red) in sorted(reds.items()):
            if request.scope is None or repo not in request.scope:
                continue
            since = []
            for check, completed in red.items():
                k = f"{repo}:{check}"
                seen.add(k)
                first = state["red_since"].get(k) or completed or _stamp(now)
                state["red_since"][k] = first
                since.append(_when(first) or now)
            age = now - min(since)
            checks = sorted(red)
            loop = f"stuck-owner:{repo}@{base}"
            why = f"{repo}@{base} {sha[:10]} red ({', '.join(checks)}) for {int(age.total_seconds() // 60)}m"
            if age < request.stuck_after:
                continue
            owner, idle = owner_of(repo, checks)
            if owner:
                found.append(("note", loop, f"{loop} | {why} | owned by {owner}", ""))
                continue
            items.setdefault(repo, []).append(
                {
                    "key": f"{repo}@{base}:{','.join(checks)}@{sha}",
                    "loop": loop,
                    "bound_h": bound_h,
                    "why": why
                    + (
                        "; controller not acting: " + "; ".join(idle)
                        if idle
                        else "; no owner"
                    ),
                    "causes": [f"{repo}:{c}" for c in checks],
                }
            )
        state["red_since"] = {k: v for k, v in state["red_since"].items() if k in seen}

        for key, rec in sorted(((request.watcher or {}).get("prs") or {}).items()):
            facts = rec.get("facts") if isinstance(rec, dict) else None
            if (
                not isinstance(facts, dict)
                or facts.get("head_ref") != BUMP_BRANCH
                or str(facts.get("state", "")).upper() != "OPEN"
            ):
                continue
            opened = _when(facts.get("created_at"))
            if opened is None or now - opened < request.stuck_after:
                continue
            repo, head = (
                key.split("#")[0].rsplit("/", 1)[-1],
                str(facts.get("head_sha") or ""),
            )
            if request.scope is None or repo not in request.scope:
                continue
            loop = f"stuck-owner:{_short(key)}"
            why = f"plugin version bump {_short(key)} open {int((now - opened).total_seconds() // 60)}m at head {head[:10]}"
            acting, evidence = judge(
                rows, leases, landing, key, now, None, request.window
            )
            if acting:
                found.append(
                    (
                        "note",
                        loop,
                        f"{loop} | {why} | the controller is acting: {evidence}",
                        "",
                    )
                )
                continue
            red_checks = sorted(
                str(c) for c in ((rec.get("ci") or {}).get("red") or [])
            )
            items.setdefault(repo, []).append(
                {
                    "key": f"{_short(key)}@{head}",
                    "loop": loop,
                    "bound_h": bound_h,
                    "why": f"{why}; controller not acting: {evidence}",
                    "causes": [f"{repo}:{c}" for c in red_checks]
                    or [f"{repo}:plugin-version-bump"],
                }
            )

        lane_of: dict[str, str] = {}
        for repo, its in sorted(items.items()):
            done = [i for i in its if i["key"] in state["dispatched"]]
            fresh = [i for i in its if i["key"] not in state["dispatched"]]
            for i in done:
                d = state["dispatched"][i["key"]]
                lane_of[repo] = str(d.get("lane"))
                found.append(
                    (
                        "note",
                        i["loop"],
                        f"{i['loop']} | {i['why']} | cause lane {d.get('lane')} dispatched "
                        f"{d.get('at')} for this cause and base; not dispatched again",
                        "",
                    )
                )
            if not fresh:
                continue
            lane = lane_name(repo, now)
            causes = sorted({c for i in fresh for c in i["causes"]})
            argv = " ".join(f"--cause '{c}'" for c in causes)
            if request.dispatch is None:
                for i in fresh:
                    found.append(
                        (
                            "missing",
                            i["loop"],
                            f"UNOWNED-NOT-ACTING: {i['why']}",
                            f"read-only tick: dispatch cause lane {lane} (onex_remote_lane.py run --lane {lane} "
                            f"--repo {repo} {argv})",
                        )
                    )
                continue
            ok, detail = request.dispatch(
                lane, repo, causes, brief(lane, repo, fresh, now, request.parent)
            )
            if ok:
                lane_of[repo] = lane
                for i in fresh:
                    state["dispatched"][i["key"]] = {
                        "lane": lane,
                        "at": _stamp(now),
                        "causes": causes,
                    }
                    found.append(
                        (
                            "note",
                            i["loop"],
                            f"{i['loop']} | {i['why']} | the tick dispatched cause lane "
                            f"{lane} ({argv}): {detail}",
                            "",
                        )
                    )
            else:
                for i in fresh:
                    found.append(
                        (
                            "missing",
                            i["loop"],
                            f"UNOWNED-NOT-ACTING: {i['why']}",
                            f"the tick's dispatch of cause lane {lane} failed ({detail}); dispatch it "
                            f"(onex_remote_lane.py run --lane {lane} --repo {repo} {argv})",
                        )
                    )
        if err := _save_state(path, state):
            found.append(
                (
                    "unknown",
                    "stuck-owner-record",
                    f"cannot write {err}: the next tick may dispatch again",
                    "make the state directory writable (OMNI_SESSION_START_STATE_DIR names another)",
                )
            )

        lines, promoted = list(request.lines), []
        for n, line in enumerate(lines):
            match = OWNED_RE.search(line)
            level, _, rest = line.partition(" ")
            if match is None or level not in ("MISSING", "NOTE"):
                continue
            acting, evidence = judge(
                rows,
                leases,
                landing,
                match["key"],
                now,
                int(match["lease"]),
                request.window,
            )
            if acting:
                continue
            loop = rest.split(" | ", 1)[0]
            repo = cause_repo(match["key"])
            owned_text = rest.split(" | FIX: ", 1)[0]
            owned_text = CONTROLLER_OWNS_RE.sub("", owned_text).rstrip(" |")
            idle_note = f"UNOWNED-NOT-ACTING (landing-L{match['lease']} cause={match['key']}: {evidence})"
            owned_text = (
                owned_text.replace(match[0], idle_note)
                if match[0] in owned_text
                else f"{owned_text} | {idle_note}"
            )
            fix = (
                f"cause lane {lane_of[repo]} carries {repo} (stuck-owner:{repo})"
                if repo in lane_of
                else f"the controller is not acting on it: dispatch one cause lane for {repo} through "
                "onex_remote_lane.py run --cause <repo>:<check> (the stuck-owner actor does at the 2h bound)"
            )
            lines[n] = f"MISSING {owned_text} | FIX: {fix}"
            if level == "NOTE":
                promoted.append(loop)
        return ModelStuckOwnerResult(lines, promoted, found + self.gated(request))

    @staticmethod
    def gated(request: ModelStuckOwnerRequest) -> list[Finding]:
        return (
            HandlerDriverGate()
            .handle(
                ModelDriverGateRequest(
                    list(request.process_fix),
                    request.watcher,
                    request.ledger,
                    request.now,
                    request.target_checks,
                )
            )
            .found
        )


def tick_request(
    lines: list[str],
    watcher: Mapping[str, Any] | None,
    landing: Path,
    ledger: Path | None,
    now: datetime,
    state_dir: Path,
    process_fix: list[Finding],
    *,
    runner: Path,
    parent: str | None,
    read_only: bool,
    registry_root: str,
    scope: Callable[[], Iterable[str]],
) -> ModelStuckOwnerRequest:
    """The throughput tick's request: a read-only tick (--no-replace) dispatches nothing; the pull_request_target
    checks are read from the canonical clones under ``registry_root`` when it is set; an unreadable landing scope (``scope``
    raises) dispatches nothing."""
    dispatch = (
        None
        if read_only
        else runner_dispatch(runner, state_dir / "stuck-owner-briefs", parent)
    )
    try:
        repos: frozenset[str] | None = frozenset(scope())
    except (OSError, ValueError):
        repos = None
    return ModelStuckOwnerRequest(
        list(lines),
        watcher,
        landing,
        ledger,
        now,
        state_dir,
        dispatch,
        parent,
        process_fix=list(process_fix),
        scope=repos,
        target_checks=clone_target_checks(Path(registry_root))
        if registry_root
        else None,
    )


def fold(
    result: Mapping[str, Any], out: ModelStuckOwnerResult, report_type: Any
) -> dict[str, Any]:
    """The tick's result with the actor's rewritten lines and findings; a new MISSING or UNKNOWN (a promoted loop
    included) changes the status line."""
    rep = report_type(
        lines=list(out.lines),
        missing=[*(result.get("missing") or []), *out.promoted],
        unknown=list(result.get("unknown") or []),
        checked=list(result.get("checked") or []),
    )
    for level, loop, why, fix in out.found:
        if level in ("missing", "unknown"):
            (rep.miss if level == "missing" else rep.unk)(loop, why, fix)
        else:
            rep.lines.append(
                f"NOTE {why}" if level == "note" else f"SKIP {loop} | {why}"
            )
    if len(rep.missing) + len(rep.unknown) == len(result.get("missing") or []) + len(
        result.get("unknown") or []
    ):
        return {**result, "lines": rep.lines}
    return {
        **result,
        "lines": rep.lines,
        "missing": rep.missing,
        "unknown": rep.unknown,
        "status_line": rep.status("THROUGHPUT", "STALL"),
        "exit_code": 1,
    }


def driver_handoffs(
    ledger: Path | None, now: datetime, days: int = 7
) -> dict[str, list[tuple[str, str, str]]]:
    """repo#n -> [(driver lane, head, at)] for each pf-drive hand-off naming a head (TERMINAL or MSG to the controller)."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    if ledger is None:
        return out
    try:
        lines = _tail_lines(ledger, LEDGER_TAIL_BYTES)
    except OSError:
        return out
    for line in lines:
        row = _cells(line)
        if row is None or now - row[0] > timedelta(days=days):
            continue
        at, kind, cells = row
        lane = cells.get("lane") or cells.get("from") or ""
        handed = (kind == "TERMINAL" and cells.get("outcome") == "handed-off") or (
            kind == "MSG" and cells.get("to") == "landing-controller"
        )
        if not lane.startswith("pf-drive-") or not handed or not cells.get("head"):
            continue
        for ref in (cells.get("pr", "") + "," + cells.get("handed-off", "")).split(","):
            if "#" in ref:
                out.setdefault(_short(ref.strip()), []).append(
                    (lane, cells["head"], _stamp(at))
                )
    return out


def _target_names(text: str) -> set[str] | None:
    """The check names a workflow produces when its ``on`` names pull_request_target; None otherwise."""
    try:
        import yaml

        doc = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    on = doc.get("on", doc.get(True))
    events = on if isinstance(on, (list, dict)) else [on]
    if "pull_request_target" not in events:
        return None
    names = {str(doc.get("name") or "")}
    for job_id, job in (doc.get("jobs") or {}).items():
        names.add(
            str((job or {}).get("name") or job_id)
            if isinstance(job, dict)
            else str(job_id)
        )
    return {n for n in names if n and "${{" not in n}


def clone_target_checks(registry_root: Path) -> Callable[[str, str], set[str] | None]:
    """(repo, base) -> check names of the base's pull_request_target workflows, read from the canonical clone (git)."""

    def read(repo: str, base: str) -> set[str] | None:
        clone = registry_root / repo
        try:
            listing = subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "ls-tree",
                    "--name-only",
                    f"origin/{base}",
                    ".github/workflows/",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            names: set[str] = set()
            for path in listing.stdout.split():
                shown = subprocess.run(
                    ["git", "-C", str(clone), "show", f"origin/{base}:{path}"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                names |= _target_names(shown.stdout) or set()
            return names
        except (OSError, subprocess.SubprocessError):
            return None

    return read


def is_target_check(check: str, names: set[str]) -> bool:
    plain = re.sub(r" \(\d+/\d+\)$", "", check)
    return any(c in names for c in (check, plain, *plain.split(" / ")))


class HandlerDriverGate:
    """Turn a process-fix driver lane FIX into a NOTE when no driver can move the PR, or one already did."""

    def handle(self, request: ModelDriverGateRequest) -> ModelDriverGateResult:
        wanted = [
            n
            for n, f in enumerate(request.found)
            if f[0] == "missing"
            and f[1].startswith(PROCESS_FIX_LOOP)
            and f[3].startswith(DRIVER)
        ]
        if not wanted:
            return ModelDriverGateResult(list(request.found))
        found = list(request.found)
        handed = driver_handoffs(request.ledger, request.now)
        prs = (request.watcher or {}).get("prs") or {}
        repos = (request.watcher or {}).get("repos") or {}
        for n in wanted:
            _level, loop, why, _fix = found[n]
            key = loop.removeprefix(PROCESS_FIX_LOOP)
            rec = prs.get(key) or {}
            facts = rec.get("facts") or {}
            head = str(facts.get("head_sha") or "")
            prior = next(
                (
                    h
                    for h in handed.get(_short(key), [])
                    if head
                    and len(h[1]) >= 7
                    and (head.startswith(h[1]) or h[1].startswith(head))
                ),
                None,
            )
            if prior:
                found[n] = (
                    "note",
                    loop,
                    f"{loop} | {why} | head {head[:12]} already handed off by {prior[0]} at "
                    f"{prior[2]}; the landing controller holds it; no driver lane",
                    "",
                )
                continue
            red = sorted(str(c) for c in ((rec.get("ci") or {}).get("red") or []))
            repo = key.split("#")[0].rsplit("/", 1)[-1]
            base = str(
                facts.get("base")
                or (repos.get(repo) or {}).get("default_branch")
                or "main"
            )
            names = (
                request.target_checks(repo, base)
                if (red and request.target_checks)
                else None
            )
            if red and names and all(is_target_check(c, names) for c in red):
                found[n] = (
                    "note",
                    loop,
                    f"{loop} | {why} | red is base-inherited pull_request_target "
                    f"({', '.join(red)}): the workflow runs from {base}, so no head of this PR changes it; "
                    f"its cause is {repo}@{base}; no driver lane",
                    "",
                )
        return ModelDriverGateResult(found)
