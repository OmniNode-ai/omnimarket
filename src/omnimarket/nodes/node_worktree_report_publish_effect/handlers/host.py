# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host adapter for the report publisher's existing scrub, git and ledger seams."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..models import ModelWorktreeReportPublishRequest
from .paths import portabilize, scrub_refusal


class StepError(Exception):
    """A failed publication stage, kept visible to the scheduled caller."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Shell:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = dict(os.environ if environ is None else environ)

    def run(self, argv: Sequence[str], *, timeout: float = 600) -> Result:
        # The report checkout must not inherit the scheduler's git worktree.
        env = dict(self.environ)
        if argv[0] == "git":
            env = {k: v for k, v in env.items() if not k.startswith("GIT_")}
        try:
            proc = subprocess.run(
                list(argv),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return Result(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return Result(124, stderr=f"timed out after {timeout}s: {argv[0]}")
        except OSError as exc:
            return Result(127, stderr=f"{type(exc).__name__}: {exc}")


def clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class ReportPublishHost:
    """Push the report, open its PR over the bus, then append one landing MSG.

    No operation here reads checks, waits for CI, merges, arms, or deletes a
    remote branch. A failed commit or push keeps the checkout and its contents.
    """

    def __init__(self, shell: Shell | None = None) -> None:
        self.shell = shell if shell is not None else Shell()

    def git(self, checkout: Path, *args: str) -> str:
        result = self.shell.run(["git", "-C", str(checkout), *args], timeout=900)
        if result.returncode:
            raise StepError(
                "publish",
                f"git {args[0]} exit {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:300]}",
            )
        return result.stdout.strip()

    def commit_and_push(
        self, request: ModelWorktreeReportPublishRequest
    ) -> tuple[str, str, str]:
        # The clone refresher owns remote updates; use its declared landing ref.
        origin_head = self.git(
            request.checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
        )
        if not origin_head.startswith("origin/"):
            raise StepError("publish", "origin/HEAD does not name a landing branch")
        base = origin_head.removeprefix("origin/")
        revision = self.git(request.checkout, "rev-parse", f"origin/{base}")
        worktree = (
            request.worktrees_root / request.ticket / request.repository.split("/")[1]
        )
        if worktree.exists():
            raise StepError("publish", f"report worktree already exists: {worktree}")
        branch = f"omn-{request.ticket.removeprefix('OMN-')}-worktree-report-{request.date}-{request.correlation_id.hex[:8]}"
        self.git(
            request.checkout, "worktree", "add", "--detach", str(worktree), revision
        )
        try:
            target = worktree / "beta" / "tracking"
            target.mkdir(parents=True, exist_ok=True)
            reports: list[Path] = []
            for source in request.reports:
                path = target / source.name
                path.write_text(
                    portabilize(
                        source.read_text(encoding="utf-8"), request.roots, request.home
                    ),
                    encoding="utf-8",
                )
                reports.append(path)
            result = self.shell.run(
                [
                    "onex-shared-doc-scrub",
                    "--names-file",
                    str(request.names_file),
                    "--write",
                    *(str(path) for path in reports),
                ]
            )
            if result.returncode:
                output = result.stdout + result.stderr
                refused = [p.name for p in reports if f"{p}:" in output]
                named = f"refused {', '.join(refused)}: " if refused else ""
                raise StepError(
                    "scrub",
                    f"onex-shared-doc-scrub exit {result.returncode}: "
                    f"{named}{scrub_refusal(output)[:600]}",
                )
            rel = [f"beta/tracking/{path.name}" for path in reports]
            self.git(worktree, "add", "--", *rel)
            verb = "executed" if request.executed else "report (no removal)"
            self.git(
                worktree,
                "commit",
                "-m",
                f"docs({request.ticket}): worktree prune {verb} {request.date}",
                "--",
                *rel,
            )
            sha = self.git(worktree, "rev-parse", "HEAD")
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise StepError("publish", "git returned no full report head")
            self.git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
        except (StepError, OSError, ValueError) as exc:
            # No cleanup after failure: a detached, unpushed commit must survive.
            if isinstance(exc, StepError):
                raise StepError(
                    exc.step, f"{exc.detail}; worktree kept at {worktree}"
                ) from exc
            raise StepError("publish", f"{exc}; worktree kept at {worktree}") from exc
        # A successful push retains the exact commit remotely before local cleanup.
        self.git(request.checkout, "worktree", "remove", str(worktree))
        return sha, branch, base

    def open_pull_request(
        self,
        request: ModelWorktreeReportPublishRequest,
        head: str,
        branch: str,
        base: str,
    ) -> int:
        body = (
            f"Unattended worktree prune report.\n\n{request.summary}\n\n"
            f"{request.lab}\n\nTicket: {request.ticket}\nCloses {request.ticket}\n"
        )
        body_file = (
            request.state_root
            / "worktree-report-publish"
            / str(request.correlation_id)
            / "pr-body.md"
        )
        body_file.parent.mkdir(parents=True, exist_ok=True)
        body_file.write_text(body, encoding="utf-8")
        result = self.shell.run(
            [
                "onex",
                "scm",
                "pr-create",
                request.repository,
                "--head",
                branch,
                "--base",
                base,
                "--head-sha",
                head,
                "--title",
                f"docs({request.ticket}): worktree prune {request.date}",
                "--body-file",
                str(body_file),
                "--idempotency-key",
                str(request.correlation_id),
            ],
            timeout=900,
        )
        if result.returncode:
            raise StepError(
                "publish",
                f"pr-create exit {result.returncode}: {(result.stderr or result.stdout).strip()[:600]}",
            )
        try:
            answer = json.loads(result.stdout)
            payload = answer["result"]
            number = payload["pr_number"]
            if (
                answer.get("outcome") != "completed"
                or type(number) is not int
                or number <= 0
            ):
                raise ValueError("no completed PR number")
            if payload.get("head_sha") != head:
                raise ValueError("pr-create returned a different or absent head")
        except (ValueError, KeyError, TypeError) as exc:
            raise StepError(
                "publish", f"pr-create returned an invalid receipt: {exc}"
            ) from exc
        return number

    def handoff(
        self, request: ModelWorktreeReportPublishRequest, number: int, head: str
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (
            f"{now} | MSG | from={request.lane} | id=worktree-report-{request.correlation_id} | "
            f"to=landing-controller | ticket={request.ticket} | pr={request.repository}#{number} | "
            f"head={head} | needs=land | worktree report ready for landing"
        )
        result = self.shell.run(
            ["onex-ledger", str(request.ledger), "--append", row], timeout=180
        )
        if result.returncode:
            raise StepError(
                "publish",
                f"landing handoff refused (exit {result.returncode}): {(result.stderr or result.stdout).strip()[:600]}",
            )

    def report_failure(
        self, request: ModelWorktreeReportPublishRequest, error: StepError
    ) -> tuple[bool, bool]:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (
            f"{now} | FRICTION | lane={request.lane} | existing={request.ticket} | "
            "cost=~15 lane-minutes of operator attention; the morning's prune outcome is at risk | "
            f"unattended worktree prune step '{clean(error.step)}' failed: {clean(error.detail)[:900]}"
        )
        appended = self.shell.run(
            ["onex-ledger", str(request.ledger), "--append", row], timeout=180
        )
        page = self.shell.run(
            [
                "bash",
                "-c",
                '. "$1" && alert_channel_send "$2" "$3"',
                "_",
                str(request.alert_script),
                "morning-worktree-prune",
                f"Morning worktree prune {request.date} FAILED at '{error.step}': {error.detail[:600]}. Ledger lane={request.lane}.",
            ],
            timeout=120,
        )
        return appended.returncode == 0, page.returncode == 0
