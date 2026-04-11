# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_doc_freshness_sweep.

Usage:
    python -m omnimarket.nodes.node_doc_freshness_sweep
    python -m omnimarket.nodes.node_doc_freshness_sweep --repo omniclaude
    python -m omnimarket.nodes.node_doc_freshness_sweep --claude-md-only
    python -m omnimarket.nodes.node_doc_freshness_sweep --broken-only --dry-run

Outputs JSON to stdout: DocFreshnessSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep import (
    DocFreshnessSweepRequest,
    NodeDocFreshnessSweep,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Scan documentation files across ONEX repos for broken references and stale content."
    )
    parser.add_argument(
        "--omni-home",
        default="",
        help="Root path of omni_home (default: $OMNI_HOME env var)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="Scan a single repo by name (default: all repos)",
    )
    parser.add_argument(
        "--claude-md-only",
        action="store_true",
        default=False,
        help="Only check CLAUDE.md files (faster)",
    )
    parser.add_argument(
        "--broken-only",
        action="store_true",
        default=False,
        help="Only report broken references (skip stale)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview findings without saving report",
    )

    args = parser.parse_args()
    repos = [args.repo] if args.repo else None

    request = DocFreshnessSweepRequest(
        omni_home=args.omni_home,
        repos=repos,
        claude_md_only=args.claude_md_only,
        broken_only=args.broken_only,
        dry_run=args.dry_run,
    )

    handler = NodeDocFreshnessSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("healthy",):
        sys.exit(1)


if __name__ == "__main__":
    main()
