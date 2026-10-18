# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_claude_token_pool_effect (OMN-17427).

Usage, on the lab host whose pool it is:
    python -m omnimarket.nodes.node_claude_token_pool_effect add --label <account>
        reads the pasted ``claude setup-token`` output on stdin (a terminal gets
        a hidden prompt for the token line); prints the label and fingerprint
    python -m omnimarket.nodes.node_claude_token_pool_effect resolve
        writes the chosen token to stdout for ``export X="$(...)"`` and refuses a
        terminal; the status line, with the chosen label, goes to stderr
    python -m omnimarket.nodes.node_claude_token_pool_effect mark-limited --label <account> --seconds <n>
    python -m omnimarket.nodes.node_claude_token_pool_effect status [--json]

Exit 0 on success, 2 on a refusal, 3 when ``resolve`` finds an empty pool and 4
when every token is limited (``resolve`` chose nothing; ``mark-limited`` and
``status`` print the ALARM line). No output names a token's value.
"""

from __future__ import annotations

import argparse
import sys
from getpass import getpass

from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    ClaudeTokenPoolRefusedError,
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)

EXIT_REFUSED = 2
EXIT_EMPTY = 3
EXIT_ALL_LIMITED = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node_claude_token_pool_effect")
    verbs = parser.add_subparsers(dest="verb", required=True)
    add = verbs.add_parser(
        "add", help="store a token from `claude setup-token` output read on stdin"
    )
    add.add_argument("--label", required=True, help="the account the token belongs to")
    add.add_argument(
        "--force", action="store_true", help="replace the token stored under this label"
    )
    verbs.add_parser(
        "resolve", help="write the first unlimited token to stdout (never a terminal)"
    )
    mark = verbs.add_parser(
        "mark-limited", help="the account behind --label hit its usage limit"
    )
    mark.add_argument("--label", required=True)
    mark.add_argument("--seconds", required=True, type=float)
    status = verbs.add_parser("status", help="print the pool status line")
    status.add_argument(
        "--json", action="store_true", help="the whole result as JSON (no token)"
    )
    return parser


def _pasted() -> str:
    if sys.stdin.isatty():
        return getpass(
            "Paste the token line `claude setup-token` printed (input hidden): "
        )
    return sys.stdin.read()


def main(
    argv: list[str] | None = None, handler: HandlerClaudeTokenPool | None = None
) -> int:
    args = _parser().parse_args(argv)
    if args.verb == "resolve" and sys.stdout.isatty():
        sys.stderr.write(
            'refusing to write a token to a terminal; capture it: export X="$(... resolve)"\n'
        )
        return EXIT_REFUSED
    operation = args.verb.replace("-", "_")
    try:
        request = ModelClaudeTokenPoolRequest(
            operation=operation,
            label=getattr(args, "label", None),
            setup_token_output=SecretStr(_pasted()) if args.verb == "add" else None,
            seconds=getattr(args, "seconds", None),
            force=getattr(args, "force", False),
        )
        result = (handler or HandlerClaudeTokenPool()).handle(request)
    except (ClaudeTokenPoolRefusedError, ValueError) as refusal:
        sys.stderr.write(f"claude-pool refused: {refusal}\n")
        return EXIT_REFUSED
    if args.verb == "resolve":
        if result.token is None:
            sys.stderr.write(result.status_line + " chosen=none\n")
            return EXIT_EMPTY if not result.tokens else EXIT_ALL_LIMITED
        sys.stderr.write(f"{result.status_line} label={result.chosen}\n")
        sys.stdout.write(result.token.get_secret_value())
        return 0
    if args.verb == "add":
        added = next(t for t in result.tokens if t.label == args.label)
        sys.stdout.write(
            f"claude-pool added label={added.label} fingerprint={added.fingerprint}\n"
        )
    if getattr(args, "json", False):
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(result.status_line + "\n")
    return EXIT_ALL_LIMITED if result.all_limited and args.verb == "mark-limited" else 0


if __name__ == "__main__":
    raise SystemExit(main())
