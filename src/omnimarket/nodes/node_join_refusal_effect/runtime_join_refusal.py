# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_join_refusal_effect <verb>``.

``run``    read the upstream failures of every declared join and write the downstream piece's typed refusals.
``probe``  the same reads, writing nothing; the reading says what is owed.

Exit codes: 0 every join was read (a refusal owed and written is still 0); 1 a join could not be read; 3 a deployment fact is
missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_join_refusal_effect.handlers.handler_join_refusal import (
    HandlerJoinRefusal,
)
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import NODE_NAME, load_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    home = os.environ.get(overlay.emission.home_env, "").strip()
    if not home:
        raise ValueError(
            f"{overlay.emission.home_env} is not set; the emission journal is relative to it"
        )
    handler = HandlerJoinRefusal(
        overlay=overlay,
        source=build_row_source(overlay.ledger_read, os.environ),
        sink=JsonlEmissionSink(Path(home) / overlay.emission.journal_relpath),
        backend="real",
    )
    result = handler.handle(
        ModelJoinRefusalCommand(
            write=args.verb == "run", only_joins=tuple(args.join or ())
        )
    )
    for r in result.readings:
        sys.stdout.write(
            f"{r.join} piece {r.from_piece} to {r.to_piece}: {r.status} "
            f"failures={r.upstream_failures} drills_skipped={r.drills_skipped} "
            f"answered={r.answered} owed={r.owed} written={r.written}"
            f"{' reason=' + r.reason if r.reason else ''}\n"
        )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "probe"))
    parser.add_argument("--join", action="append")
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
