# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done-gate readings of a pull request, as one stdlib-only command.

The contracts under ``contracts/`` carry unbound ``local_done_gate`` items that read
the PR (its comments, its check-runs and its merge state). Each item pipes one
``onex scm`` answer into this file and gets exit 0 only when the fact holds::

    onex scm pr-view <repo>#<n> --source bus | python3 tests/ci/pr_state_checks.py \
        lab-comment "$(git rev-parse HEAD)" --contains "Findings 1-9"

The answer shapes are the recorded ones in ``tests/ci/fixtures/omn25309``.
``tests/ci/test_omn25309_local_check_assertions.py`` runs every contract command
against those recordings and against payloads that lack the fact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SHARED_RED = "delegation-health-check / Delegation Health Check"
# mergeStateStatus values the reading accepts. BLOCKED is what GitHub reports while
# a required check is not green, which the required-checks reading covers; DIRTY
# (a conflict), BEHIND, UNKNOWN and DRAFT are refused.
_MERGE_STATES_OK = frozenset({"CLEAN", "HAS_HOOKS", "BLOCKED", "UNSTABLE"})


def _fail(message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def lab_comment(view: dict[str, Any], head: str, contains: list[str]) -> int:
    """A comment whose body STARTS with the Lab line for ``head`` holds each text.

    A review comment that quotes the line mid-body, or a Lab line for another head,
    does not count.
    """
    result = view.get("result", {})
    if result.get("head_sha") != head:
        return _fail(f"the PR head is {result.get('head_sha')!r}, not {head}")
    comments = result.get("comments")
    if not isinstance(comments, list):
        return _fail("the answer holds no comments list (use --source bus)")
    prefix = f"Lab: head={head}"
    for comment in comments:
        body = str(comment.get("body", ""))
        if body.startswith(prefix) and all(text in body for text in contains):
            return 0
    return _fail(f"no comment starts with {prefix!r} and holds {contains!r}")


def required_checks(checks: dict[str, Any], head: str, required_file: str) -> int:
    """Every required context reads success at ``head`` (the shared red is exempt).

    The names come from the file (the branch-protection contexts and the ruleset
    context); a name that never reported, was skipped or was cancelled has no
    ``success`` row in ``runs`` and fails.
    """
    result = checks.get("result", {})
    if result.get("head_sha") != head:
        return _fail(f"the check-runs are for {result.get('head_sha')!r}, not {head}")
    conclusions: dict[str, set[str]] = {}
    for name, _status, conclusion, *_rest in result.get("runs", []):
        conclusions.setdefault(name, set()).add(str(conclusion))
    required = [
        line.strip()
        for line in Path(required_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    not_green = [
        name
        for name in required
        if name != SHARED_RED and "success" not in conclusions.get(name, set())
    ]
    other_red = [name for name in result.get("red", []) if name != SHARED_RED]
    if not_green or other_red or result.get("pending"):
        return _fail(
            f"not success: {not_green}; red: {other_red}; pending: {result.get('pending')}"
        )
    return 0


def mergeable(text: str, head: str) -> int:
    """Open, not a draft, at ``head``, and a merge state that is not a conflict.

    ``pr-status`` carries no conflict flag (its ``blocked`` read false on a PR whose
    merge_state read BLOCKED), so the reading is the watcher line that
    ``pr_state_local.py --pr`` prints: ``state=``, ``head=``, ``draft=`` and
    ``merge_state=`` (DIRTY, BEHIND, UNKNOWN or DRAFT fail).
    """
    lines = [line for line in text.splitlines() if " head=" in line]
    if not lines:
        return _fail("no watcher line with a head= field on stdin")
    fields = dict(part.split("=", 1) for part in lines[0].split() if "=" in part)
    if not (
        fields.get("state") == "OPEN"
        and fields.get("head") == head
        and fields.get("draft") == "False"
    ):
        return _fail(f"the watcher line does not read open, not draft, at {head}")
    if fields.get("merge_state") not in _MERGE_STATES_OK:
        return _fail(f"merge_state is {fields.get('merge_state')!r}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return _fail(
            "usage: pr_state_checks.py lab-comment|required-checks|mergeable HEAD ..."
        )
    check, head, rest = argv[1], argv[2], argv[3:]
    if check == "lab-comment":
        contains = [rest[i + 1] for i, a in enumerate(rest) if a == "--contains"]
        return lab_comment(json.load(sys.stdin), head, contains)
    if check == "required-checks":
        return required_checks(json.load(sys.stdin), head, rest[0])
    if check == "mergeable":
        return mergeable(sys.stdin.read(), head)
    return _fail(f"unknown check {check!r}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
