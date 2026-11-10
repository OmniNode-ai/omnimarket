# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_secret_rotation_readback_effect <verb>``.

``read``   read back every declared consumer after the named rotation and append one row each.
``probe``  the same probes, appending nothing.

Each verb prints the events the run produced, one JSON object per line (topic and body). No
line carries a secret value: the ports return outcomes only.

Exit codes: 0 the run completed (a NOT WORKING reading is still 0: a reading refuses nothing);
1 the run failed or the rotation was refused (no approver, an approver or key the overlay does
not list, an append that did not land); 3 a deployment fact is missing (no overlay, no host
name, no append route).
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
from omnimarket.models.model_secret_rotation import ModelRotationReadbackCommand
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
    SecretStoreEffectProbeStore,
)
from omnimarket.nodes.node_secret_reading_effect.protocols import (
    ProtocolSecretProbeStore,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.adapters import (
    CommandConsumerCalls,
    InMemoryConsumerCalls,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.handlers.handler_rotation_readback import (
    HandlerRotationReadback,
    events_of,
    load_topics,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationReadbackOverlay,
    load_overlay,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.protocols import (
    ProtocolConsumerCalls,
)


def build_store(overlay: ModelRotationReadbackOverlay) -> ProtocolSecretProbeStore:
    addressing = overlay.store.secret_store
    if addressing is None:
        # Synthetic profile: every consumer's key resolves to a value derived from its own handle.
        return InMemorySecretProbeStore(
            {
                (c.folder, c.key): f"synthetic:{c.folder}:{c.key}"
                for c in overlay.consumers
            }
        )
    return SecretStoreEffectProbeStore.from_overlay(addressing)


def build_calls(
    overlay: ModelRotationReadbackOverlay, store: ProtocolSecretProbeStore
) -> ProtocolConsumerCalls:
    """The overlay chooses: a consumer with a probe command is probed through it; with none, the
    in-memory profile holds every consumer on the store's value."""
    if overlay.store.secret_store is None:
        assert isinstance(store, InMemorySecretProbeStore)
        return InMemoryConsumerCalls(store)
    return CommandConsumerCalls(timeout_seconds=overlay.call_timeout_seconds)


def _run(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    store = build_store(overlay)
    handler = HandlerRotationReadback(
        overlay=overlay,
        store=store,
        calls=build_calls(overlay, store),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        host=host,
    )
    command = ModelRotationReadbackCommand(
        correlation_id=arguments.correlation_id or str(uuid.uuid4()),
        rotation_id=arguments.rotation_id,
        rotated_folder=arguments.folder,
        rotated_key=arguments.key,
        approver=arguments.approver or "",
        drill=arguments.drill,
        write=write,
    )
    result = handler.handle(command)
    for topic, body in events_of(result, load_topics()):
        line = json.dumps({"topic": topic, "body": body}, sort_keys=True) + "\n"
        sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0 if result.status == "completed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_secret_rotation_readback_effect")
    parser.add_argument("verb", choices=("read", "probe"))
    parser.add_argument("--rotation-id", required=True)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--approver")
    parser.add_argument("--drill", action="store_true")
    parser.add_argument("--correlation-id")
    arguments = parser.parse_args(argv)
    try:
        return _run(arguments, write=arguments.verb == "read")
    except (NodeOverlayError, LedgerRouteError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        return 3


__all__ = ["build_calls", "build_store", "main"]
