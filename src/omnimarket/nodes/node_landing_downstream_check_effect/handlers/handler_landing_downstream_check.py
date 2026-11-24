# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install a core or spi pull request head into each downstream repository and run its import smoke.

The landing controller merges a pull request into a repository that other repositories install only after asking
this node. For each downstream repository it builds a throwaway venv, installs the repository's checkout with no
lockfile, replaces the package with the head under test, and runs the import smoke the downstream repository's own
CI runs (``tests/ci/latest_core_smoke.py``, which names each failing module, symbol and importing file and line),
then any extra command the target declares. A break in any repository, or any step that did not run to an answer,
is not a pass: the verdict is ``break`` or ``unread`` and the controller holds the merge. The handler raises
nothing; every failure ends as a typed reading.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from omnimarket.models.model_downstream_gate import (
    EnumDownstreamVerdict,
    ModelDownstreamBreak,
    ModelDownstreamGateFact,
    ModelDownstreamTargetReading,
)

from ..models import (
    ModelCommandOutcome,
    ModelDownstreamTarget,
    ModelLandingDownstreamCheckRequest,
)

# Interpreter-location variables of the caller; a venv's interpreter must resolve its own packages.
INTERPRETER_ENV = ("PYTHONPATH", "PYTHONHOME")
SMOKE_FAIL = re.compile(
    r"^LATEST-CORE-SMOKE FAIL node=(?P<node>\S+) module=(?P<module>\S+) symbol=(?P<symbol>\S+) "
    r"at=(?P<at>\S+) error=(?P<error>.*)$"
)
SMOKE_TOTAL = re.compile(
    r"^LATEST-CORE-SMOKE packages=(?P<packages>\d+) modules_tried=(?P<tried>\d+) failed=(?P<failed>\d+)$"
)
FILE_LINE = re.compile(r"(?P<at>[\w./-]+\.py:\d+)")

# Run by the venv's own interpreter, isolated: the commit the package was installed from, or nothing for a release.
PROBE = (
    "import importlib.metadata as m, json, sys\n"
    "text = m.distribution(sys.argv[1]).read_text('direct_url.json')\n"
    "print(json.loads(text).get('vcs_info', {}).get('commit_id', '') if text else '')\n"
)

Runner = Callable[[Sequence[str], float, Path | None], ModelCommandOutcome]


def _run(
    argv: Sequence[str], timeout_seconds: float, cwd: Path | None
) -> ModelCommandOutcome:
    env: Mapping[str, str] = {
        k: v for k, v in os.environ.items() if k not in INTERPRETER_ENV
    }
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=dict(env),
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return ModelCommandOutcome(returncode=-1, timed_out=True)
    except OSError as exc:
        return ModelCommandOutcome(returncode=-1, os_error=type(exc).__name__)
    return ModelCommandOutcome(
        returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
    )


def _tail(outcome: ModelCommandOutcome) -> str:
    lines = (outcome.stderr or outcome.stdout).strip().splitlines()
    return lines[-1] if lines else "no output"


def _failure(step: str, outcome: ModelCommandOutcome) -> str:
    """The typed reason a command ended badly, or an empty string when it succeeded."""
    if outcome.timed_out:
        return f"timeout:{step}"
    if outcome.os_error:
        return f"{step}-failed:{outcome.os_error}"
    if outcome.returncode != 0:
        return f"{step}-failed:rc={outcome.returncode}"
    return ""


class HandlerLandingDownstreamCheck:
    """Try one head in every downstream repository; the verdict is a pass only when every one passed."""

    def __init__(self, run: Runner = _run) -> None:
        self._run = run

    def handle(
        self, request: ModelLandingDownstreamCheckRequest
    ) -> ModelDownstreamGateFact:
        readings = tuple(self._try(request, target) for target in request.targets)
        verdict = EnumDownstreamVerdict.PASS
        if any(r.verdict is EnumDownstreamVerdict.UNREAD for r in readings):
            verdict = EnumDownstreamVerdict.UNREAD
        if any(r.verdict is EnumDownstreamVerdict.BREAK for r in readings):
            verdict = EnumDownstreamVerdict.BREAK
        reasons = tuple(
            line
            for r in readings
            for line in (
                *(
                    f"{b.downstream}: {b.module} cannot import {b.symbol} at {b.at}"
                    for b in r.breaks
                ),
                *((f"{r.downstream}: {r.reason}",) if r.reason else ()),
            )
        )
        return ModelDownstreamGateFact(
            repo=request.repo,
            head_sha=request.head_sha,
            verdict=verdict,
            targets=readings,
            reasons=reasons,
        )

    def _try(
        self, request: ModelLandingDownstreamCheckRequest, target: ModelDownstreamTarget
    ) -> ModelDownstreamTargetReading:
        venv = (
            request.work_dir / f"{request.repo}-{request.head_sha[:12]}-{target.name}"
        )
        python = venv / "bin" / "python"
        installer = request.installer
        # The installs run in the work directory with no installer config: a checkout's own pyproject would
        # otherwise constrain the package under test to the version it already holds, and the head would not
        # replace it (found replaying omnibase_core#2361: "Checked 1 package", head never installed).
        try:
            request.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ModelDownstreamTargetReading(
                downstream=target.name,
                verdict=EnumDownstreamVerdict.UNREAD,
                reason=f"work-dir-failed:{type(exc).__name__}",
            )
        cwd = request.work_dir
        steps: tuple[tuple[str, list[str], float, Path | None], ...] = (
            (
                "venv",
                [
                    installer,
                    "venv",
                    "--no-config",
                    "--python",
                    request.python_version,
                    str(venv),
                ],
                request.venv_timeout_seconds,
                cwd,
            ),
            (
                "install-downstream",
                [
                    installer,
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "-e",
                    str(target.checkout),
                ],
                request.install_timeout_seconds,
                target.checkout,
            ),
            (
                "install-head",
                [
                    installer,
                    "pip",
                    "install",
                    "--no-config",
                    "--python",
                    str(python),
                    "--upgrade",
                    "--no-deps",
                    request.core_spec(),
                ],
                request.install_timeout_seconds,
                cwd,
            ),
            (
                "probe-head",
                [str(python), "-I", "-c", PROBE, request.package],
                request.venv_timeout_seconds,
                cwd,
            ),
        )
        for step, argv, timeout, step_cwd in steps:
            outcome = self._run(argv, timeout, step_cwd)
            reason = _failure(step, outcome)
            if reason:
                return ModelDownstreamTargetReading(
                    downstream=target.name,
                    verdict=EnumDownstreamVerdict.UNREAD,
                    reason=f"{reason}: {_tail(outcome)}",
                )
            if step == "probe-head" and outcome.stdout.strip() != request.head_sha:
                # an install that exited zero but left a release in place would make every head read as a pass
                return ModelDownstreamTargetReading(
                    downstream=target.name,
                    verdict=EnumDownstreamVerdict.UNREAD,
                    reason=(
                        f"head-not-installed: {request.package} is installed from "
                        f"{outcome.stdout.strip() or 'a release'}, not {request.head_sha[:12]}"
                    ),
                )
        smoke = self._run(
            [str(python), str(request.smoke_script)],
            request.smoke_timeout_seconds,
            target.checkout,
        )
        reading = self._read_smoke(target.name, smoke)
        if reading.verdict is not EnumDownstreamVerdict.PASS or not target.test_args:
            return reading
        extra = self._run(
            [str(python), *target.test_args],
            request.smoke_timeout_seconds,
            target.checkout,
        )
        failed = _failure("tests", extra)
        if not failed:
            return reading
        if extra.timed_out or extra.os_error:
            return ModelDownstreamTargetReading(
                downstream=target.name,
                verdict=EnumDownstreamVerdict.UNREAD,
                reason=failed,
                modules_tried=reading.modules_tried,
            )
        located = FILE_LINE.search(extra.stdout + "\n" + extra.stderr)
        return ModelDownstreamTargetReading(
            downstream=target.name,
            verdict=EnumDownstreamVerdict.BREAK,
            reason=f"{failed}: {_tail(extra)}",
            modules_tried=reading.modules_tried,
            breaks=(
                ModelDownstreamBreak(
                    downstream=target.name,
                    module="tests",
                    at=located["at"] if located else "-",
                    error=_tail(extra),
                ),
            ),
        )

    @staticmethod
    def _read_smoke(
        name: str, smoke: ModelCommandOutcome
    ) -> ModelDownstreamTargetReading:
        failure = _failure("smoke", smoke)
        if smoke.timed_out or smoke.os_error:
            return ModelDownstreamTargetReading(
                downstream=name, verdict=EnumDownstreamVerdict.UNREAD, reason=failure
            )
        breaks: list[ModelDownstreamBreak] = []
        tried = 0
        for line in smoke.stdout.splitlines():
            if (m := SMOKE_FAIL.match(line)) is not None:
                breaks.append(
                    ModelDownstreamBreak(
                        downstream=name,
                        module=m["module"],
                        symbol=m["symbol"],
                        at=m["at"],
                        error=m["error"],
                    )
                )
            elif (t := SMOKE_TOTAL.match(line)) is not None:
                tried = int(t["tried"])
        if breaks:
            return ModelDownstreamTargetReading(
                downstream=name,
                verdict=EnumDownstreamVerdict.BREAK,
                modules_tried=tried,
                breaks=tuple(breaks),
            )
        if smoke.returncode != 0 or tried == 0:
            # a smoke that exited non-zero with no named failure, or imported nothing, is no pass
            return ModelDownstreamTargetReading(
                downstream=name,
                verdict=EnumDownstreamVerdict.UNREAD,
                reason=failure or "smoke-imported-nothing",
                modules_tried=tried,
            )
        return ModelDownstreamTargetReading(
            downstream=name, verdict=EnumDownstreamVerdict.PASS, modules_tried=tried
        )
