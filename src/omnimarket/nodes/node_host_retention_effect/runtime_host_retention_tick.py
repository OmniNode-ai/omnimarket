# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick entry for the host retention node.

A registry-module tick the host retention systemd unit runs on a timer. It
prints one sorted-key JSON line and returns the exit code the unit records. The
probe flag answers before anything is loaded; the overlay comes only from the
deployment overlay, never from this module.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Never

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.tick_unit_arguments import write_tick_heartbeat
from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
    RefusalError,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
)

EXIT_REFUSED = 2
EXIT_INDETERMINATE = 3

_CLASS_CHOICES = (
    "lane_scratch",
    "lane_results",
    "runner_work",
    "uv_cache",
    "pre_commit_store",
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="host-retention-tick", description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--omni-home", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument(
        "--class",
        dest="classes",
        action="append",
        default=None,
        choices=_CLASS_CHOICES,
        help="restrict which classes run",
    )
    return parser


def run_once(
    *,
    host_name: str,
    dry_run: bool,
    classes: tuple[str, ...],
    handler: HandlerHostRetention | None = None,
) -> tuple[int, dict[str, object]]:
    """Run one retention pass and return the exit code and the line to print."""
    handler = handler or HandlerHostRetention()
    result = handler.handle(
        ModelHostRetentionRequest(
            host_name=host_name,
            dry_run=dry_run,
            classes=classes,
        )
    )
    errors = [str(error) for error in result.errors]
    removed = sum(totals.removed_count for totals in result.totals)
    kept = sum(totals.kept_count for totals in result.totals)
    line: dict[str, object] = {
        "status": result.status,
        "host": result.host,
        "classes": list(classes),
        "removed": removed,
        "kept": kept,
        "refusals": list(result.refusals),
        "errors": errors,
    }
    if result.status == "refused":
        code = EXIT_REFUSED
    elif errors:
        code = EXIT_INDETERMINATE
    else:
        code = 0
    return code, line


def main(argv: list[str], handler: HandlerHostRetention | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv))
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return EXIT_INDETERMINATE
    if args.probe:
        sys.stdout.write(json.dumps({"status": "probe_ok"}, sort_keys=True) + "\n")
        return 0
    host = args.host or os.environ.get("ONEX_HOST_NAME", "")
    if not host:
        line: dict[str, object] = {"status": "host_unnamed"}
        write_tick_heartbeat(args.state_dir, code=EXIT_INDETERMINATE, line=line)
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
        return EXIT_INDETERMINATE
    classes = tuple(args.classes) if args.classes else ("uv_cache",)
    try:
        code, line = run_once(
            host_name=host,
            dry_run=args.dry_run,
            classes=classes,
            handler=handler,
        )
    except (
        NodeOverlayError,
        ValidationError,
        RefusalError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        code = EXIT_INDETERMINATE
        line = {"status": "indeterminate", "detail": f"{type(exc).__name__}: {exc}"}
    write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
