# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-line entry point for the test duration record check (OMN-23560)."""

from __future__ import annotations

import argparse
import subprocess
import sys

from .adapters.policy_file import load_policy
from .adapters.source_added_files_git import SourceAddedFilesGit
from .adapters.source_tree_snapshot_git import SourceTreeSnapshotGit
from .adapters.timer_pytest import TimerPytest
from .handlers.handler_test_duration_record_check import HandlerTestDurationRecordCheck
from .models.model_test_duration_record_check import (
    EnumTestDurationRecordOperation,
    ModelTestDurationRecordCheckRequest,
)


def _push_commits(
    repo_root: str, before: str | None, head: str
) -> tuple[str, ...] | None:
    """Return the commits a push to the branch added, oldest first, or ``None`` when git failed.

    A push with no readable ``before`` (a new branch, or the all-zero sha) reads only the head.
    """
    if before is None or set(before) == {"0"}:
        return (head,)
    completed = subprocess.run(
        [
            "git",
            "-C",
            repo_root,
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{before}..{head}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    commits = tuple(line for line in completed.stdout.splitlines() if line.strip())
    return commits or (head,)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the handler, print its lines, and return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--base-ref", default=None)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--record", nargs="+", metavar="FILE")
    parser.add_argument(
        "--sidecar",
        action="store_true",
        help="with --record: write one entry file per test file instead of editing the shared list",
    )
    parser.add_argument(
        "--freshness",
        action="store_true",
        help="post-merge: name the first commit of the push that reaches the unrecorded limit",
    )
    parser.add_argument("--before", default=None, help="the branch tip before the push")
    parser.add_argument("--head", default="HEAD", help="the tip after the push")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    policy = load_policy()
    handler = HandlerTestDurationRecordCheck(
        policy, TimerPytest(policy), SourceTreeSnapshotGit(policy)
    )
    if args.freshness:
        commits = _push_commits(args.repo_root, args.before, args.head)
        if commits is None:
            sys.stdout.write(
                "test-duration-record: cannot list the push's commits from git\n"
            )
            return 2
        request = ModelTestDurationRecordCheckRequest(
            repo_root=args.repo_root,
            operation=EnumTestDurationRecordOperation.FRESHNESS,
            commits=commits,
            before_sha=args.before,
            limit=args.limit,
        )
    elif args.record:
        request = ModelTestDurationRecordCheckRequest(
            repo_root=args.repo_root,
            added_files=tuple(args.record),
            operation=EnumTestDurationRecordOperation.RECORD,
            sidecar=args.sidecar,
        )
    elif args.staged or args.base_ref is not None:
        files = SourceAddedFilesGit().added_files(
            args.repo_root, None if args.staged else args.base_ref
        )
        if files is None:
            sys.stdout.write(
                "test-duration-record: cannot read the change's added files from git\n"
            )
            return 2
        request = ModelTestDurationRecordCheckRequest(
            repo_root=args.repo_root,
            added_files=files,
            operation=EnumTestDurationRecordOperation.CHECK,
        )
    else:
        parser.error("one of --record, --staged, --base-ref or --freshness is required")
    result = handler.handle(request)
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
