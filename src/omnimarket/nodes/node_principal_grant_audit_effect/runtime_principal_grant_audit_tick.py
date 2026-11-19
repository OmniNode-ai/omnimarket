# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick entry for the principal grant audit node (OMN-21622).

A registry-module tick the host's audit systemd unit runs on a timer. It prints one
sorted-key JSON line and returns the exit code the unit records. The probe flag
answers before anything is loaded; every path, name and timeout comes only from the
deployment overlay, never from this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from omnimarket.handlers.tick_unit_arguments import (
    write_tick_heartbeat,
)
from omnimarket.nodes.node_principal_grant_audit_effect.handlers.handler_principal_grant_audit import (
    HandlerPrincipalGrantAudit,
    PrincipalGrantAuditConfigurationError,
)
from omnimarket.nodes.node_principal_grant_audit_effect.models.model_principal_grant_audit_request import (
    ModelPrincipalGrantAuditRequest,
)


def run_once(
    *,
    registry_root: Path,
    dry_run: bool,
    handler: HandlerPrincipalGrantAudit | None = None,
) -> tuple[int, dict[str, object]]:
    """Run one audit pass and return the exit code and the line to print."""
    handler = handler or HandlerPrincipalGrantAudit()
    result = handler.handle(
        ModelPrincipalGrantAuditRequest(registry_root, dry_run=dry_run)
    )
    errors = [str(error) for error in result.errors]
    line: dict[str, object] = {
        "status": result.status,
        "exit_code": result.exit_code,
        "principals": len(result.principals),
        "missing": len(result.missing_lines),
        "extra": len(result.extra_lines),
        "principal_lines": [
            f"principal={held.principal} expected={held.expected} live={held.live} "
            f"missing={held.missing} extra={held.extra}"
            for held in result.principals
        ],
        "missing_lines": list(result.missing_lines),
        "extra_lines": list(result.extra_lines),
        "errors": errors,
        "line": result.line(),
    }
    return result.exit_code, line


def main(argv: list[str], handler: HandlerPrincipalGrantAudit | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="principal-grant-audit-tick")
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
    except PrincipalGrantAuditConfigurationError as exc:
        code, line = 3, {"status": "overlay_refused", "detail": str(exc)}
    held_lines = [
        str(held)
        for key in ("principal_lines", "missing_lines", "extra_lines")
        for held in cast("list[object]", line.pop(key, []))
    ]
    for held in held_lines:
        sys.stdout.write(held + "\n")
    write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code
