# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_roots_resolve_effect``: resolve the overlay root list.

Prints one line of JSON on the out stream: ``{"roots": [...], "considered": [...], "finding": ...}``.
Exit 0 when at least one root is present; exit 2 when none is present (the JSON line is still
printed and the finding goes to the err stream). No hard-coded path or name: the handler reads
them from the contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from omnimarket.models.overlay_roots import ModelOverlayRootsRequest
from omnimarket.nodes.node_overlay_roots_resolve_effect.handlers.handler_overlay_roots_resolve import (
    HandlerOverlayRootsResolve,
)
from omnimarket.nodes.node_overlay_roots_resolve_effect.protocols.protocol_overlay_roots_probe import (
    ProtocolOverlayRootsProbe,
)


def main(
    argv: Sequence[str] | None = None,
    probe: ProtocolOverlayRootsProbe | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_overlay_roots_resolve_effect")
    parser.add_argument("--registry-root", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--declared-root", action="append", default=[])
    args = parser.parse_args(argv)
    out_stream = out if out is not None else sys.stdout
    err_stream = err if err is not None else sys.stderr
    request = ModelOverlayRootsRequest(
        registry_root=args.registry_root,
        home=args.home,
        declared_roots=tuple(args.declared_root),
    )
    result = HandlerOverlayRootsResolve(probe).handle(request)
    payload = {
        "roots": [root.path for root in result.roots],
        "considered": [
            {"path": entry.path, "source": entry.source.value, "present": entry.present}
            for entry in result.considered
        ],
        "finding": result.finding,
    }
    out_stream.write(json.dumps(payload) + "\n")
    if not result.roots:
        err_stream.write((result.finding or "no overlay root is present") + "\n")
        return 2
    return 0
