# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_bringup_run_effect --host-name H [--install]``.

The default is a dry run. The exit code is the first failed step's, or 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_bringup_run_effect.handlers.adapter_installer import (
    CliHostInstaller,
    FileDeployStore,
    StreamEventPublisher,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.handler_host_bringup_run import (
    HandlerHostBringupRun,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.overlay import load_overlay
from omnimarket.nodes.node_host_bringup_run_effect.models import (
    EnumBringupMode,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
)


def render(result: ModelHostBringupRunResult) -> list[str]:
    lines = [
        f"BRING-UP {result.mode.value} host={result.host_name} role={result.role} "
        f"steps={len(result.steps)}"
    ]
    for step in result.steps:
        rc = "" if step.exit_code is None else f" rc={step.exit_code}"
        lines.append(
            f"STEP {step.number} {step.unit} {step.installer_mode or '-'} "
            f"{step.outcome.value}{rc} {step.detail}".rstrip()
        )
    if result.refusal:
        lines.append(f"REFUSED {result.refusal}")
    if result.stopped_at is not None:
        lines.append(
            f"STOPPED at step {result.stopped_at}; {result.rollback_hint or ''}".rstrip()
        )
    elif result.completed:
        lines.append(
            f"BRING-UP {result.mode.value} complete changed_host={result.changed_host}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="node_host_bringup_run_effect", allow_abbrev=False
    )
    parser.add_argument("--host-name", required=True)
    parser.add_argument("--role", default="linux-lab")
    for mode in EnumBringupMode:
        parser.add_argument(
            "--" + mode.value, action="store_const", dest="mode", const=mode
        )
    parser.set_defaults(mode=EnumBringupMode.DRY_RUN)
    args = parser.parse_args(argv)
    try:
        overlay = load_overlay()
    except NodeOverlayError as exc:
        sys.stderr.write(f"node_host_bringup_run_effect: {exc}\n")
        return 66
    store = Path(overlay.deploy_store_file) if overlay.deploy_store_file else None
    handler = HandlerHostBringupRun(
        overlay=overlay,
        installer=CliHostInstaller(
            overlay.installer_command, overlay.repo_root, overlay.step_timeout_seconds
        ),
        deploy_store=FileDeployStore(store),
        publisher=StreamEventPublisher(),
        environ=os.environ,
    )
    result = handler.handle(
        ModelHostBringupRunRequest(
            host_name=args.host_name, role=args.role, mode=args.mode
        )
    )
    for line in render(result):
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
