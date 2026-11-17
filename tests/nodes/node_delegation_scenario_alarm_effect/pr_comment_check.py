# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the fixer's comment for a pull request's current head from ``onex scm pr-view --source bus`` (OMN-25281).

The bus read prints one JSON answer whose ``result.comments`` is the list of the conversation comments, each with its own
``body`` and ``created_at``. The fixer's comment for a head is the newest comment whose first line is ``Lab: head=<the full
head>``. Every check below reads that one comment and no other: a review that quotes a finding number, an older head's Lab
comment and a later comment that holds the command or the exit words all fail, and a corrected second Lab comment for the
same head is the one read.

Usage: ``onex scm pr-view --source bus <repo>#<n> | python3 pr_comment_check.py --head <sha> [--finding N ...] [--lab]``.
Exit 0 when every requested reading holds in that comment, 1 otherwise (the reason on stderr), 2 on a malformed answer.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence

_MARKER = re.compile(r"Lab: head=([0-9A-Za-z]+)")
LAB_COMMAND = r"lab.work run|lab.run"
EXIT_ZERO = r"exit[ =:]+0([^0-9]|$)"
FULL_HEAD = re.compile(r"^[0-9a-f]{40}$")


def read_answer(text: str) -> list[dict[str, object]]:
    """The comments of the tool's JSON answer; lines before the first ``{`` line (a binding notice) are skipped."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    if start is None:
        raise ValueError("no JSON answer on stdin")
    answer = json.loads("\n".join(lines[start:]))
    result = answer.get("result") if isinstance(answer, dict) else None
    comments = result.get("comments") if isinstance(result, dict) else None
    if not isinstance(comments, list):
        raise ValueError("the answer has no result.comments list")
    return [c for c in comments if isinstance(c, dict)]


def fixer_comment(comments: Sequence[dict[str, object]], head: str) -> str | None:
    """The body of the newest comment that opens with ``Lab: head=<head>`` (the full head), else None."""
    candidates: list[tuple[str, int, str]] = []
    for index, comment in enumerate(comments):
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        marker = _MARKER.match(body.lstrip())
        if marker is not None and marker.group(1) == head:
            candidates.append((str(comment.get("created_at") or ""), index, body))
    return max(candidates)[2] if candidates else None


def missing_readings(body: str, findings: Sequence[int], *, lab: bool) -> list[str]:
    """What the one comment lacks: each finding number it does not name, and the lab command or exit 0."""
    missing = [
        f"finding {n}"
        for n in findings
        if re.search(rf"finding {n}([^0-9]|$)", body) is None
    ]
    if lab:
        if re.search(LAB_COMMAND, body) is None:
            missing.append("the lab command")
        if re.search(EXIT_ZERO, body) is None:
            missing.append("exit 0")
    return missing


def main(argv: Sequence[str] | None = None, stdin: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr_comment_check")
    parser.add_argument("--head", required=True)
    parser.add_argument("--finding", type=int, action="append", default=[])
    parser.add_argument("--lab", action="store_true")
    args = parser.parse_args(argv)
    if FULL_HEAD.match(args.head) is None:
        sys.stderr.write(f"the head {args.head!r} is not a full 40-character sha\n")
        return 1
    try:
        comments = read_answer(sys.stdin.read() if stdin is None else stdin)
    except ValueError as exc:
        sys.stderr.write(f"malformed answer: {exc}\n")
        return 2
    body = fixer_comment(comments, args.head)
    if body is None:
        sys.stderr.write(f"no comment opens with 'Lab: head={args.head}'\n")
        return 1
    missing = missing_readings(body, args.finding, lab=args.lab)
    if missing:
        sys.stderr.write(
            f"the comment for head {args.head} lacks: {', '.join(missing)}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
