# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_append_effect.runtime_join_j5 <verb>`` (OMN-23086).

``run``    answer each unanswered piece 5 golden reading with a ledger append under the store-resolved identity, and write
           piece 6's chain to the emission journal.
``probe``  read what is owed and write nothing.

``--inject <break>`` swaps one real port for a named injected break (the failing control); it is refused unless the overlay
sets ``allow_injected_break``. Each verb prints one JSON object naming correlation ids, outcomes and positions, never a
value. Exit codes: 0 every answered chain landed (or nothing was owed); 1 a chain did not land or the ledger could not be
read; 3 a deployment fact is missing (no overlay, no host name, no append route), reported as INDETERMINATE.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.work_ledger_append import ModelJoinJ5RunCommand
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
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    load_overlay as load_reading_overlay,
)
from omnimarket.nodes.node_secret_reading_effect.runtime_secret_reading import (
    build_store,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    BusCliAppend,
    SourceProjectedRows,
    StoreProbeIdentities,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5 import (
    HandlerWorkLedgerJoinJ5,
    load_join_j5_policy,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5_run import (
    HandlerWorkLedgerJoinJ5Run,
)
from omnimarket.nodes.node_work_ledger_append_effect.overlay_join_j5 import (
    load_join_overlay,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    InMemoryProjectedRows,
    InMemoryStoreIdentities,
    ProtocolProjectedRows,
    ProtocolStoreIdentities,
)

BREAKS = ("deaf-projection", "unresolvable-identity")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the overlay names it")
    return value


def _run(arguments: argparse.Namespace) -> int:
    join = load_join_overlay()
    if arguments.inject is not None and not join.allow_injected_break:
        raise ValueError("an injected break runs only under an overlay that allows it")
    refusal = load_refusal_overlay()
    policy = load_join_j5_policy()
    declared = {j.join: j for j in refusal.joins}
    if policy.join_id not in declared:
        raise ValueError(
            f"the {REFUSAL_NODE} overlay declares no join {policy.join_id}: the upstream rule is its"
        )
    rule = declared[policy.join_id].upstream
    host = _required_env(join.ledger.host_env)
    ledger_path = Path(_required_env(join.ledger.path_env))
    home = _required_env(refusal.emission.home_env)
    sink = JsonlEmissionSink(Path(home) / refusal.emission.journal_relpath)

    identities: ProtocolStoreIdentities = StoreProbeIdentities(
        build_store(load_reading_overlay()), (join.identity,)
    )
    projection: ProtocolProjectedRows = SourceProjectedRows(
        build_row_source(join.projection.read, os.environ),
        attempts=join.projection.attempts,
        interval_seconds=join.projection.interval_seconds,
        slack_seconds=join.projection.slack_seconds,
    )
    if arguments.inject == "deaf-projection":
        projection = InMemoryProjectedRows()
    elif arguments.inject == "unresolvable-identity":
        identities = InMemoryStoreIdentities({})

    append = BusCliAppend(
        shlex.split(_required_env(join.ledger.command_env)),
        host=host,
        timeout_seconds=float(join.ledger.append_timeout_seconds),
    )
    handler = HandlerWorkLedgerJoinJ5Run(
        join=HandlerWorkLedgerJoinJ5(
            append=append,
            ledger=LocalLedgerFile(ledger_path),
            identities=identities,
            projection=projection,
            sink=sink,
            policy=policy,
            backend="real",
        ),
        source=build_row_source(refusal.ledger_read, os.environ),
        sink=sink,
        upstream=rule,
        identity_ref=join.identity.ref,
        ledger_id=join.ledger.ledger_id,
        row_lane=join.row.lane,
        requesting_lane=join.row.requesting_lane,
        row_text=join.row.text,
        host=host,
        window_hours=refusal.emission.window_hours,
        max_appends=join.max_appends_per_run,
        to_piece=policy.to_piece,
        clock=lambda: datetime.now(UTC),
    )
    result = handler.handle(ModelJoinJ5RunCommand(probe=arguments.verb == "probe"))
    sys.stdout.buffer.write(
        (json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    )
    sys.stdout.buffer.flush()
    ok = result.read_ok and all(i.outcome.value == "landed" for i in result.items)
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_work_ledger_join_j5")
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
