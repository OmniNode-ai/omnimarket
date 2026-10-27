# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch saved resume patches to apply-and-land lanes and close out stale patches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    require_lane_model,
)

from ..models import (
    Disposition,
    ModelPatchApplyDispatchRequest,
    ModelPatchApplyDispatchResult,
    ModelPatchFinding,
)

LANE = "mech-patch-auto-review"
UMBRELLA = "OMN-17427"
APPLY_LANE_PREFIX = "mech-patch-apply"
REPORT_LIMIT = 6000
DISPATCH_COMMAND_ENV = "ONEX_PATCH_APPLY_DISPATCH_COMMAND"
RUNNER_GLOB = "skills/remote-lane/scripts/onex_remote_lane.py"
GIT_LOCATION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)
APPEND_COMMAND_ENV = "ONEX_LEDGER_COMMAND"
# A runner that refuses the brief (a ticket mismatch, no host) exits within seconds.
RUNNER_REFUSAL_SECONDS = 5
STAMP = "%Y-%m-%dT%H:%M:%SZ"

Dispatch = Callable[[Path, str, str, str], tuple[bool, str]]
AppendRow = Callable[[str], tuple[bool, str]]


def runner_command(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Use the host's explicit command or installed plugin root, without private paths."""
    values = env if env is not None else os.environ
    stated = values.get(DISPATCH_COMMAND_ENV)
    if stated:
        return tuple(shlex.split(stated))
    plugin = values.get("OMNI_PLUGIN_ROOT") or values.get("CLAUDE_PLUGIN_ROOT")
    if plugin:
        script = Path(plugin) / RUNNER_GLOB
        if script.is_file():
            return ("python3", str(script), "run")
    return ()


def append_ledger_row(ledger: Path, row: str) -> tuple[bool, str]:
    """Use the deployment's ledger writer, retaining its validation and bus transport."""
    command = shlex.split(os.environ.get(APPEND_COMMAND_ENV, "onex-ledger"))
    if not command:
        return False, "ledger command is empty"
    try:
        done = subprocess.run(
            [*command, str(ledger), "--append", row],
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ledger writer did not run ({type(exc).__name__})"
    return done.returncode == 0, f"exit {done.returncode}: {done.stderr.strip()[:300]}"


def runner_dispatch(command: tuple[str, ...], ledger: Path) -> Dispatch:
    """Start one remote-lane run for a brief; a runner that refuses it in seconds is a failure."""

    def dispatch(brief: Path, lane: str, ticket: str, repo: str) -> tuple[bool, str]:
        if not command:
            return (
                False,
                f"no remote-lane runner found ({DISPATCH_COMMAND_ENV} unset, no installed plugin root)",
            )
        log = brief.with_name(f"{lane}-dispatch.log")
        try:
            with log.open("ab") as output:
                runner = subprocess.Popen(
                    [
                        *command,
                        "--brief",
                        str(brief),
                        "--lane",
                        lane,
                        "--model",
                        require_lane_model(
                            requested_engine="claude", lane_kind="patch-apply"
                        ),
                        "--effort",
                        "high",
                        "--ticket",
                        ticket,
                        "--repo",
                        repo,
                        "--ledger",
                        str(ledger),
                    ],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                )
            try:
                code = runner.wait(timeout=RUNNER_REFUSAL_SECONDS)
            except subprocess.TimeoutExpired:
                return True, ""
        except OSError as exc:
            return False, f"runner did not start ({type(exc).__name__})"
        if code == 0:
            return True, ""
        tail = " ".join(log.read_text(errors="replace").split())[-200:]
        return False, f"runner exited {code}: {tail}"

    return dispatch


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime(STAMP)


def _family(lane: str) -> str:
    """``x-r2`` and ``x-r3`` are one lane's re-dispatches."""
    return re.sub(r"-r\d+$", "", lane)


def _cells(line: str) -> tuple[datetime, str, dict[str, str]] | None:
    parts = [part.strip() for part in line.split(" | ")]
    if len(parts) < 3:
        return None
    try:
        at = datetime.strptime(parts[0], STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    cells = dict(
        part.split("=", 1) for part in parts[2:] if re.match(r"^[\w-]+=", part)
    )
    return at, parts[1], cells


class _Ledger:
    """The two things the tick reads from the ledger: live CLAIMs and a lane's ticket."""

    def __init__(self, text: str) -> None:
        self.rows = [row for row in map(_cells, text.splitlines()) if row is not None]

    def ticket(self, lane: str) -> str:
        family = _family(lane)
        for _, _, cells in reversed(self.rows):
            found = re.fullmatch(r"OMN-\d+", cells.get("ticket", ""))
            if found and _family(cells.get("lane", "")) == family:
                return found.group(0)
        return ""

    def live_claim(self, lane: str, now: datetime, window: timedelta) -> str:
        family = _family(lane)
        closed = {
            (cells.get("lane", ""), cells.get("closes-CLAIM", ""))
            for _, kind, cells in self.rows
            if kind == "TERMINAL"
        }
        for at, kind, cells in reversed(self.rows):
            claimant = cells.get("lane", "")
            if kind != "CLAIM" or not now - window <= at <= now:
                continue
            if claimant.startswith(APPLY_LANE_PREFIX):
                if f"{APPLY_LANE_PREFIX}-{family[:24]}-" not in f"{claimant}":
                    continue
            elif _family(claimant) != family:
                continue
            if (claimant, _stamp(at)) not in closed:
                return f"{claimant} CLAIM {_stamp(at)}"
        return ""


def _git(
    clone: Path, argv: list[str], *, index: Path | None = None, stdin: str = ""
) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_ENV}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    done = subprocess.run(
        ["git", *argv],
        cwd=clone,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout.strip()


def _apply_facts(clone: Path, patch_text: str) -> tuple[str, bool, bool] | None:
    """(default ref, applies, already_on_base) read with a scratch index, or None if unreadable."""
    code, ref = _git(clone, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if code != 0 or not ref:
        return None
    with tempfile.TemporaryDirectory(prefix="patch-apply-dispatch-") as scratch:
        index = Path(scratch) / "index"
        if _git(clone, ["read-tree", ref], index=index)[0] != 0:
            return None
        applies = (
            _git(
                clone,
                ["apply", "--cached", "--check", "-"],
                index=index,
                stdin=patch_text,
            )[0]
            == 0
        )
        present = (
            _git(
                clone,
                ["apply", "--cached", "--reverse", "--check", "-"],
                index=index,
                stdin=patch_text,
            )[0]
            == 0
        )
    return ref, applies, present


def _repo_of(name: str, repos: tuple[str, ...]) -> str:
    """The longest registry clone name the patch file name starts with (``-`` and ``_`` alike)."""
    flat = name.lower().replace("_", "-")
    matches = [
        repo for repo in repos if flat.startswith(repo.lower().replace("_", "-") + "-")
    ]
    return max(matches, key=len, default="")


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


class HandlerPatchApplyDispatch:
    def __init__(
        self, *, dispatch: Dispatch | None = None, append_row: AppendRow | None = None
    ) -> None:
        self._stated_dispatch = dispatch
        self._stated_append = append_row
        self._dispatch: Dispatch
        self._append_row: AppendRow

    def handle(
        self, request: ModelPatchApplyDispatchRequest
    ) -> ModelPatchApplyDispatchResult:
        errors: list[str] = []
        self._dispatch = self._stated_dispatch or runner_dispatch(
            runner_command(), request.ledger_path
        )
        self._append_row = self._stated_append or (
            lambda row: append_ledger_row(request.ledger_path, row)
        )
        root = (
            request.patch_root or request.registry_root / ".onex_state/hook-preflight"
        )
        reports = (
            request.reports_dir or request.registry_root / ".claude_scratch/reports"
        )
        ledger: _Ledger | None = None
        try:
            ledger = _Ledger(
                request.ledger_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            errors.append(
                f"ledger unreadable ({type(exc).__name__}): live CLAIMs cannot be checked"
            )
        state_file = request.state_dir / "dispatched.json"
        try:
            sent: dict[str, dict[str, object]] = json.loads(state_file.read_text())
        except (OSError, ValueError):
            sent = {}
        findings: list[ModelPatchFinding] = []
        dispatched = 0
        patches = sorted(root.glob("*/*.patch")) if root.is_dir() else []
        for patch in patches:
            finding = self._one(
                request, patch, ledger, reports, sent, dispatched, errors
            )
            if finding.disposition in {"dispatched"} or (
                finding.disposition == "dry-run"
                and finding.detail.startswith("would dispatch")
            ):
                dispatched += 1
            findings.append(finding)
        if request.apply:
            try:
                request.state_dir.mkdir(parents=True, exist_ok=True)
                state_file.write_text(json.dumps(sent, indent=1, sort_keys=True) + "\n")
            except OSError as exc:
                errors.append(f"dispatch state not written ({type(exc).__name__})")
        counts = {
            kind: sum(1 for f in findings if f.disposition == kind)
            for kind in {f.disposition for f in findings}
        }
        failed = counts.get("dispatch-failed", 0) + counts.get("unroutable", 0)
        closed = counts.get("closed-applied", 0) + counts.get("closed-expired", 0)
        held = (
            counts.get("held-owned", 0)
            + counts.get("held-recent", 0)
            + counts.get("deferred", 0)
        )
        summary = (
            f"patch-apply-dispatch: patches={len(findings)} "
            f"dispatched={counts.get('dispatched', 0)} "
            f"closed={closed} held={held} failed={failed}"
            + (" (dry-run)" if not request.apply else "")
        )
        result = ModelPatchApplyDispatchResult(
            correlation_id=request.correlation_id,
            findings=tuple(findings),
            dispatched=counts.get("dispatched", 0),
            closed=closed,
            held=held,
            failed=failed,
            summary=summary,
            exit_code=1 if failed or errors else 0,
            errors=tuple(errors),
        )
        self._heartbeat(request, result)
        return result

    def _one(
        self,
        request: ModelPatchApplyDispatchRequest,
        patch: Path,
        ledger: _Ledger | None,
        reports: Path,
        sent: dict[str, dict[str, object]],
        dispatched: int,
        errors: list[str],
    ) -> ModelPatchFinding:
        now = request.now
        lane = patch.parent.name
        raw = patch.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        age = max(
            0.0,
            (now - datetime.fromtimestamp(patch.stat().st_mtime, UTC)).total_seconds()
            / 3600,
        )
        repo = _repo_of(patch.name, request.repos)
        ticket = (ledger.ticket(lane) if ledger else "") or self._filename_ticket(
            patch.name
        )
        report = (
            max(reports.glob(f"{lane}-*.md"), default=None)
            if reports.is_dir()
            else None
        )
        apply_lane = re.sub(
            r"[^a-z0-9-]", "-", f"{APPLY_LANE_PREFIX}-{lane[:24].lower()}-{sha[:6]}"
        )

        def finding(
            disposition: Disposition, detail: str = "", **extra: str
        ) -> ModelPatchFinding:
            return ModelPatchFinding(
                patch=str(patch),
                lane=lane,
                repo=repo,
                ticket=ticket,
                report=str(report or ""),
                sha256=sha,
                age_hours=round(age, 2),
                disposition=disposition,
                apply_lane=extra.get("apply_lane", ""),
                detail=detail,
            )

        patch_text = raw.decode("utf-8", errors="replace")
        facts = None
        if repo and patch_text.strip():
            facts = _apply_facts(request.registry_root / repo, patch_text)
        if not patch_text.strip():
            close: tuple[Disposition, str] | None = ("closed-applied", "empty patch")
        elif facts is not None and facts[2]:
            close = ("closed-applied", f"already on {facts[0]}")
        elif age > request.max_age_hours:
            close = (
                "closed-expired",
                f"{age:.1f} h old, limit {request.max_age_hours:g} h",
            )
        else:
            close = None
        if close is not None:
            if not request.apply:
                return finding("dry-run", f"would be {close[0]}: {close[1]}")
            problem = self._close_out(request, patch, sha, close)
            if problem:
                errors.append(problem)
            return finding(close[0], close[1])
        if not repo:
            return finding(
                "unroutable", f"{patch.name} names none of the registry clones"
            )
        if ledger is None:
            return finding("deferred", "ledger unreadable")
        holder = ledger.live_claim(
            lane, now, timedelta(minutes=request.claim_window_minutes)
        )
        if holder:
            return finding("held-owned", holder, apply_lane=apply_lane)
        previous = sent.get(sha)
        if previous:
            at = datetime.strptime(str(previous["at"]), STAMP).replace(tzinfo=UTC)
            if now - at < timedelta(hours=request.redispatch_hours):
                return finding(
                    "held-recent", f"dispatched {previous['at']} as {previous['lane']}"
                )
        if dispatched >= request.max_dispatch:
            return finding("deferred", f"per-tick cap {request.max_dispatch}")
        if not request.apply:
            return finding(
                "dry-run", f"would dispatch {apply_lane}", apply_lane=apply_lane
            )
        brief = request.state_dir / "briefs" / f"{apply_lane}-brief.md"
        try:
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.write_text(
                self._brief(patch, patch_text, report, repo, ticket, lane, facts),
                encoding="utf-8",
            )
            ok, detail = self._dispatch(brief, apply_lane, ticket or UMBRELLA, repo)
        except OSError as exc:
            ok, detail = False, f"brief not written ({type(exc).__name__})"
        if ok:
            sent[sha] = {"at": _stamp(now), "lane": apply_lane, "patch": str(patch)}
        self._status(request, patch, lane, repo, ticket, apply_lane, ok, detail, errors)
        return finding(
            "dispatched" if ok else "dispatch-failed", detail, apply_lane=apply_lane
        )

    @staticmethod
    def _filename_ticket(name: str) -> str:
        found = re.search(r"omn-(\d+)", name.lower())
        return f"OMN-{found.group(1)}" if found else ""

    @staticmethod
    def _brief(
        patch: Path,
        patch_text: str,
        report: Path | None,
        repo: str,
        ticket: str,
        lane: str,
        facts: tuple[str, bool, bool] | None,
    ) -> str:
        base = facts[0] if facts else f"origin/<default branch of {repo}>"
        state = (
            "applies cleanly"
            if facts and facts[1]
            else "does not apply cleanly to that base; resolve the conflicts against it"
            if facts
            else "was not checked (the registry clone's default branch was unreadable)"
        )
        text = report.read_text(encoding="utf-8", errors="replace") if report else ""
        if len(text) > REPORT_LIMIT:
            text = text[:REPORT_LIMIT] + "\n[report truncated]\n"
        fence = _fence(patch_text + text)
        return (
            "Resume patch: apply and land\n\n"
            f"Repo: {repo}\nTicket: {ticket or UMBRELLA}\nLeft by lane: {lane}\n"
            f"Patch file (on the launching host): {patch}\n"
            f"Report file (on the launching host): {report or 'none found'}\n"
            f"The patch against {base} {state}.\n\n"
            f"Lane {lane} stopped without committing; the patch below is its saved change and the "
            "report below is what it said. Apply the change in a fresh worktree of the repository "
            "built from the refreshed default branch, finish whatever the report names as undone, "
            "run the focused tests and the commit hook preflight, commit, open one pull request "
            f"titled with {ticket or UMBRELLA} only, and hand it off with /omni:pr-handoff "
            "(needs=land). Read any PR with pr_state_local.py before touching it; a live CLAIM or "
            "a landing-controller lease on it means you leave it and list it as owned. If the "
            "patch is already on the default branch, or the report shows the work moved elsewhere, "
            "stop with LANE_RESULT outcome=skipped-owned and say why. The patch file is not "
            "yours to edit or move; the tick that dispatched you closes it out once its change is "
            "on the default branch or it is 48 hours old.\n\n"
            f"Report:\n{fence}\n{text}\n{fence}\n\nPatch:\n{fence}diff\n{patch_text}\n{fence}\n"
        )

    def _status(
        self,
        request: ModelPatchApplyDispatchRequest,
        patch: Path,
        lane: str,
        repo: str,
        ticket: str,
        apply_lane: str,
        ok: bool,
        detail: str,
        errors: list[str],
    ) -> None:
        row = (
            f"{_stamp(request.now)} | STATUS | lane={LANE} | ticket={UMBRELLA} | "
            f"actor=launchd:patch-apply-dispatch | patch={patch} | from_lane={lane} | "
            f"repo={repo} | patch_ticket={ticket or '-'} | apply_lane={apply_lane} | "
            f"dispatched={'yes' if ok else 'no'} | "
            + (
                f"resume patch of {lane} dispatched as {apply_lane} through the remote-lane runner"
                if ok
                else f"resume patch of {lane} not dispatched: {' '.join(detail.split())[:200]}"
            )
        )
        appended, why = self._append_row(row)
        if not appended:
            errors.append(f"status row not appended: {why}")

    def _close_out(
        self,
        request: ModelPatchApplyDispatchRequest,
        patch: Path,
        sha: str,
        close: tuple[Disposition, str],
    ) -> str:
        reason = "applied" if close[0] == "closed-applied" else "expired"
        target = patch.parent / "closed"
        try:
            target.mkdir(exist_ok=True)
            record = target / f"{patch.name}.closed.json"
            record.write_text(
                json.dumps(
                    {
                        "reason": reason,
                        "detail": close[1],
                        "closed_utc": _stamp(request.now),
                        "sha256": sha,
                    },
                    indent=1,
                )
                + "\n"
            )
            os.replace(patch, target / patch.name)
        except OSError as exc:
            return f"{patch} not closed out ({type(exc).__name__})"
        row = (
            f"{_stamp(request.now)} | STATUS | lane={LANE} | ticket={UMBRELLA} | "
            f"actor=launchd:patch-apply-dispatch | patch={patch} | from_lane={patch.parent.name} | "
            f"closed={reason} | resume patch closed out ({close[1]})"
        )
        appended, why = self._append_row(row)
        return "" if appended else f"close-out row not appended: {why}"

    @staticmethod
    def _heartbeat(
        request: ModelPatchApplyDispatchRequest, result: ModelPatchApplyDispatchResult
    ) -> None:
        if not request.apply:
            return
        try:
            request.state_dir.mkdir(parents=True, exist_ok=True)
            (request.state_dir / "last-run.json").write_text(
                json.dumps(
                    {
                        "ran_utc": _stamp(request.now),
                        "patches": len(result.findings),
                        "dispatched": result.dispatched,
                        "closed": result.closed,
                        "held": result.held,
                        "failed": result.failed,
                        "errors": list(result.errors),
                        "findings": [
                            {
                                "patch": f.patch,
                                "disposition": f.disposition,
                                "detail": f.detail,
                            }
                            for f in result.findings
                        ],
                    },
                    indent=1,
                )
                + "\n"
            )
        except OSError:
            pass
