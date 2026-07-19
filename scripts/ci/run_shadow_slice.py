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
  * ``2`` on a harness usage/spawn error.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys

EXIT_HANG = 124
"""Conventional timeout exit code; distinct from a product test failure (1)."""

_DEFAULT_TIMEOUT_S = 900.0
_ABORT_GRACE_S = 2.0  # let PYTHONFAULTHANDLER flush thread stacks before SIGKILL
_REAP_WAIT_S = 5.0

# Canonical isolation-hang phrases the merge-controller classifier keys on
# (``reason_code_classifier._RUNNER_INFRA_LOG_SIGNATURES``). Kept as literals so
# this harness stays stdlib-only; the field-by-field seam against the live
# classifier is guarded by tests/ci/test_run_shadow_slice.py (a real
# cross-boundary regression test, per CLAUDE.md's define-and-match-seams rule).
_HANG_SIGNATURES: tuple[str, ...] = ("hard timeout", "leaked thread", "os._exit(1)")


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
        )
    except (FileNotFoundError, OSError) as exc:
        _emit(f"[run_shadow_slice] error: could not start command: {exc}")
        return 2

    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _on_timeout(proc, timeout_s)
        return EXIT_HANG
    except BaseException:
        # Runner cancellation (SIGTERM→KeyboardInterrupt) or Ctrl-C: never leave
        # the pytest process group orphaned on the runner.
        _emit("[run_shadow_slice] interrupted — reaping child process group")
        _kill_group(proc, _safe_pgid(proc))
        raise


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
