# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of ci-watch's CI verdict (OMN-20686).

The retired ci_state.py mixed these decisions with GitHub reads, scratch-file writes and the watcher
state loader. Here the caller reads those and passes the facts; the handler returns what the script
printed, which scratch files it wrote and how it exited, behaviour-preserving:

* ``watcher``: the PR watcher's record answers with zero GitHub requests, or the reason it cannot and
  the caller reads GitHub.
* ``live``: keys GREEN / RED / PENDING on the required contexts from every check-run copy at the head.
  A GREEN that GitHub calls BLOCKED, DIRTY or BEHIND asks the caller for the head's check suites once
  (``read-check-suites``) to name a stuck umbrella suite (class A1).
* ``classify-log``: names the evidence line a gate error says is missing (classes C10 and C13).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from omnimarket.nodes.node_ci_state_verdict_compute.models.model_ci_state_verdict import (
    ModelCiStateCheckRun,
    ModelCiStateCheckSuite,
    ModelCiStateVerdictRequest,
    ModelCiStateVerdictResult,
    ModelCiStateWatcher,
)

PASSING = {"success", "neutral", "skipped"}
# ci-watch class A1: a GitHub App check suite stuck QUEUED past this long, naming no required context.
STUCK_QUEUED_AFTER = dt.timedelta(hours=1)
VERDICT_EXIT = {"GREEN": 0, "RED": 1, "PENDING": 2}
READ_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_iso(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, READ_AT_FORMAT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def stuck_check_suites(
    suites: list[ModelCiStateCheckSuite],
    runs: list[ModelCiStateCheckRun],
    required: dict[str, str],
    now: dt.datetime,
) -> list[ModelCiStateCheckSuite]:
    """Class A1: a suite still QUEUED past an hour that owns no required check-run."""
    required_suite_ids = {c.suite for c in runs if c.name in required}
    found = []
    for suite in suites:
        if suite.status != "queued" or suite.id in required_suite_ids:
            continue
        created = parse_iso(suite.created_at)
        if created is not None and now - created >= STUCK_QUEUED_AFTER:
            found.append(suite)
    return found


def run_and_job(url: str | None) -> tuple[str, str]:
    m = re.search(r"/runs/(\d+)(?:/job/(\d+))?", url or "")
    return (m.group(1), m.group(2) or "-") if m else ("-", "-")


def classify_evidence_log(log: str) -> dict[str, str] | None:
    """Classify only an explicit gate error about a missing body line, never a gate name alone."""
    for line in log.splitlines():
        if not re.search(
            r"(?:RECEIPT|DEPLOY) GATE FAILED:|Contract Compliance evidence resolution failed:",
            line,
            re.I,
        ):
            continue
        if not re.search(r"missing (?:an? |required )?", line, re.I):
            continue
        missing = line.lower().split("missing", 1)[1]
        if "evidence-ticket" in missing:
            return {
                "class": "C13",
                "missing_line": "Evidence-Ticket",
                "fix": "Owner uses the pr-land paired body helper with the verified ticket and companion; "
                "preserve the existing evidence-source stamp verbatim. Hand the corrected body to "
                "the landing controller; rerun alone cannot repair missing evidence. Never delete "
                "the stamp or relax the guard.",
            }
        if "evidence-source" in missing:
            return {
                "class": "C10",
                "missing_line": "Evidence-Source",
                "fix": "Check draft residue (D5) first. For a ready PR whose companion exists, the owner "
                "restores both lines through the pr-land paired body helper, naming the verified "
                "companion and ticket; preserve surviving evidence. Hand off to the landing controller.",
            }
    return None


def _tsv(rows: list[list[Any]]) -> str:
    return "".join("\t".join(str(cell) for cell in row) + "\n" for row in rows)


class HandlerCiStateVerdict:
    """ci-watch's verdict decisions: ``handle(request) -> result``."""

    def handle(self, request: ModelCiStateVerdictRequest) -> ModelCiStateVerdictResult:
        if request.operation == "classify-log":
            return self._classify(request)
        if request.operation == "watcher":
            return self._watcher(request)
        return self._live(request)

    def _classify(
        self, request: ModelCiStateVerdictRequest
    ) -> ModelCiStateVerdictResult:
        found = classify_evidence_log(request.log_text)
        if found is None:
            return ModelCiStateVerdictResult(
                action="answer",
                exit_code=2,
                stdout_lines=[
                    "CLASS UNCLASSIFIED: use the remaining ci-watch class table"
                ],
            )
        return ModelCiStateVerdictResult(
            action="answer",
            exit_code=1,
            stdout_lines=[
                f"CLASS {found['class']} missing={found['missing_line']}: {found['fix']}"
            ],
        )

    def _watcher(
        self, request: ModelCiStateVerdictRequest
    ) -> ModelCiStateVerdictResult:
        why = self._watcher_unusable(request.watcher, request.watcher_unavailable)
        watcher = request.watcher
        if why is not None or watcher is None or watcher.ci is None:
            return ModelCiStateVerdictResult(action="read-github", fallback_reason=why)
        facts, ci = watcher.facts, watcher.ci
        sha, verdict = str(facts.head_sha), str(ci.verdict)
        full, number = request.full, request.number
        pr_json = {
            "headRefOid": sha,
            "baseRefName": facts.base,
            "isDraft": False,
            "state": "OPEN",
            "labels": [{"name": x} for x in facts.labels],
            "autoMergeRequest": {"enabled": True} if facts.armed else None,
            "source": "pr-watcher-state",
        }
        armed = "armed" if facts.armed else "null"
        out = [
            f"PR {full}#{number} state={facts.state} draft={facts.draft} base={facts.base} head={sha}",
            f"   mergeable=unread mergeStateStatus=unread auto={armed} labels={list(facts.labels)}",
            f"   source=watcher; CI read by the watcher at {ci.read_at}; {request.reads_line}",
        ]
        for run in ci.runs:
            name, status, conclusion, when = (str(x) for x in run)
            if name in ci.red or name in ci.pending:
                out.append(f"   {name[:60]:<60} {status}/{conclusion or '-'} {when}")
        out.append(
            f"   check-run names read by the watcher: {len(ci.runs)} (newest copy of each); "
            f"red={','.join(ci.red) or '-'} pending={','.join(ci.pending) or '-'}"
        )
        if verdict == "RED":
            out.append(
                "   NEXT: read the failing log of the red names above only, with the run and job ids from "
                "`ci_state.sh --live` (the exact-head read, which also applies the class table)"
            )
        elif verdict == "GREEN":
            out.append(
                "   NEXT: before a merge or an arm, run `ci_state.sh --live` once (the exact-head check: "
                "the watcher does not see an unreported required context or mergeStateStatus)"
            )
        else:
            out.append(
                "   NEXT: hand off (`/omni:pr-handoff`) and exit; the watcher reports the verdict, do not poll"
            )
        out.append(
            f"VERDICT {verdict} {sha} required=unread names={len(ci.runs)} copies={ci.total} "
            f"mergeStateStatus=unread read_at={ci.read_at} source=watcher"
        )
        return ModelCiStateVerdictResult(
            action="answer",
            exit_code=VERDICT_EXIT[verdict],
            stdout_lines=out,
            files={"pr.json": json.dumps(pr_json, indent=1)},
            verdict=verdict,
        )

    @staticmethod
    def _watcher_unusable(
        watcher: ModelCiStateWatcher | None, unavailable: str | None
    ) -> str | None:
        """Why the watcher state cannot answer, or None when it can."""
        if unavailable is not None:
            return unavailable
        if watcher is None:
            return "the caller passed no watcher record"
        facts, ci = watcher.facts, watcher.ci
        if facts.state != "OPEN":
            return f"watcher state has the PR as {facts.state}"
        if facts.draft:
            return "the watcher never reads the CI of a draft PR"
        if ci is None or ci.sha != facts.head_sha:
            return f"the watcher has not read CI at head {str(facts.head_sha)[:12]} yet (ci=UNREAD)"
        if ci.verdict not in VERDICT_EXIT:
            return f"the watcher read no check-runs at this head (ci={ci.verdict})"
        return None

    def _live(self, request: ModelCiStateVerdictRequest) -> ModelCiStateVerdictResult:
        now_dt = dt.datetime.fromisoformat(request.now)
        read_at = now_dt.strftime(READ_AT_FORMAT)
        pr, required, runs = request.pr, request.required, request.check_runs
        sha, base = pr["headRefOid"], pr["baseRefName"]
        mss = pr.get("mergeStateStatus")
        latest_status = {s.context: s for s in request.statuses}
        wf_runs = {
            r[0]: {"id": r[0], "name": r[1], "status": r[3], "created_at": r[6]}
            for r in request.workflow_runs
        }
        out: list[str] = []
        if request.live_flag:
            out.append(
                "   --live: exact-head check, reading GitHub (not the watcher state)"
            )
        else:
            out.append(
                f"   watcher state not used: {request.watcher_not_used_reason}; reading GitHub"
            )
        out.extend(request.reader_lines)
        out.append(
            f"PR {request.full}#{request.number} state={pr['state']} draft={pr['isDraft']} base={base} head={sha}"
        )
        out.append(
            f"   mergeable={pr['mergeable']} mergeStateStatus={mss} "
            f"auto={'armed' if pr.get('autoMergeRequest') else 'null'} labels={[x['name'] for x in pr.get('labels', [])]}"
        )
        files = {
            "pr.json": json.dumps(pr, indent=1),
            "required.txt": "".join(f"{k}\t{v}\n" for k, v in required.items()),
            "cr.tsv": _tsv(
                [
                    [
                        getattr(c, k) or "-"
                        for k in (
                            "id",
                            "name",
                            "status",
                            "conclusion",
                            "started_at",
                            "completed_at",
                            "details_url",
                            "suite",
                        )
                    ]
                    for c in runs
                ]
            ),
            "statuses.tsv": "".join(
                f"{s.context}\t{s.state}\t{s.created_at}\n"
                for s in latest_status.values()
            ),
            "runs.tsv": _tsv([list(r) for r in request.workflow_runs]),
        }
        total = request.check_runs_total
        out.append(
            f"   check-runs read={len(runs)} total_count={total} (paginated, equal); "
            f"statuses={len(latest_status)}; workflow runs={len(request.workflow_runs)}"
        )
        if total == 0 and not latest_status:
            return ModelCiStateVerdictResult(
                action="answer",
                exit_code=3,
                stdout_lines=out,
                stderr_lines=[
                    "ERROR: zero check-runs and zero statuses at the head: prove the query on a head with rows before reporting anything"
                ],
                files=files,
            )

        reds = [
            c
            for c in runs
            if c.status == "completed" and (c.conclusion or "-") not in PASSING
        ]
        files["reds.tsv"] = "".join(
            f"{'REQUIRED' if c.name in required else 'optional'}\t{c.name}\t{c.conclusion}\t"
            f"{run_and_job(c.details_url)[0]}\t{run_and_job(c.details_url)[1]}\n"
            for c in sorted(reds, key=lambda c: c.name)
        )

        def superseded(copy: ModelCiStateCheckRun) -> dict[str, str] | None:
            """A newer, unfinished run of the same workflow at this head (P5): the red is a wait."""
            rid = run_and_job(copy.details_url)[0]
            own = wf_runs.get(rid)
            if own is None:
                return None
            newer = [
                r
                for r in wf_runs.values()
                if r["name"] == own["name"]
                and r["id"] != rid
                and r["created_at"] > own["created_at"]
                and r["status"] != "completed"
            ]
            return max(newer, key=lambda r: r["created_at"]) if newer else None

        by_name: dict[str, list[ModelCiStateCheckRun]] = {}
        for c in runs:
            by_name.setdefault(c.name, []).append(c)
        keyed = required or {
            **{c.name: "no-required:check-run" for c in runs},
            **{k: "no-required:status" for k in latest_status if k not in by_name},
        }
        if not required:
            out.append(
                "   WARNING: no required contexts found on this base; keying the verdict on EVERY check-run AND "
                "every legacy commit status (conservative)"
            )
        any_red = any_pending = False
        stale_total = 0
        out.append(f"   {'context':<60} source              copies  newest-copy state")
        for ctx, src in keyed.items():
            # newest copy: latest started_at (else completed_at), then id; a copy with neither
            # timestamp sorts NEWEST, never oldest (fail closed).
            copies = sorted(
                by_name.get(ctx, []),
                key=lambda c: (c.started_at or c.completed_at or "9999", c.id),
            )
            st = latest_status.get(ctx)
            newest = copies[-1] if copies else None
            stale = [
                c
                for c in copies[:-1]
                if c.status == "completed" and (c.conclusion or "-") not in PASSING
            ]
            stale_total += len(stale)
            note = (
                f" (+{len(stale)} older non-green copies: C5 candidates)"
                if stale
                else ""
            )
            if newest is None and st is None:
                state, any_pending = "MISSING (not reported at this head)", True
            elif newest is not None and newest.status != "completed":
                state, any_pending = f"PENDING ({newest.status})", True
            elif (
                newest is not None
                and (newest.conclusion or "-") not in PASSING
                and superseded(newest)
            ):
                nr = superseded(newest)
                assert nr is not None
                state, any_pending = (
                    (
                        f"PENDING (red copy's run {run_and_job(newest.details_url)[0]} is superseded by a newer "
                        f"'{nr['name']}' run {nr['id']} at this head, {nr['status']}: class P5)"
                    ),
                    True,
                )
            elif newest is not None and (newest.conclusion or "-") not in PASSING:
                state, any_red = f"RED {newest.conclusion}", True
            elif st is not None and st.state in ("failure", "error"):
                state, any_red = f"RED status:{st.state}", True
            elif st is not None and st.state == "pending":
                state, any_pending = "PENDING (status)", True
            else:
                state = "green"
            out.append(f"   {ctx[:60]:<60} {src:<19} {len(copies):>6}  {state}{note}")
        verdict = "RED" if any_red else ("PENDING" if any_pending else "GREEN")
        n_opt_red = len([c for c in reds if c.name not in required])
        out.append(
            f"   non-green copies: {len(reds)} ({n_opt_red} on optional contexts); see reds.tsv"
        )
        if verdict == "GREEN" and mss in ("BLOCKED", "DIRTY", "BEHIND"):
            if not request.check_suites_read:
                return ModelCiStateVerdictResult(
                    action="read-check-suites", verdict=verdict
                )
            out.append(
                f"   MISMATCH: required contexts read green but GitHub says {mss}: read C5 (stale copies), R4/R5 (branch state) and reviews before reporting"
            )
            for s in stuck_check_suites(request.check_suites, runs, required, now_dt):
                created = parse_iso(s.created_at)
                assert created is not None
                out.append(
                    f"   STUCK-CHECK-SUITE app={s.app or '?'} suite={s.id} status=queued "
                    f"age={now_dt - created} names no required context (class A1): never rerun (nothing is "
                    f"queued to rerun), never wait it out; take a fresh head (gh pr update-branch)"
                )
        if verdict == "RED" and mss in ("CLEAN", "UNSTABLE", "HAS_HOOKS"):
            out.append(
                f"   MISMATCH: a required context reads red but GitHub says {mss}: re-read; GitHub may not have recomputed yet"
            )
        if stale_total:
            out.append(
                f"   {stale_total} older non-green copies of required contexts sit beside newer copies (C5 candidates)"
            )
        out.append(
            f"VERDICT {verdict} {sha} required={len(required)} checkruns={len(runs)}/{total} mergeStateStatus={mss} read_at={read_at}"
        )
        return ModelCiStateVerdictResult(
            action="answer",
            exit_code=VERDICT_EXIT[verdict],
            stdout_lines=out,
            files=files,
            verdict=verdict,
        )
