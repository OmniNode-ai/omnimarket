# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_memory_pilot_effect.runtime_memory_pilot_run [--probe]``.

One run of the frozen memory pilot: both arms, the measured result written to the measurement node's result port.
``--probe`` loads the overlay and prints ``PROBE ok``: it builds nothing, calls nothing and writes nothing (the host
installer runs a probe before loading the unit). ``--omni-home`` and ``--state-dir`` are the installer's tick
arguments; the run takes every path from its overlay.

Exit codes: 0 the run completed and its result was written; 1 the run failed (typed); 3 a deployment fact is
missing or invalid (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot_run import (
    HandlerMemoryPilotRun,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.journal_event_publisher import (
    JournalEventPublisher,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotOverlay,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    ModelMemoryPilotRunRequest,
)

NODE_NAME = "node_memory_pilot_effect"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        overlay = load_structured_overlay(NODE_NAME, ModelMemoryPilotOverlay)
        if overlay.run is None:
            raise ValueError("the overlay carries no run settings")
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    if args.probe:
        sys.stdout.write("PROBE ok\n")
        return 0
    handler = HandlerMemoryPilotRun(
        event_bus=JournalEventPublisher(overlay.run.event_journal), overlay=overlay
    )
    result = asyncio.run(
        handler.handle(ModelMemoryPilotRunRequest(correlation_id=uuid4()))
    )
    sys.stdout.write(
        f"MEMORY-PILOT-RUN status={result.status} "
        f"error={result.error_code.value if result.error_code else '-'} "
        f"p95={result.query_p95_seconds:.1f} ingest={result.ingest_seconds:.1f} "
        f"written={result.result_written} detail={result.error_detail or '-'}\n"
    )
    for name, value in sorted(result.metrics.items()):
        sys.stdout.write(f"METRIC {name}={value}\n")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
