# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pass of the landing tick orchestrator on its real backend (OMN-23514): observe mode only.

    python -m omnimarket.nodes.node_pr_landing_tick_orchestrator

Reads the overlay (<root>/node_pr_landing_tick_orchestrator/overlay.yaml under
ONEX_SKILL_OVERLAY_ROOTS), reads the recorded tick snapshots the overlay names (the landing
snapshot step's records on its topic, or the injected in-memory ones), and compares each one
through the node path against the decision the script recorded: one compared or failed event per
tick on the contract's topics, and one chain per tick in the floor's emission journal. This
runtime never writes: whatever the overlay says it runs observe mode and never constructs a
writer, and an overlay that enables act mode is refused outright.

Exit 0: every snapshot compared, or none arrived. Exit 1: any snapshot ended in the typed failed
event. Exit 3: the overlay could not be read, act mode was enabled, or the source could not be
read; nothing was compared.
"""

from __future__ import annotations

import json
import sys

from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    PublisherFactory,
    TopicReaderFactory,
    build_emission,
    build_publisher,
    build_snapshot_source,
    load_tick_overlay,
    run_snapshots,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
)


def run_once(
    overlay: ModelTickOrchestratorOverlay,
    *,
    topic_reader_factory: TopicReaderFactory | None = None,
    publisher: ProtocolTickPublisher | None = None,
    publisher_factory: PublisherFactory | None = None,
) -> tuple[int, dict[str, object]]:
    """One pass: the exit code and the line it prints."""
    if overlay.act_enabled:
        return 3, {
            "status": "act_refused",
            "detail": "this runtime runs observe mode only and never constructs a writer",
        }
    if publisher is None:
        publisher = build_publisher(overlay, publisher_factory=publisher_factory)
    source = build_snapshot_source(overlay, topic_reader_factory=topic_reader_factory)
    sink, label = build_emission(overlay)
    handler = HandlerPrLandingTickOrchestrator(
        publisher=publisher,
        act_enabled=False,
        emission_sink=sink,
        floor_piece=overlay.floor_piece,
        emission_backend=label,
        cutover_repository=overlay.cutover_repository,
        script_out_of_scope=overlay.script_out_of_scope_repositories,
    )
    try:
        results = run_snapshots(handler, source, EnumTickMode.OBSERVE)
    except Exception as exc:  # the reader raised, or a record did not validate
        return 3, {"status": "source_unread", "detail": type(exc).__name__}
    failed = sum(result.status is EnumTickStatus.FAILED for result in results)
    line: dict[str, object] = {
        "status": ("no_records" if not results else "failed" if failed else "compared"),
        "mode": "observe",
        "records": len(results),
        "compared": sum(result.status is EnumTickStatus.COMPARED for result in results),
        "failed": failed,
        "equal": sum(result.equal for result in results),
        "ticks": [result.tick for result in results],
        "divergent": sorted(
            {d.subject for result in results for d in result.divergences}
        ),
    }
    return (1 if failed else 0), line


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        overlay = load_tick_overlay()
        code, line = run_once(overlay)
    except ValueError as exc:
        code, line = 3, {"status": "overlay_refused", "detail": str(exc)}
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__: list[str] = ["main", "run_once"]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
