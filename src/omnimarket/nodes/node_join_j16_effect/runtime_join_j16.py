# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_join_j16_effect <verb>``.

``run``    read the eval piece's result and the floor record, write the join's two chains to the emission journal.
``probe``  the same reads, writing nothing.

Exit codes: 0 the join ran (a golden or an error join is a recorded result); 1 the ledger could not be read; 3 a deployment fact
is missing (INDETERMINATE).
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
from omnimarket.nodes.node_join_j16_effect.handlers.handler_join_j16 import (
    HandlerJoinJ16,
)
from omnimarket.nodes.node_join_j16_effect.models import ModelJoinJ16Command
from omnimarket.nodes.node_join_j16_effect.overlay import NODE_NAME, load_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    home = os.environ.get(overlay.emission.home_env, "").strip()
    if not home:
        raise ValueError(
            f"{overlay.emission.home_env} is not set; the journal and the floor record are relative to it"
        )
    handler = HandlerJoinJ16(
        overlay=overlay,
        source=build_row_source(overlay.ledger_read, os.environ),
        sink=JsonlEmissionSink(Path(home) / overlay.emission.journal_relpath),
        floor_record_path=Path(home) / overlay.emission.floor_record_relpath,
        backend="real",
    )
    result = handler.handle(ModelJoinJ16Command(write=args.verb == "run"))
    join = overlay.join
    sys.stdout.write(
        f"{join.join} piece {join.from_piece} to {join.to_piece}: {result.outcome} "
        f"correlation_id={result.correlation_id} wrote={result.wrote} "
        f"reading_age_s={result.reading_age_s}"
        f"{' failed_side=' + result.failed_side if result.failed_side else ''}"
        f"{' failure_code=' + result.failure_code if result.failure_code else ''}"
        f"{' detail=' + result.detail if result.outcome == 'unread' else ''}\n"
    )
    return 1 if result.outcome == "unread" else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "probe"))
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
