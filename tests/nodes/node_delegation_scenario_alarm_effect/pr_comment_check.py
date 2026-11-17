# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the fixer's comment for a pull request's current head from ``onex scm pr-view --source bus`` (OMN-25281).

The bus read prints one JSON answer whose ``result.comments`` is the list of the conversation comments, each with its own
``body`` and ``created_at``. The fixer's comment for a head is the newest comment whose first line is ``Lab: head=<the full
head>``. Every check below reads that one comment and no other: a review that quotes a finding number, an older head's Lab
comment and a later comment that holds the command or the exit words all fail, and a corrected second Lab comment for the
same head is the one read.

A truncated comment (``body_truncated``) or a truncated comment list (``truncated``) is refused, and a comment with no
author is refused; ``--author <login>`` also requires that login. ``--round <tag>`` reads each ``--finding N`` only on a
line that also holds the tag, so one bare ``finding 8`` cannot answer the findings of two reviews.

Usage: ``onex scm pr-view --source bus <repo>#<n> | python3 pr_comment_check.py --head <sha> [--finding N ...] [--round TAG]
[--lab] [--author LOGIN]``.
Exit 0 when every requested reading holds in that comment, 1 otherwise (the reason on stderr), 2 on a malformed answer.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence

_MARKER = re.compile(r"Lab: head=([0-9A-Za-z]+)")
LAB_COMMAND = re.compile(r"\b(?:lab-work run|lab[-]run)\b")
EXIT_CODE = re.compile(r"\bexit[ =:]+(\d+)")
FULL_HEAD = re.compile(r"^[0-9a-f]{40}$")


def read_answer(text: str) -> list[dict[str, object]]:
    """The comments of the tool's JSON answer; lines before the first ``{`` line (a binding notice) are skipped.

    A comment list the tool marks ``truncated`` is refused: the newest comment may be missing from it."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    if start is None:
        raise ValueError("no JSON answer on stdin")
    answer = json.loads("\n".join(lines[start:]))
    result = answer.get("result") if isinstance(answer, dict) else None
    comments = result.get("comments") if isinstance(result, dict) else None
    if not isinstance(comments, list):
        raise ValueError("the answer has no result.comments list")
    if isinstance(result, dict) and result.get("truncated") is not False:
        raise ValueError("the answer's comment list is truncated or does not say")
    return [c for c in comments if isinstance(c, dict)]


def fixer_comment(
    comments: Sequence[dict[str, object]], head: str, author: str | None = None
) -> str | None:
    """The body of the newest comment that opens with ``Lab: head=<head>`` (the full head), else None.

    The comment must carry a non-empty author (and be by ``author`` when one is given) and must not be truncated."""
    candidates: list[tuple[str, int, str]] = []
    for index, comment in enumerate(comments):
        body = comment.get("body")
        who = comment.get("author")
        if not isinstance(body, str) or not isinstance(who, str) or not who:
            continue
        if author is not None and who != author:
            continue
        if comment.get("body_truncated") is not False:
            continue
        marker = _MARKER.match(body.lstrip())
        if marker is not None and marker.group(1) == head:
            candidates.append((str(comment.get("created_at") or ""), index, body))
    return max(candidates)[2] if candidates else None


def _exit_zero_line(body: str) -> bool:
    """True when some line names exit 0 and names no other exit code (``exit 1, expected exit 0`` is not exit 0)."""
    for line in body.splitlines():
        codes = EXIT_CODE.findall(line)
        if codes and all(code == "0" for code in codes):
            return True
    return False


def missing_readings(
    body: str, findings: Sequence[int], *, lab: bool, round_tag: str | None = None
) -> list[str]:
    """What the one comment lacks: each finding number it does not name (on a line with ``round_tag`` when one is
    given), and the lab command or exit 0."""
    lines = body.splitlines()
    if round_tag is not None:
        tag = re.compile(rf"(?<![0-9A-Za-z]){re.escape(round_tag)}(?![0-9A-Za-z])")
        lines = [line for line in lines if tag.search(line)]
    scoped = "\n".join(lines)
    missing = [
        f"finding {n}" + (f" (round {round_tag})" if round_tag else "")
        for n in findings
        if re.search(rf"finding {n}([^0-9]|$)", scoped) is None
    ]
    if lab:
        if LAB_COMMAND.search(body) is None:
            missing.append("the lab command")
        if not _exit_zero_line(body):
            missing.append("exit 0")
    return missing


def main(argv: Sequence[str] | None = None, stdin: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr_comment_check")
    parser.add_argument("--head", required=True)
    parser.add_argument("--finding", type=int, action="append", default=[])
    parser.add_argument("--lab", action="store_true")
    parser.add_argument("--round", dest="round_tag", default=None)
    parser.add_argument("--author", default=None)
    args = parser.parse_args(argv)
    if FULL_HEAD.match(args.head) is None:
        sys.stderr.write(f"the head {args.head!r} is not a full 40-character sha\n")
        return 1
    try:
        comments = read_answer(sys.stdin.read() if stdin is None else stdin)
    except ValueError as exc:
        sys.stderr.write(f"malformed answer: {exc}\n")
        return 2
    body = fixer_comment(comments, args.head, args.author)
    if body is None:
        sys.stderr.write(f"no comment opens with 'Lab: head={args.head}'\n")
        return 1
    missing = missing_readings(
        body, args.finding, lab=args.lab, round_tag=args.round_tag
    )
    if missing:
        sys.stderr.write(
            f"the comment for head {args.head} lacks: {', '.join(missing)}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
