# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pass of the landing watcher facts node on its file backend (OMN-23507): the unit's tick.

    python -m omnimarket.nodes.node_pr_landing_watcher_facts_effect

Reads the overlay (<root>/node_pr_landing_watcher_facts_effect/overlay.yaml under
ONEX_SKILL_OVERLAY_ROOTS), reads the PR watcher's state, and writes the open pull requests of the
overlay's repositories as one tick of landing facts to facts_path, the file the snapshot step
reads. Each pass writes one chain (start, then success or failure) to the floor's emission journal
under the request's correlation id.

Exit 0: written. Exit 1: the pass ended in the typed failed event; nothing was written. Exit 3:
the overlay could not be read.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.handler_pr_landing_watcher_facts import (
    HandlerPrLandingWatcherFacts,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_sources import (
    build_watcher_facts_ports,
    resolve_path,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts import (
    EnumWatcherFactsStatus,
    ModelWatcherFactsRequest,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts_overlay import (
    OVERLAY_ID,
    ModelWatcherFactsOverlay,
)

NODE = "node_pr_landing_watcher_facts_effect"


def run_once(
    overlay: ModelWatcherFactsOverlay,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[int, dict[str, object]]:
    """One pass: the exit code and the line it prints."""
    source, sink, publisher = build_watcher_facts_ports(overlay)
    if (
        overlay.emission_journal is None
    ):  # the overlay's validator refuses this for file
        raise ValueError("the file backend needs emission_journal")
    request = ModelWatcherFactsRequest(correlation_id=uuid4())
    chain = ChainEmitter(
        JsonlEmissionSink(resolve_path(overlay.emission_journal)),
        node=NODE,
        piece=overlay.floor_piece,
        correlation_id=request.correlation_id.hex,
    )
    chain.start()
    result = HandlerPrLandingWatcherFacts(
        overlay=overlay,
        state_source=source,
        sink=sink,
        publisher=publisher,
        now=now,
    ).handle(request)
    line: dict[str, object] = {
        "status": result.status.value,
        "correlation_id": str(request.correlation_id),
    }
    if result.status is EnumWatcherFactsStatus.WRITTEN and result.written:
        chain.success({"prs": result.written.prs})
        line.update(
            tick=result.written.tick,
            prs=result.written.prs,
            repositories=list(result.written.repositories),
            facts_path=result.written.facts_path,
        )
        return 0, line
    reason = result.failure_reason.value if result.failure_reason else "failed"
    chain.failure(reason, result.failure_detail or "")
    line.update(failure_reason=reason, detail=result.failure_detail)
    return 1, line


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        overlay = load_structured_overlay(OVERLAY_ID, ModelWatcherFactsOverlay)
        code, line = run_once(overlay)
    except (NodeOverlayError, ValueError) as exc:
        code, line = 3, {"status": "overlay_refused", "detail": str(exc)}
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__: list[str] = ["main", "run_once"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
