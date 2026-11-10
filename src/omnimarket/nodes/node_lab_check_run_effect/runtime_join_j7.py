# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_check_run_effect.runtime_join_j7 <verb>`` (OMN-23420).

``run``    answer each unanswered piece 7 golden reading with a send past a stop whose lab check posts on a test head, write
           piece 8's chain to the emission journal, and write piece 8's error drill (a superseded attempt) once per window.
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

from omnimarket.handlers.emission_sink import InMemoryEmissionSink, JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStorePolicy,
)
from omnimarket.models.model_lab_check_run_join_j7_run import ModelJoinJ7RunCommand
from omnimarket.nodes.node_gate_store_effect.handlers.adapters_join_j6 import (
    InMemoryJoinJ6AuditSink,
    InMemoryJoinJ6Projection,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
    load_join_j6_policy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    NODE_NAME as REFUSAL_NODE,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    load_overlay as load_refusal_overlay,
)
from omnimarket.nodes.node_lab_check_run_effect.adapters.inmemory_check_run_api import (
    InMemoryCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.adapters_join_j7 import (
    DeafInvalidationCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_effect import (
    HandlerLabCheckRunEffect,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_join_j7_run import (
    HandlerLabCheckRunJoinJ7Run,
    JoinJ7Rig,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay_join_j7 import (
    ModelJoinJ7Overlay,
    load_join_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)

BREAKS = ("deaf-invalidation",)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the overlay names it")
    return value


def build_rig(overlay: ModelJoinJ7Overlay, *, deaf: bool = False) -> JoinJ7Rig:
    """One twin: a fresh in-memory gate store behind the join and a fresh check-run API; ``deaf`` is the break."""
    store = InMemoryGateStore(
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
    join = HandlerGateStoreJoinJ6(
        store,
        InMemoryJoinJ6AuditSink(),
        InMemoryJoinJ6Projection(),
        load_join_j6_policy(),
    )
    settings = overlay.check_run.settings
    api: InMemoryCheckRunApi = (
        DeafInvalidationCheckRunApi(settings.superseded_conclusion)
        if deaf
        else InMemoryCheckRunApi()
    )
    labcheck = HandlerLabCheckRunEffect(
        overlay=overlay.check_run,
        api=api,
        sink=InMemoryEmissionSink(),
        clock=lambda: datetime.now(UTC),
    )
    return JoinJ7Rig(join=join, api=api, labcheck=labcheck)


def _run(arguments: argparse.Namespace) -> int:
    overlay = load_join_overlay()
    if arguments.inject is not None and not overlay.allow_injected_break:
        raise ValueError("an injected break runs only under an overlay that allows it")
    refusal = load_refusal_overlay()
    declared = {j.join: j for j in refusal.joins}
    join_id = overlay.join_id
    if join_id not in declared:
        raise ValueError(
            f"the {REFUSAL_NODE} overlay declares no join {join_id}: the upstream rule is its"
        )
    if declared[join_id].downstream_node != overlay.emission_node:
        raise ValueError(
            f"emission_node {overlay.emission_node!r} is not the {REFUSAL_NODE} overlay's {join_id} downstream_node"
        )
    home = _required_env(refusal.emission.home_env)
    sink = JsonlEmissionSink(Path(home) / refusal.emission.journal_relpath)
    deaf = arguments.inject == "deaf-invalidation"
    handler = HandlerLabCheckRunJoinJ7Run(
        source=build_row_source(refusal.ledger_read, os.environ),
        sink=sink,
        upstream=declared[join_id].upstream,
        overlay=overlay,
        golden_rig=lambda: build_rig(overlay),
        drill_rig=lambda: build_rig(overlay, deaf=deaf),
        window_hours=refusal.emission.window_hours,
        to_piece=declared[join_id].to_piece,
        backend="in_memory",
        clock=lambda: datetime.now(UTC),
    )
    result = handler.handle(ModelJoinJ7RunCommand(probe=arguments.verb == "probe"))
    sys.stdout.buffer.write(
        (json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    )
    sys.stdout.buffer.flush()
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_lab_check_run_join_j7")
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
