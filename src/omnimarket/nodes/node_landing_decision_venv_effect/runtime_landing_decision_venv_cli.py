# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verbs of the landing decision environment node: ``build`` and ``verify``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .handlers.handler_landing_decision_venv import HandlerLandingDecisionVenv
from .models import ModelLandingDecisionVenvRequest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onex-landing-decision-venv")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        verb = commands.add_parser(name)
        verb.add_argument("--pin-file", type=Path, required=True)
        verb.add_argument("--venv-dir", type=Path, required=True)
        verb.add_argument("--node-src", type=Path)
        verb.add_argument("--runtime-file", type=Path)
        if name == "build":
            verb.add_argument("--base-python", type=Path, required=True)
    args = parser.parse_args(argv)
    result = HandlerLandingDecisionVenv().handle(
        ModelLandingDecisionVenvRequest(
            operation=args.command,
            pin_file=args.pin_file.expanduser(),
            venv_dir=args.venv_dir.expanduser(),
            base_python=getattr(args, "base_python", None),
            node_src=args.node_src,
            runtime_file=args.runtime_file,
        )
    )
    sys.stdout.write(result.line() + "\n")
    for reason in result.reasons:
        sys.stdout.write(reason + "\n")
    return int(result.status == "refused")


if __name__ == "__main__":
    raise SystemExit(main())
