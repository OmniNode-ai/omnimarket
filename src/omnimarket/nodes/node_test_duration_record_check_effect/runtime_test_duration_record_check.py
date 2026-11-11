# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-line entry point for the test duration record check (OMN-23560)."""

from __future__ import annotations

import argparse
import sys

from .adapters.policy_file import load_policy
from .adapters.source_added_files_git import SourceAddedFilesGit
from .adapters.timer_pytest import TimerPytest
from .handlers.handler_test_duration_record_check import HandlerTestDurationRecordCheck
from .models.model_test_duration_record_check import (
    EnumTestDurationRecordOperation,
    ModelTestDurationRecordCheckRequest,
)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the handler, print its lines, and return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--base-ref", default=None)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--record", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    policy = load_policy()
    handler = HandlerTestDurationRecordCheck(policy, TimerPytest(policy))
    if args.record:
        request = ModelTestDurationRecordCheckRequest(
            repo_root=args.repo_root,
            added_files=tuple(args.record),
            operation=EnumTestDurationRecordOperation.RECORD,
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
        parser.error("one of --record, --staged or --base-ref is required")
    result = handler.handle(request)
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
