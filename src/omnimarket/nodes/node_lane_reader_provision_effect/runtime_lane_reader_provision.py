# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provision the catalog find and ledger parity reader inputs on this host.

    python -m omnimarket.nodes.node_lane_reader_provision_effect.runtime_lane_reader_provision --host <name> [--check] [--restart]

Exit 0: every input is provisioned and nothing is open for the host owner. Exit 1: provisioned or would be, and an operator
step is open (a key only the host owner supplies), or ``--check`` found a change to make. Exit 2: a step failed or was refused.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .handlers.adapter_host_files import HostFiles, load_overlay, load_table
from .handlers.handler_lane_reader_provision import HandlerLaneReaderProvision
from .models.model_lane_reader_provision import ModelLaneReaderProvisionRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args(argv)
    try:
        handler = HandlerLaneReaderProvision(load_overlay(), load_table(), HostFiles())
    except (NodeOverlayError, ValidationError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 2
    result = handler.handle(
        ModelLaneReaderProvisionRequest(
            host=args.host, check_only=args.check, restart=args.restart
        )
    )
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    if not result.ok:
        return 2
    open_work = bool(result.operator_steps) or any(
        s.state == "would-change" for s in result.steps
    )
    return 1 if open_work else 0


if __name__ == "__main__":
    raise SystemExit(main())
