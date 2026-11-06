# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_secret_reading_effect <verb>``.

``read``   probe every declared consumer and append the piece 5 reading row.
``probe``  the same probes, appending nothing.

Each verb prints the events the run produced, one JSON object per line (topic and body). No
line carries a secret value: the probe port returns outcomes only.

Exit codes: 0 the run completed (a NOT WORKING reading is still 0: a reading refuses nothing);
1 the run failed (an undeclared consumer, an append that did not land); 3 a deployment fact is
missing (no overlay, no host name, no append route).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_secret_reading import ModelSecretReadingCommand
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
    LocalExposureSurfaces,
    SecretStoreEffectProbeStore,
)
from omnimarket.nodes.node_secret_reading_effect.handlers.handler_secret_reading import (
    HandlerSecretReading,
    events_of,
    load_topics,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    ModelSecretReadingOverlay,
    load_overlay,
)
from omnimarket.nodes.node_secret_reading_effect.protocols import (
    ProtocolSecretProbeStore,
)


def build_store(overlay: ModelSecretReadingOverlay) -> ProtocolSecretProbeStore:
    if overlay.store.backend == "inmemory":
        return InMemorySecretProbeStore({})
    assert overlay.store.secret_store is not None
    return SecretStoreEffectProbeStore.from_overlay(overlay.store.secret_store)


def _run(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    handler = HandlerSecretReading(
        overlay=overlay,
        store=build_store(overlay),
        surfaces=LocalExposureSurfaces(),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        host=host,
    )
    command = ModelSecretReadingCommand(
        correlation_id=arguments.correlation_id or str(uuid.uuid4()),
        consumers=tuple(arguments.consumer or ()),
        write=write,
    )
    result = handler.handle(command)
    for topic, body in events_of(result, load_topics()):
        sys.stdout.write(
            json.dumps({"topic": topic, "body": body}, sort_keys=True) + "\n"
        )
    return 0 if result.status == "completed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_secret_reading_effect")
    parser.add_argument("verb", choices=("read", "probe"))
    parser.add_argument("--consumer", action="append")
    parser.add_argument("--correlation-id")
    arguments = parser.parse_args(argv)
    try:
        return _run(arguments, write=arguments.verb == "read")
    except (NodeOverlayError, LedgerRouteError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        return 3


__all__ = ["build_store", "main"]
