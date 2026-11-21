# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_review_lock_effect``: the review workflow's two calls.

``acquire`` takes the review lock of one pull request head, recorded on the ledger as one STATUS
row written through the ledger writer. ``check-scope`` decides whether a fixer push that added files
is held for review. Each prints one JSON object on stdout and one line on stderr.

Exit codes of ``acquire``: 0 acquired (the lock row is written; dispatch the review); 3 refused
(the head already holds a lock or a verdict; stdout carries the first one's pointer); 4 undecided
(the ledger could not be read or the row could not be written; do not dispatch); 2 bad arguments.

Exit codes of ``check-scope``: 0 release (no added file outside the scope, or every added file is
named with a finding); 3 hold (stdout lists the added files left unnamed); 2 bad arguments.

The ledger file is the required ``--ledger`` argument (the workflow passes its own ledger path); the writer command is
the required ``$ONEX_LEDGER_APPEND_CMD`` (split like a shell line), which the
workflow sets to the same ledger writer command it uses for its own rows.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_review_lock_effect.handlers.adapter_ledger import (
    CommandLedgerRowWriter,
    FileLedgerStatusReader,
)
from omnimarket.nodes.node_review_lock_effect.handlers.handler_review_lock import (
    HandlerReviewLock,
)
from omnimarket.nodes.node_review_lock_effect.handlers.handler_scope_check import (
    HandlerScopeCheck,
)
from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    ModelReviewLockRequest,
    ModelScopeCheckRequest,
)
from omnimarket.nodes.node_review_lock_effect.protocols import (
    ProtocolLedgerRowWriter,
    ProtocolLedgerStatusReader,
)

EXIT_USAGE = 2


def _read_lines(path: str) -> tuple[str, ...]:
    text = Path(path).read_text(encoding="utf-8")
    return tuple(line.strip() for line in text.splitlines() if line.strip())


_GIT_LOCATION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    }
)


def _git_files(repo_dir: str, base: str, head: str) -> tuple[str, ...]:
    """The files a head changed against the base, read from a local clone (no network)."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    done = subprocess.run(
        ["git", "-C", repo_dir, "diff", "--name-only", base, head],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if done.returncode != 0:
        raise OSError(f"git diff {base} {head} failed: {done.stderr.strip()}")
    return tuple(line for line in done.stdout.splitlines() if line)


def _writer_command(env: Mapping[str, str]) -> list[str]:
    """The ledger writer command: ``$ONEX_LEDGER_APPEND_CMD`` split like a shell line."""
    return shlex.split(env.get("ONEX_LEDGER_APPEND_CMD", "").strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node_review_lock_effect", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)
    acq = sub.add_parser(
        "acquire", help="take the review lock of one pull request head"
    )
    acq.add_argument(
        "--repo", required=True, help="repository name, e.g. omnibase_infra"
    )
    acq.add_argument("--pr", required=True, type=int, help="pull request number")
    acq.add_argument(
        "--head", required=True, help="pull request head sha (12 or 40 hex)"
    )
    acq.add_argument(
        "--tag", required=True, help="dispatch tag of this review, e.g. 1448"
    )
    acq.add_argument(
        "--ledger", required=True, help="the ledger file the workflow reads and writes"
    )
    acq.add_argument(
        "--verdict-dir", help="directory of verdict-<repo>-<n>-<tag>.md files"
    )
    acq.add_argument(
        "--lock-ttl-minutes",
        type=int,
        help="a lock with no verdict older than this is expired (default: never)",
    )
    acq.add_argument(
        "--dry-run", action="store_true", help="decide without writing the row"
    )
    scope = sub.add_parser("check-scope", help="hold a fixer push that adds files")
    scope.add_argument(
        "--first-files", help="file: one path per line, the first reviewed head"
    )
    scope.add_argument("--new-files", help="file: one path per line, the new head")
    scope.add_argument(
        "--repo-dir", help="local clone to read the file sets from with git diff"
    )
    scope.add_argument(
        "--base", help="with --repo-dir: the merge base of the pull request"
    )
    scope.add_argument("--first-head", help="with --repo-dir: the first reviewed head")
    scope.add_argument("--new-head", help="with --repo-dir: the new head")
    scope.add_argument("--comment-file", help="file holding the fixer comment text")
    scope.add_argument("--comment", help="the fixer comment text")
    scope.add_argument(
        "--contract", help="the ticket's contracts/OMN-<n>.yaml (scope source)"
    )
    scope.add_argument(
        "--finding-pattern", help="regex of a finding token (default built in)"
    )
    return parser


def _emit(payload: dict[str, object], line: str) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stderr.write(line + "\n")


def _acquire(
    args: argparse.Namespace,
    env: Mapping[str, str],
    reader: ProtocolLedgerStatusReader | None,
    writer: ProtocolLedgerRowWriter | None,
    clock: Callable[[], datetime],
) -> int:
    ledger = Path(args.ledger)
    reader = reader or FileLedgerStatusReader(ledger)
    if writer is None:
        command = _writer_command(env)
        if not command:
            sys.stderr.write(
                "acquire: ONEX_LEDGER_APPEND_CMD names no ledger writer command\n"
            )
            return EXIT_USAGE
        writer = CommandLedgerRowWriter(command, ledger)
    request = ModelReviewLockRequest(
        repo=args.repo,
        pr=args.pr,
        head=args.head,
        tag=args.tag,
        verdict_dir=args.verdict_dir,
        lock_ttl_minutes=args.lock_ttl_minutes,
        dry_run=args.dry_run,
    )
    result = HandlerReviewLock(reader, writer, clock).handle(request)
    _emit(
        result.model_dump(mode="json") | {"exit_code": result.exit_code},
        f"REVIEW-LOCK {result.outcome.value} {result.key} {result.detail}".rstrip(),
    )
    return result.exit_code


def _check_scope(args: argparse.Namespace) -> int:
    if args.repo_dir:
        if not (args.base and args.first_head and args.new_head):
            sys.stderr.write(
                "check-scope: --repo-dir needs --base, --first-head and --new-head\n"
            )
            return EXIT_USAGE
        first = _git_files(args.repo_dir, args.base, args.first_head)
        new = _git_files(args.repo_dir, args.base, args.new_head)
    elif args.first_files and args.new_files:
        first, new = _read_lines(args.first_files), _read_lines(args.new_files)
    else:
        sys.stderr.write(
            "check-scope: give --first-files and --new-files, or --repo-dir\n"
        )
        return EXIT_USAGE
    if args.comment_file:
        comment = Path(args.comment_file).read_text(encoding="utf-8")
    else:
        comment = args.comment or ""
    contract_text = (
        Path(args.contract).read_text(encoding="utf-8") if args.contract else None
    )
    result = HandlerScopeCheck().handle(
        ModelScopeCheckRequest(
            first_files=first,
            new_files=new,
            comment=comment,
            contract_path=args.contract,
            contract_text=contract_text,
            finding_pattern=args.finding_pattern,
        )
    )
    _emit(
        result.model_dump(mode="json") | {"exit_code": result.exit_code},
        f"SCOPE-CHECK {result.verdict.value} source={result.scope_source}"
        f" added={len(result.added_files)} unnamed={len(result.unnamed_files)}",
    )
    return result.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    reader: ProtocolLedgerStatusReader | None = None,
    writer: ProtocolLedgerRowWriter | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    env = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        if args.command == "acquire":
            return _acquire(
                args, env, reader, writer, clock or (lambda: datetime.now(UTC))
            )
        return _check_scope(args)
    except (ValidationError, OSError) as exc:
        sys.stderr.write(f"{args.command}: {exc}\n")
        return EXIT_USAGE
