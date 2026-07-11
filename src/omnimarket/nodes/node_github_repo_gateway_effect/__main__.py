# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_github_repo_gateway_effect — the typed status reader.

Runs one read operation against a repo (and optionally a PR) and prints ONE
small typed JSON object to stdout — a compact, structured replacement for
``gh pr checks/view --json`` in a verify-before-accept loop.

Usage::

    python -m omnimarket.nodes.node_github_repo_gateway_effect \
        --operation pr_status --repo OmniNode-ai/omnimarket --pr 1683

    python -m omnimarket.nodes.node_github_repo_gateway_effect \
        --operation open_prs_list --repo OmniNode-ai/omnimarket

The GitHub token is resolved from the contract-declared ``secrets.GITHUB_TOKEN``
ref via the canonical secret-store resolver — no raw env read, no ``gh`` shell.
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_github_repo_gateway_effect.token_resolver import (
    resolve_github_token,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    RealGitHubReadTransport,
)

_OPERATIONS = [op.value for op in EnumGithubGatewayOperation]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="node_github_repo_gateway_effect",
        description="Typed GitHub repo status reader — one small typed object out.",
    )
    parser.add_argument(
        "--operation",
        required=True,
        choices=_OPERATIONS,
        help="Which read operation to run.",
    )
    parser.add_argument("--repo", required=True, help="GitHub repo slug (org/name).")
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number (required for PR-scoped operations).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON (default: compact one line).",
    )
    args = parser.parse_args(argv)

    request = ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation(args.operation),
        repo=args.repo,
        pr_number=args.pr,
    )

    token = resolve_github_token()
    transport = RealGitHubReadTransport(token)
    result = dispatch(request, transport)

    payload = result.model_dump(mode="json")
    if args.pretty:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
