# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick entry for the lab disk hygiene node (OMN-23707).

A registry-module tick the host's disk hygiene systemd unit runs on a timer. It
prints one sorted-key JSON line and returns the exit code the unit records. The
probe flag answers before anything is loaded; the census path comes only from
the deployment overlay, never from this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from omnimarket.handlers.tick_unit_arguments import (
    write_tick_heartbeat,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.handlers.handler_lab_disk_hygiene import (
    HandlerLabDiskHygiene,
    LabDiskHygieneConfigurationError,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.models.model_lab_disk_hygiene_request import (
    ModelLabDiskHygieneRequest,
)


def run_once(
    *,
    registry_root: Path,
    dry_run: bool,
    handler: HandlerLabDiskHygiene | None = None,
) -> tuple[int, dict[str, object]]:
    """Run one hygiene pass and return the exit code and the line to print."""
    handler = handler or HandlerLabDiskHygiene()
    result = handler.handle(ModelLabDiskHygieneRequest(registry_root, dry_run=dry_run))
    errors = [str(error) for error in result.errors]
    line: dict[str, object] = {
        "status": "ok" if not errors else "failed",
        "docker": result.docker,
        "removed_containers": len(result.removed_containers),
        "kept_containers": len(result.kept_containers),
        "ghost_containers": len(result.ghost_containers),
        "errors": errors,
        "line": result.line(),
    }
    return (0 if not errors else 1), line


def main(argv: list[str], handler: HandlerLabDiskHygiene | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lab-disk-hygiene-tick")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--omni-home", dest="registry_root", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": "probe_ok"}, sort_keys=True) + "\n")
        return 0
    if not args.registry_root:
        parser.error("--omni-home is required unless --probe is given")
    try:
        code, line = run_once(
            registry_root=args.registry_root, dry_run=args.dry_run, handler=handler
        )
    except LabDiskHygieneConfigurationError as exc:
        code, line = 3, {"status": "overlay_refused", "detail": str(exc)}
    write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code
