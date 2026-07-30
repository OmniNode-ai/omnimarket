#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prove the merge-hold gate FIRES, in the adopting repo's own CI (OMN-15484).

Why a live self-proof and not a test
------------------------------------
OMN-15484 AC3 asks, for each adopting repo, for "a held-title vector producing
exit 1 in that repo's CI (a workflow-dispatch or scratch PR is acceptable; an
assertion is not)". A green ``Merge Hold Gate`` check-run proves only that the
job ran and that *this* PR is not held. It does not prove the gate can say no.
Every failure mode that matters is silent under that observation:

* the canonical checkout resolved to a ref where the vocabulary has moved,
* the gate script exited 0 on an internal error a caller swallowed,
* the workflow passes the surfaces in a shape the evaluator reads as "not
  observed" — which on a *clear* PR still exits 0 via the payload path,
* a future edit narrows the vocabulary to nothing.

In every one of those the adopting repo shows a green hold gate and enforces
nothing. That is precisely the "looks like enforcement and enforces nothing"
outcome the ticket calls worse than no gate at all.

So this script runs BEFORE the real evaluation, on every execution in every
adopting repo, and drives the real ``check_pr_hold_marker.py`` CLI as a
subprocess over two vectors:

1. a **held** title -> the CLI must exit 1,
2. a **clear** title -> the CLI must exit 0.

Both directions are required. Exit-1-always is as broken as exit-0-always, and
only the pair distinguishes a working matcher from a stuck one.

The probe vectors are validated against the canonical vocabulary first
-----------------------------------------------------------------------
A hardcoded "held" string would rot the moment the vocabulary changes, and it
would rot *silently* in the safe-looking direction: the vector stops being a
hold token, the gate correctly exits 0, and the self-test starts asserting
nothing. So the script loads the canonical module and asserts — via the
canonical ``match_hold_token`` — that its held vector really is a token and its
clear vector really is not, **before** running either through the CLI. If the
vocabulary ever stops matching the probe, this fails loudly instead of going
quietly vacuous.

The vectors are exercised through the event-payload path with no token in the
environment, so the self-test performs no network I/O and cannot be answered by
the live PR under evaluation.

Exit codes: ``0`` the gate demonstrably fires and demonstrably clears, ``1``
anything else.

Related: OMN-15484 AC3, OMN-15483 (the gate this proves).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.ci.check_pr_hold_marker import (
    CANONICAL_HOLD_MODULE,
    CanonicalVocabularyUnavailableError,
    load_canonical_hold_module,
)

EXIT_OK = 0
EXIT_BROKEN = 1

GATE_SCRIPT = Path(__file__).resolve().parent / "check_pr_hold_marker.py"

# The probe vectors. Neither is trusted: both are checked against the canonical
# matcher before use (see the module docstring), so a vocabulary change that
# invalidates either one fails this script rather than hollowing it out.
#
# The held vector is deliberately the shape the OMN-15483 incident table records
# a human actually using on a PR title.
HELD_VECTOR = "[OMN-15484 self-test] DO NOT MERGE — hold gate liveness probe"
CLEAR_VECTOR = "feat(OMN-15484): fan out the merge hold gate to this repository"

_SUBPROCESS_TIMEOUT_SECONDS = 60


class SelfTestBrokenError(RuntimeError):
    """The gate did not behave as a gate. Never downgraded to a warning."""


def _run_gate(title: str, *, gate_script: Path, repo_root: Path) -> tuple[int, str]:
    """Run the real gate CLI over ``title`` on the payload path.

    Args:
        title: The PR title to feed the gate.
        gate_script: Path to ``check_pr_hold_marker.py``.
        repo_root: Working directory (the canonical checkout root).

    Returns:
        ``(exit_code, combined_output)``.
    """
    env = {
        # Only what the gate reads. No GH_TOKEN/GITHUB_TOKEN: the live-fetch
        # branch must not be reachable, or the probe could be answered by the
        # real PR instead of by the vector.
        "GITHUB_EVENT_NAME": "pull_request",
        "PR_TITLE": title,
        "PR_LABELS_JSON": "[]",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(repo_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(gate_script)],
        env=env,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def check_context_name(canonical: object, context_name: str | None) -> str | None:
    """AC5: the check's own context name must not be a hold token.

    The first draft of the omnimarket gate was named ``Verification Hold Gate``,
    and ``verification hold`` is a token in the vocabulary the gate enforces.
    Nothing failed at the time — a job name only reaches the *title* surface
    when a human writes it there — but the fan-out is exactly that case: a PR
    titled "fan out <gate name> to onex_change_control" would have been refused
    by the gate it was installing, which reads as the gate being broken rather
    than as the gate working.

    Checking the CONTEXT name (``<caller job> / <inner job>``) rather than only
    the inner job name matters because the caller supplies half of it, and the
    caller is a different repo from the one holding this test.

    Args:
        canonical: The loaded canonical vocabulary module.
        context_name: The context name to validate, or ``None`` to skip.

    Returns:
        A failure report, or ``None`` when the name is acceptable.
    """
    if not context_name:
        return None
    token = canonical.match_hold_token(context_name)  # type: ignore[attr-defined]
    if token is None:
        return None
    return (
        "FAIL — this gate's own check context is itself a hold token.\n"
        f"  context : {context_name!r}\n"
        f"  token   : {token!r}\n"
        "\n"
        "Any PR whose title mentions this check would be held by the check "
        "itself — including the PR that installs it in the next repository. "
        "Rename the job to something the vocabulary does not match."
    )


def run(
    *,
    gate_script: Path = GATE_SCRIPT,
    module_path: Path = CANONICAL_HOLD_MODULE,
    held_vector: str = HELD_VECTOR,
    clear_vector: str = CLEAR_VECTOR,
    context_name: str | None = None,
) -> tuple[int, str]:
    """Prove the gate fires on a held vector and clears on a clean one.

    Args:
        gate_script: The gate CLI to drive.
        module_path: The canonical vocabulary module.
        held_vector: A title that MUST be held.
        clear_vector: A title that MUST NOT be held.
        context_name: The check context name to validate for AC5, if supplied.

    Returns:
        ``(exit_code, report)``.
    """
    try:
        canonical = load_canonical_hold_module(module_path)
    except CanonicalVocabularyUnavailableError as exc:
        return EXIT_BROKEN, f"FAIL (fail-closed): {exc}"

    if not gate_script.is_file():
        return EXIT_BROKEN, (
            f"FAIL (fail-closed): the gate CLI is not at {gate_script} — this "
            "repository's hold check cannot run at all"
        )

    name_failure = check_context_name(canonical, context_name)
    if name_failure is not None:
        return EXIT_BROKEN, name_failure

    # (1) The vectors must still mean what they are named. Checked FIRST so a
    #     vocabulary change can never leave this script asserting nothing.
    held_token = canonical.match_hold_token(held_vector)
    if held_token is None:
        return EXIT_BROKEN, (
            "FAIL — the self-test's HELD vector is no longer a hold token "
            f"({held_vector!r}). The vocabulary changed and this probe went "
            "vacuous: it would have 'passed' by testing nothing. Update the "
            "vector in the same commit as the vocabulary."
        )
    if canonical.match_hold_token(clear_vector) is not None:
        return EXIT_BROKEN, (
            "FAIL — the self-test's CLEAR vector now matches the vocabulary "
            f"({clear_vector!r}), so the negative direction proves nothing. "
            "Either the vocabulary over-matches or the vector needs replacing."
        )

    repo_root = gate_script.resolve().parents[2]

    # (2) Held vector -> the gate must refuse.
    held_code, held_output = _run_gate(
        held_vector, gate_script=gate_script, repo_root=repo_root
    )
    if held_code == 0:
        return EXIT_BROKEN, (
            "FAIL — the hold gate did NOT fire on a held title.\n"
            f"  vector      : {held_vector!r}\n"
            f"  token in it : {held_token!r}\n"
            f"  exit code   : {held_code} (expected 1)\n"
            f"  gate output : {held_output}\n"
            "\n"
            "A gate that cannot say no is not enforcement. This repository's "
            "Merge Hold Gate would be green on every PR while holding nothing."
        )

    # (3) Clear vector -> the gate must pass. Guards the opposite failure: a
    #     gate stuck at exit 1 would block every PR in the adopting repo.
    clear_code, clear_output = _run_gate(
        clear_vector, gate_script=gate_script, repo_root=repo_root
    )
    if clear_code != 0:
        return EXIT_BROKEN, (
            "FAIL — the hold gate refused an UNHELD title.\n"
            f"  vector     : {clear_vector!r}\n"
            f"  exit code  : {clear_code} (expected 0)\n"
            f"  gate output: {clear_output}\n"
            "\n"
            "Stuck-closed is a fleet outage, not a safe default: every PR in "
            "this repository would be unmergeable."
        )

    return EXIT_OK, (
        "PASS — the hold gate demonstrably fires and demonstrably clears.\n"
        f"  held  {held_vector!r} -> exit {held_code} "
        f"(matched {held_token!r})\n"
        f"  clear {clear_vector!r} -> exit {clear_code}\n"
        f"  vocabulary: {module_path}\n"
        f"  gate CLI  : {gate_script}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 proven, 1 broken.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Prove the merge-hold gate fires on a held title and clears on an "
            "unheld one, in the calling repository's own CI (OMN-15484 AC3)."
        )
    )
    parser.add_argument(
        "--gate-script",
        type=Path,
        default=GATE_SCRIPT,
        help="Path to check_pr_hold_marker.py (default: the sibling script).",
    )
    parser.add_argument(
        "--module-path",
        type=Path,
        default=CANONICAL_HOLD_MODULE,
        help="Path to the canonical hold_marker.py (default: the in-repo module).",
    )
    parser.add_argument(
        "--context-name",
        default=None,
        help=(
            "Check context name to validate for AC5. Defaults to the "
            "HOLD_GATE_CONTEXT_NAME environment variable, which is how the "
            "reusable workflow passes it (env, never interpolated into a "
            "script body)."
        ),
    )
    args = parser.parse_args(argv)

    code, report = run(
        gate_script=args.gate_script,
        module_path=args.module_path,
        context_name=args.context_name or os.environ.get("HOLD_GATE_CONTEXT_NAME"),
    )
    if code == EXIT_OK:
        print(report)
        print("::notice::Merge Hold Gate self-test: the gate fires and clears.")
    else:
        print(report, file=sys.stderr)
        print(
            f"::error::Merge Hold Gate self-test: {report.splitlines()[0]}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
