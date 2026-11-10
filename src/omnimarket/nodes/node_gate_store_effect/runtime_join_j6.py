# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_gate_store_effect.runtime_join_j6 <verb>`` (OMN-22936).

``run``    answer each unanswered piece 6 golden reading with a stop in the gate store (audited, projected, fencing its
           piece), write piece 7's chain to the emission journal, and write the fence drill once per window.
``probe``  read what is owed and write nothing.

``--inject <break>`` swaps one real part for a named injected break (the failing control); it is refused unless the overlay
sets ``allow_injected_break``. Each verb prints one JSON object naming correlation ids and outcomes, never a value. Exit
codes: 0 every chain written landed (or nothing was owed); 1 a chain did not land or the ledger could not be read; 3 a
deployment fact is missing (no overlay, no home, no ledger route), reported as INDETERMINATE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStorePolicy,
)
from omnimarket.models.model_gate_store_join_j6_run import ModelJoinJ6RunCommand
from omnimarket.nodes.node_gate_store_effect.handlers.adapters_join_j6 import (
    DroppingJoinJ6AuditSink,
    InMemoryJoinJ6AuditSink,
    InMemoryJoinJ6Projection,
    UnansweringStopGateStore,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
    load_join_j6_policy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6_run import (
    HandlerGateStoreJoinJ6Run,
    JoinJ6Rig,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_gate_store_effect.overlay_join_j6 import (
    ModelJoinJ6Overlay,
    load_join_overlay,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    NODE_NAME as REFUSAL_NODE,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    load_overlay as load_refusal_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)

BREAKS = ("drop-audit-row",)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the overlay names it")
    return value


def _store(overlay: ModelJoinJ6Overlay) -> InMemoryGateStore:
    return InMemoryGateStore(
        ModelGateStorePolicy(
            backend=EnumGateStoreBackend.IN_MEMORY,
            limits={
                overlay.stopped_piece: overlay.piece_limit,
                overlay.other_piece: overlay.piece_limit,
            },
            reservation_ttl_seconds=overlay.reservation_ttl_seconds,
            order=(),
        )
    )


def build_rig(
    overlay: ModelJoinJ6Overlay, *, silent: bool = False, drop_audit: bool = False
) -> JoinJ6Rig:
    """One join over a fresh in-memory twin; ``silent`` leaves a stop's commit unanswered, ``drop_audit`` is the break."""
    policy = load_join_j6_policy()
    audit = DroppingJoinJ6AuditSink() if drop_audit else InMemoryJoinJ6AuditSink()
    projection = InMemoryJoinJ6Projection()
    twin = _store(overlay)
    if silent:
        policy = policy.model_copy(
            update={"commit_deadline_seconds": overlay.drill_commit_deadline_seconds}
        )
        held = UnansweringStopGateStore(twin, overlay.drill_silence_seconds)
        return JoinJ6Rig(
            HandlerGateStoreJoinJ6(held, audit, projection, policy),
            audit,
            projection,
            held.release,
        )
    return JoinJ6Rig(
        HandlerGateStoreJoinJ6(twin, audit, projection, policy), audit, projection
    )


def _run(arguments: argparse.Namespace) -> int:
    overlay = load_join_overlay()
    if arguments.inject is not None and not overlay.allow_injected_break:
        raise ValueError("an injected break runs only under an overlay that allows it")
    refusal = load_refusal_overlay()
    policy = load_join_j6_policy()
    declared = {j.join: j for j in refusal.joins}
    if policy.join_id not in declared:
        raise ValueError(
            f"the {REFUSAL_NODE} overlay declares no join {policy.join_id}: the upstream rule is its"
        )
    home = _required_env(refusal.emission.home_env)
    sink = JsonlEmissionSink(Path(home) / refusal.emission.journal_relpath)
    drop = arguments.inject == "drop-audit-row"
    handler = HandlerGateStoreJoinJ6Run(
        source=build_row_source(refusal.ledger_read, os.environ),
        sink=sink,
        upstream=declared[policy.join_id].upstream,
        overlay=overlay,
        golden_rig=lambda: build_rig(overlay, drop_audit=drop),
        drill_rig=lambda: build_rig(overlay, silent=True, drop_audit=drop),
        window_hours=refusal.emission.window_hours,
        to_piece=policy.to_piece,
        backend="in_memory",
        clock=lambda: datetime.now(UTC),
    )
    result = handler.handle(ModelJoinJ6RunCommand(probe=arguments.verb == "probe"))
    sys.stdout.buffer.write(
        (json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    )
    sys.stdout.buffer.flush()
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_gate_store_join_j6")
    parser.add_argument("verb", choices=("run", "probe"))
    parser.add_argument("--inject", choices=BREAKS)
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return _run(arguments)
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
