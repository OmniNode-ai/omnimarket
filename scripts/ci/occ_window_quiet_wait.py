# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wait for a quiet OCC batch window before the receipt runner pushes (OMN-20042).

Why this exists
---------------
``occ-receipt-runner.yml`` pushes an ``evidence(OMN-16859)`` commit onto the
product PR's open companion branch. When that branch is the repository's batch
window (``auto/window-*``), every push is a new head: it cancels the window's
in-flight change-control run and starts it again. A busy window that is pushed
on every member event never merges, and holds every member PR behind it.

The companion emitter already refuses to rebuild a window in that state
(``OccCompanionEmitter._window_in_flight_reason``). This script applies the
same guard to the runner's push. It is standalone, not an import of the
emitter, so the constants below are copies; keep them in step with
``_WINDOW_MAX_HOLD_SECONDS``, ``_WINDOW_SETTLE_SECONDS`` and
``_RED_CHECK_CONCLUSIONS`` in
``src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/occ_companion_emitter.py``.

Exit codes
----------
``0``  proceed with the push. The window is quiet, the branch is not a window,
       there is no open window, the window conflicts, its head is older than
       the hold, the probe could not read it, or the wait timed out. Every
       fail-open path is logged as a warning; a stuck run never strands a
       member.
``10`` the window merged or closed while this script waited. Do not push: the
       receipts bind to the next window the product PR's autobind mints.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # fixed argv, no shell, trusted gh binary
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

# Copied from OccCompanionEmitter (see the module docstring).
WINDOW_MAX_HOLD_SECONDS: Final = 1800
WINDOW_SETTLE_SECONDS: Final = 180
RED_CHECK_CONCLUSIONS: Final = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    }
)

EXIT_PROCEED: Final = 0
EXIT_WINDOW_GONE: Final = 10

DEFAULT_MAX_WAIT_SECONDS: Final = 1500
DEFAULT_POLL_SECONDS: Final = 30
DEFAULT_OCC_REPO: Final = "OmniNode-ai/onex_change_control"

_WINDOW_PREFIX: Final = "auto/window-"
_CHECK_RUNS_PER_PAGE: Final = 100
_GH_TIMEOUT_SECONDS: Final = 60


class EnumQuietVerdict(StrEnum):
    PROCEED = "PROCEED"
    WAIT = "WAIT"
    WINDOW_GONE = "WINDOW_GONE"


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class WindowSnapshot:
    """One read of the window PR. ``state`` is the REST value, open or closed."""

    number: int
    state: str
    merged: bool
    conflicting: bool
    head_sha: str
    head_age_seconds: float
    armed: bool
    check_runs: tuple[CheckRun, ...]


@dataclass(frozen=True)
class NoOpenWindow:
    """The branch has no open PR."""


@dataclass(frozen=True)
class ProbeFailed:
    reason: str


Observation = WindowSnapshot | NoOpenWindow | ProbeFailed


@dataclass(frozen=True)
class QuietDecision:
    verdict: EnumQuietVerdict
    reason: str


def is_window_branch(branch: str) -> bool:
    return branch.startswith(_WINDOW_PREFIX)


def decide(branch: str, observation: Observation) -> QuietDecision:
    """Whether the receipt push may go to ``branch`` now. Pure."""
    proceed = EnumQuietVerdict.PROCEED
    if not is_window_branch(branch):
        return QuietDecision(proceed, f"{branch} is not a batch window")
    if isinstance(observation, NoOpenWindow):
        return QuietDecision(proceed, f"{branch} has no open window PR")
    if isinstance(observation, ProbeFailed):
        return QuietDecision(
            proceed,
            f"the window probe for {branch} could not read it "
            f"({observation.reason}); not holding the push",
        )
    label = f"OCC#{observation.number} head {observation.head_sha[:8]}"
    if observation.state != "open":
        how = "merged" if observation.merged else "closed"
        return QuietDecision(
            EnumQuietVerdict.WINDOW_GONE, f"OCC#{observation.number} {how}"
        )
    if observation.conflicting:
        return QuietDecision(
            proceed, f"OCC#{observation.number} conflicts; a push cannot cancel a merge"
        )
    if observation.head_age_seconds > WINDOW_MAX_HOLD_SECONDS:
        return QuietDecision(
            proceed,
            f"{label} is {int(observation.head_age_seconds)}s old, past the "
            f"{WINDOW_MAX_HOLD_SECONDS}s window hold",
        )
    runs = observation.check_runs
    running = [run for run in runs if run.status != "completed"]
    if running:
        return QuietDecision(
            EnumQuietVerdict.WAIT,
            f"{label} has {len(running)} check run(s) still running",
        )
    if not runs and observation.head_age_seconds < WINDOW_SETTLE_SECONDS:
        return QuietDecision(
            EnumQuietVerdict.WAIT,
            f"{label} was pushed {int(observation.head_age_seconds)}s ago and "
            f"its CI has not started",
        )
    red = [run for run in runs if run.conclusion in RED_CHECK_CONCLUSIONS]
    if observation.armed and not red:
        return QuietDecision(
            EnumQuietVerdict.WAIT, f"{label} is armed and green, so it is merging"
        )
    return QuietDecision(proceed, f"{label} is quiet")


class WindowProbe(Protocol):
    def observe(self, branch: str, number: int | None) -> Observation:
        """Read the open PR on ``branch``, or PR ``number`` once it is known."""
        ...


def wait_for_quiet(
    branch: str,
    probe: WindowProbe,
    *,
    max_wait_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> int:
    """Poll until the window is quiet, gone, or the budget is spent."""
    if not is_window_branch(branch):
        log(f"::notice::{branch} is not a batch window; pushing receipts now.")
        return EXIT_PROCEED
    deadline = clock() + max(0.0, max_wait_seconds)
    number: int | None = None
    while True:
        observation = probe.observe(branch, number)
        if isinstance(observation, WindowSnapshot):
            number = observation.number
        decision = decide(branch, observation)
        if decision.verdict is EnumQuietVerdict.WINDOW_GONE:
            log(
                f"::notice::{decision.reason} while the receipt push waited for "
                f"it to go quiet. Not pushing: the receipts bind when the product "
                f"PR's next autobind mints the next window (OMN-20042)."
            )
            return EXIT_WINDOW_GONE
        if decision.verdict is EnumQuietVerdict.PROCEED:
            level = "warning" if isinstance(observation, ProbeFailed) else "notice"
            log(f"::{level}::{decision.reason}; pushing receipts now (OMN-20042).")
            return EXIT_PROCEED
        remaining = deadline - clock()
        if remaining < poll_seconds:
            log(
                f"::warning::{decision.reason}, and the {int(max_wait_seconds)}s "
                f"wait budget is spent; pushing anyway so no member is stranded "
                f"(OMN-20042)."
            )
            return EXIT_PROCEED
        log(
            f"::notice::{decision.reason}; waiting {int(poll_seconds)}s before "
            f"pushing so the window's run is not cancelled (OMN-20042)."
        )
        sleep(poll_seconds)


class GitHubWindowProbe:
    """Reads the window PR through ``gh api`` with the OCC token."""

    def __init__(self, *, occ_repo: str, token: str) -> None:
        self._base = f"repos/{occ_repo}"
        self._owner = occ_repo.split("/", 1)[0]
        self._env = {**os.environ, "GH_TOKEN": token}

    def _get(self, path: str) -> object:
        completed = subprocess.run(
            ["gh", "api", f"{self._base}{path}"],
            capture_output=True,
            text=True,
            check=False,
            env=self._env,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise OSError(
                f"gh api {path} exited {completed.returncode}: "
                f"{completed.stderr.strip()[:200]}"
            )
        return json.loads(completed.stdout)

    def observe(self, branch: str, number: int | None) -> Observation:
        try:
            if number is None:
                head = urllib.parse.quote(f"{self._owner}:{branch}", safe=":")
                listing = self._get(f"/pulls?state=open&head={head}")
                if not isinstance(listing, list) or not listing:
                    return NoOpenWindow()
                first = listing[0]
                if not isinstance(first, dict) or not isinstance(
                    first.get("number"), int
                ):
                    return ProbeFailed(reason="the open PR listing names no number")
                number = int(first["number"])
            return self._snapshot(number)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            return ProbeFailed(reason=f"{type(exc).__name__}: {exc}")

    def _snapshot(self, number: int) -> Observation:
        pr = self._get(f"/pulls/{number}")
        if not isinstance(pr, dict):
            return ProbeFailed(reason=f"OCC#{number} did not read as an object")
        state = str(pr.get("state"))
        merged = pr.get("merged") is True
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if state != "open":
            return WindowSnapshot(
                number=number,
                state=state,
                merged=merged,
                conflicting=False,
                head_sha=head_sha if isinstance(head_sha, str) else "",
                head_age_seconds=0.0,
                armed=False,
                check_runs=(),
            )
        if not isinstance(head_sha, str) or not head_sha:
            return ProbeFailed(reason=f"OCC#{number} names no head sha")
        conflicting = (
            pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty"
        )
        commit = self._get(f"/commits/{head_sha}")
        committed = (
            ((commit.get("commit") or {}).get("committer") or {}).get("date")
            if isinstance(commit, dict)
            else None
        )
        head_age = (
            datetime.now(tz=UTC)
            - datetime.fromisoformat(str(committed).replace("Z", "+00:00"))
        ).total_seconds()
        return WindowSnapshot(
            number=number,
            state=state,
            merged=merged,
            conflicting=conflicting,
            head_sha=head_sha,
            head_age_seconds=head_age,
            armed=pr.get("auto_merge") is not None,
            check_runs=self._check_runs(head_sha),
        )

    def _check_runs(self, head_sha: str) -> tuple[CheckRun, ...]:
        runs: list[CheckRun] = []
        page = 1
        while True:
            listing = self._get(
                f"/commits/{head_sha}/check-runs"
                f"?per_page={_CHECK_RUNS_PER_PAGE}&page={page}"
            )
            batch = listing.get("check_runs") if isinstance(listing, dict) else None
            if not isinstance(batch, list) or not batch:
                break
            for run in batch:
                if isinstance(run, dict):
                    conclusion = run.get("conclusion")
                    runs.append(
                        CheckRun(
                            name=str(run.get("name")),
                            status=str(run.get("status")),
                            conclusion=conclusion
                            if isinstance(conclusion, str)
                            else None,
                        )
                    )
            total = listing.get("total_count") if isinstance(listing, dict) else None
            if not isinstance(total, int) or len(runs) >= total:
                break
            page += 1
        return tuple(runs)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--branch", required=True, help="the OCC companion branch")
    parser.add_argument("--occ-repo", default=DEFAULT_OCC_REPO)
    parser.add_argument(
        "--max-wait-seconds", type=float, default=DEFAULT_MAX_WAIT_SECONDS
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    token = os.environ.get("OCC_TOKEN", "")
    if is_window_branch(args.branch) and not token:
        print(
            "::warning::OCC_TOKEN is not set, so the window cannot be read; "
            "pushing receipts without waiting (OMN-20042)."
        )
        return EXIT_PROCEED
    return wait_for_quiet(
        args.branch,
        GitHubWindowProbe(occ_repo=args.occ_repo, token=token),
        max_wait_seconds=args.max_wait_seconds,
        poll_seconds=args.poll_seconds,
        clock=time.monotonic,
        sleep=time.sleep,
        log=lambda line: print(line, flush=True),
    )


if __name__ == "__main__":
    sys.exit(main())
