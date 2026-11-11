# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_join_j0_effect <verb>``.

``run``    read the floor record, write the join's two chains to the emission journal, and prove the record reader
            breaks on the control's deliberately corrupt record.
``probe``  the same reads, writing nothing.

Exit codes: 0 the join ran (a golden or an error join is a recorded result) and the control broke; 1 the control did
not break (the reader accepted the deliberately corrupt record); 3 a deployment fact is missing or the join's from
node is not one the floor reads (INDETERMINATE).
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
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    load_runtime,
)
from omnimarket.nodes.node_join_j0_effect.handlers.handler_join_j0 import (
    HandlerJoinJ0,
    refuse_unjudged_join_end,
)
from omnimarket.nodes.node_join_j0_effect.models import ModelJoinJ0Command
from omnimarket.nodes.node_join_j0_effect.overlay import NODE_NAME, load_overlay


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    runtime = load_runtime()
    refuse_unjudged_join_end(
        overlay.join.join,
        overlay.join.from_node,
        runtime.pipeline_chains,
        runtime.join_ends,
    )
    home = os.environ.get(overlay.emission.home_env, "").strip()
    if not home:
        raise ValueError(
            f"{overlay.emission.home_env} is not set; the journal and the floor record are relative to it"
        )
    handler = HandlerJoinJ0(
        overlay=overlay,
        sink=JsonlEmissionSink(Path(home) / overlay.emission.journal_relpath),
        floor_record_path=Path(home) / overlay.emission.floor_record_relpath,
        backend="real",
    )
    result = handler.handle(ModelJoinJ0Command(write=args.verb == "run"))
    join = overlay.join
    sys.stdout.write(
        f"{join.join} piece {join.from_piece} to {join.to_piece}: {result.outcome} "
        f"correlation_id={result.correlation_id} wrote={result.wrote} "
        f"control={result.control} "
        f"control_correlation_id={result.control_correlation_id} "
        f"floor_rows={result.floor_rows}"
        f"{' failure_code=' + result.failure_code if result.failure_code else ''}\n"
    )
    if result.control == "did_not_break":
        sys.stderr.write("control-did-not-break\n")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "probe"))
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (
        NodeOverlayError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
