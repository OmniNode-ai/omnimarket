#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Signal-based timeout harness for the product-readiness shadow slice (OMN-14775).

Problem (friction F-23, evidence ``omnibase_infra#2325``): the shadow-slice /
merge-proof harness enforced its per-slice timeout by letting a hung test trip
``pytest-timeout``'s **thread** method, which calls ``os._exit(1)``. That hard
exit

  * does NOT reap child processes — a subprocess/xdist worker spawned by the
    hung test is orphaned/leaked and can hold a runner slot;
  * masks every not-yet-run downstream result (the interpreter is torn down
    mid-session with no session teardown);
  * surfaces as a bare non-zero exit that the merge-controller reason-code
    classifier maps to ``PRODUCT_FAILED`` — a wrong code-fix dispatch for what
    is actually a thread/isolation hang.

The classifier half of F-23 (tagging such a hang as ``RUNNER_INFRA``) already
landed — ``omnimarket.merge_control.reason_code_classifier`` keys on the
``os._exit(1)`` / ``thread timeout`` / ``hard timeout`` / ``leaked thread``
log signatures (OMN-14765 / OMN-14769). This harness is the deferred **mechanism**
half: it replaces the ``os._exit`` thread-timeout with a *signal-based* timeout
that

  1. runs the slice command in its own process group (``start_new_session``),
  2. on timeout, sends ``SIGABRT`` to the group so the child (run with
     ``PYTHONFAULTHANDLER=1``) dumps every thread stack — the leaked-thread /
     isolation diagnostics — then ``SIGTERM`` and finally ``SIGKILL`` to REAP
     the WHOLE group (no orphaned/leaked child), and
  3. emits a diagnostic line carrying the canonical hang signatures so the
     classifier routes the timeout to ``RUNNER_INFRA`` (hang/isolation), never
     ``PRODUCT_FAILED``.

It is a sibling of ``scripts/ci/run_coverage_sweep_gate.py`` (which reaps the
coverage-generation process group the same way); this one adds the SIGABRT
thread-dump + the hang-signature emission that the classifier consumes.

Usage:
  run_shadow_slice.py [--timeout SECONDS] -- CMD [ARGS...]

Exit codes:
  * the wrapped command's own exit code on normal completion (0 = green);
  * ``124`` (:data:`EXIT_HANG`) when the slice exceeded ``--timeout`` and was
    reaped via signals — a hang/isolation class, not a product failure;
  * ``125`` (:data:`EXIT_INFRA_CRASH`) when the slice's own summary reported a
    COMPLETE, zero-failure session and the interpreter then died on a fatal
    signal — a runner/environment class, not a product failure (OMN-18820);
  * ``2`` on a harness usage/spawn error.

OMN-18820 is the second half of the same idea as the timeout above. Measured over
48 h, 7 of 19 failing shadow runs were a C-extension crash in garbage collection
*after* ``15683 passed`` — the product was green and the gate said
``PRODUCT_FAILED``. A further 4 carried real failing tests alongside the same
crash and must stay red; that asymmetry is why the class is gated on the slice's
own summary rather than on the exit code alone.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
from collections import deque

EXIT_HANG = 124
"""Conventional timeout exit code; distinct from a product test failure (1)."""

EXIT_INFRA_CRASH = 125
"""The interpreter died on a fatal SIGNAL *after* the session reported clean.

OMN-18820. Distinct from :data:`EXIT_HANG` (the slice never finished) and from a
product failure (the slice finished and some test failed). This code means the
product evidence is COMPLETE and GREEN and the runner's interpreter then crashed
on its way out -- a runner/environment fact, never a statement about the code.
"""

_DEFAULT_TIMEOUT_S = 900.0
_ABORT_GRACE_S = 2.0  # let PYTHONFAULTHANDLER flush thread stacks before SIGKILL
_REAP_WAIT_S = 5.0
_TAIL_LINES = 400  # bounded: enough to hold pytest's summary + a fault trace

# Canonical isolation-hang phrases the merge-controller classifier keys on
# (``reason_code_classifier._RUNNER_INFRA_LOG_SIGNATURES``). Kept as literals so
# this harness stays stdlib-only; the field-by-field seam against the live
# classifier is guarded by tests/ci/test_run_shadow_slice.py (a real
# cross-boundary regression test, per CLAUDE.md's define-and-match-seams rule).
_HANG_SIGNATURES: tuple[str, ...] = ("hard timeout", "leaked thread", "os._exit(1)")

# OMN-18820: the post-clean-session crash class, mirrored into the same live
# classifier list and pinned by the same cross-boundary test as the hang
# signatures above. It is SAFE for that classifier to rank this as infra above a
# product failure -- the precedence problem that rules out a bare "segmentation
# fault" signature -- because this harness emits the phrase ONLY when the
# session's own summary reported zero failures and zero errors. A run with a
# failing test can never carry it.
_CRASH_SIGNATURES: tuple[str, ...] = (
    "runner interpreter crashed after a clean session",
)

# pytest's terminal summary line, in `-q --no-header` form, e.g.
#   "2 failed, 15718 passed, 176 skipped, ... in 755.38s (0:12:35)"
#   "15683 passed, 176 skipped, 7019 deselected, ... in 775.72s (0:12:55)"
# The trailing wall-clock is what distinguishes the real summary from progress
# chatter that merely contains the word "passed".
_SUMMARY_RE = re.compile(r"\b\d+\s+(?:passed|failed|error|errors)\b.*?\bin\s+[\d.]+s")
_SUMMARY_RED_RE = re.compile(r"\b\d+\s+(?:failed|error|errors)\b")

VERDICT_CLEAN = "clean"
VERDICT_FAILED = "failed"
VERDICT_UNKNOWN = "unknown"
"""No terminal summary was printed at all -- the session did not finish."""


def session_verdict(tail: list[str]) -> str:
    """Read the slice's own terminal summary out of its last lines.

    Returns :data:`VERDICT_UNKNOWN` when no summary line is present, which is the
    fail-closed answer: a session that never printed a summary has proven
    nothing, so a crash on top of it is NOT eligible for the infra class.
    """
    for line in reversed(tail):
        if _SUMMARY_RE.search(line):
            return VERDICT_FAILED if _SUMMARY_RED_RE.search(line) else VERDICT_CLEAN
    return VERDICT_UNKNOWN


def fatal_signal_number(returncode: int) -> int | None:
    """The signal that killed the slice, or ``None`` if it exited normally.

    Two shapes, because the two are not distinguishable downstream. A child this
    harness spawns directly reports ``-N``. A child behind a wrapper that waits
    and translates -- ``uv run`` is the live case, which is why the field logs
    show a plain ``139`` rather than ``-11`` -- reports ``128 + N``.
    """
    if returncode < 0:
        return -returncode
    if 128 < returncode < 256:
        return returncode - 128
    return None


def _emit(msg: str) -> None:
    """Print to stdout (captured in the CI job log the classifier scans)."""
    print(msg, flush=True)


def _safe_pgid(proc: subprocess.Popen[bytes]) -> int:
    if os.name != "posix":
        return proc.pid
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return proc.pid


def _signal_group(proc: subprocess.Popen[bytes], pgid: int, sig: int) -> None:
    """Best-effort signal to the child's whole process group (never raises)."""
    if os.name != "posix":
        with contextlib.suppress(ProcessLookupError, OSError, ValueError):
            proc.send_signal(sig)
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, sig)


def _wait_briefly(proc: subprocess.Popen[bytes], seconds: float) -> bool:
    """Wait up to ``seconds`` for exit; return True if the process is gone."""
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _kill_group(proc: subprocess.Popen[bytes], pgid: int) -> None:
    """SIGTERM then SIGKILL the process group until it is reaped."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(proc, pgid, sig)
        if _wait_briefly(proc, _REAP_WAIT_S):
            return


def _on_timeout(proc: subprocess.Popen[bytes], timeout_s: float) -> None:
    """Signal-based reap of a hung slice, replacing the legacy ``os._exit``.

    Emits the diagnostic FIRST so the hang signatures reach the job log even if
    the subsequent reap stalls, then SIGABRT (thread dump) → SIGTERM → SIGKILL
    across the whole process group so no child is orphaned.
    """
    pgid = _safe_pgid(proc)
    _emit(
        f"[run_shadow_slice] HARD TIMEOUT after {timeout_s:g}s (pgid={pgid}). "
        "Replacing the legacy os._exit(1) thread-timeout with a signal-based "
        "reap (OMN-14775): this is a thread/isolation hang (leaked thread), "
        "NOT a product failure. Dumping child thread stacks via SIGABRT, then "
        "reaping the process group."
    )
    # SIGABRT → the child (PYTHONFAULTHANDLER=1) writes every thread's stack to
    # stderr: the leaked-thread / isolation diagnostics acceptance criterion #2.
    _signal_group(proc, pgid, signal.SIGABRT)
    _wait_briefly(proc, _ABORT_GRACE_S)
    _kill_group(proc, pgid)
    if proc.poll() is None:
        _emit("[run_shadow_slice] warning: child still present after SIGKILL")
    else:
        _emit("[run_shadow_slice] process group reaped; no orphaned child left behind")


def run_slice(command: list[str], timeout_s: float) -> int:
    """Run ``command`` under a signal-based timeout; return its (or hang) code."""
    if not command:
        _emit("[run_shadow_slice] error: no command given after `--`")
        return 2

    child_env = dict(os.environ)
    # Ensure the child dumps all thread tracebacks on SIGABRT (isolation diag).
    child_env["PYTHONFAULTHANDLER"] = "1"
    new_session = os.name == "posix"
    _emit(f"[run_shadow_slice] running (timeout={timeout_s:g}s): " + " ".join(command))
    try:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            command,
            env=child_env,
            start_new_session=new_session,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        _emit(f"[run_shadow_slice] error: could not start command: {exc}")
        return 2

    # OMN-18820: the child's output is teed rather than inherited, so the harness
    # can read the slice's OWN verdict. The reader runs on its own thread and
    # writes every line straight through, so the job log is byte-for-byte what it
    # was before; draining continuously is also what keeps the pipe from filling
    # and deadlocking the timeout below.
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    reader = threading.Thread(target=_tee, args=(proc, tail), daemon=True)
    reader.start()

    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _on_timeout(proc, timeout_s)
        return EXIT_HANG
    except BaseException:
        # Runner cancellation (SIGTERM→KeyboardInterrupt) or Ctrl-C: never leave
        # the pytest process group orphaned on the runner.
        _emit("[run_shadow_slice] interrupted — reaping child process group")
        _kill_group(proc, _safe_pgid(proc))
        raise

    # Let the tee drain whatever is still buffered before the summary is read;
    # the child is already gone, so this cannot block indefinitely.
    reader.join(timeout=_REAP_WAIT_S)
    return _classify_exit(rc, list(tail))


def _tee(proc: subprocess.Popen[bytes], tail: deque[str]) -> None:
    """Stream the child's output through to our stdout, keeping a bounded tail."""
    stream = proc.stdout
    if stream is None:  # pragma: no cover - PIPE is always requested above
        return
    with contextlib.suppress(ValueError, OSError):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            print(line, flush=True)
            tail.append(line)


def _classify_exit(rc: int, tail: list[str]) -> int:
    """Separate a post-clean-session runner crash from a product failure.

    The whole point of the split: a fatal signal is not evidence about the code.
    It is only eligible for the infra class when the slice's own summary says the
    product evidence is complete and green. Anything else -- a failing test, or
    no summary at all -- stays exactly as red as it is today.
    """
    signo = fatal_signal_number(rc)
    if signo is None:
        return rc

    verdict = session_verdict(tail)
    name = (
        signal.Signals(signo).name
        if signo in set(signal.Signals)
        else f"signal {signo}"
    )
    if verdict == VERDICT_CLEAN:
        _emit(
            f"[run_shadow_slice] {_CRASH_SIGNATURES[0]}: the slice reported a "
            f"complete summary with zero failures and the interpreter then died "
            f"on {name} (exit {rc}). The product evidence is GREEN and COMPLETE; "
            f"this is a runner/environment fact and is reported as the infra "
            f"dimension, NOT as a product failure (OMN-18820)."
        )
        return EXIT_INFRA_CRASH

    why = (
        "the summary reports failures"
        if verdict == VERDICT_FAILED
        else "the slice printed NO terminal summary, so it proved nothing"
    )
    _emit(
        f"[run_shadow_slice] the interpreter died on {name} (exit {rc}) and "
        f"{why} — this stays a PRODUCT failure and the original exit code is "
        f"preserved (OMN-18820)."
    )
    return rc


def _install_cancellation_reaper() -> None:
    """Turn runner SIGTERM/SIGINT into KeyboardInterrupt so we reap on cancel."""

    def _raise_interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Not on the main thread / unsupported platform — skip silently.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _raise_interrupt)


def _split_args(raw: list[str]) -> tuple[list[str], list[str]]:
    """Split argv on the FIRST bare ``--``: harness options, then slice command.

    A manual split is deterministic and unit-testable (``argparse.REMAINDER`` is
    quirky about whether it keeps the leading ``--``).
    """
    if "--" in raw:
        sep = raw.index("--")
        return raw[:sep], raw[sep + 1 :]
    return raw, []


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    opt_args, command = _split_args(raw)

    parser = argparse.ArgumentParser(
        prog="run_shadow_slice.py",
        description=(
            "Run a shadow-slice command under a signal-based timeout that reaps "
            "the whole process group (OMN-14775)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_S,
        help="Seconds before the slice is treated as a hang and reaped.",
    )
    ns = parser.parse_args(opt_args)
    if ns.timeout <= 0:
        parser.error("--timeout must be > 0")

    _install_cancellation_reaper()
    return run_slice(command, float(ns.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
