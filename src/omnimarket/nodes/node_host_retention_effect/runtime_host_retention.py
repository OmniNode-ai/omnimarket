# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the host retention node: ``run`` one pass."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Never

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
    RefusalError,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
)

EXIT_REFUSED = 2
EXIT_INDETERMINATE = 3


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="node_host_retention_effect", description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    run = commands.add_parser("run")
    run.add_argument("--host", required=True, help="the host's name in the overlay")
    run.add_argument(
        "--overlay-file",
        default="",
        help="the overlay file (default: the environment pin)",
    )
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--class",
        dest="classes",
        action="append",
        default=[],
        choices=("lane_scratch", "lane_results", "runner_work", "uv_cache"),
        help="restrict which classes run",
    )
    return parser


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return EXIT_INDETERMINATE
    try:
        if args.command == "run":
            return _run(args)
        return EXIT_INDETERMINATE
    except (
        NodeOverlayError,
        ValidationError,
        RefusalError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return EXIT_INDETERMINATE


def _run(args: argparse.Namespace) -> int:
    result = HandlerHostRetention().handle(
        ModelHostRetentionRequest(
            host_name=args.host,
            overlay_file=args.overlay_file,
            dry_run=args.dry_run,
            classes=tuple(args.classes),
        )
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    if result.status == "refused":
        return EXIT_REFUSED
    if result.errors:
        return EXIT_INDETERMINATE
    return 0
