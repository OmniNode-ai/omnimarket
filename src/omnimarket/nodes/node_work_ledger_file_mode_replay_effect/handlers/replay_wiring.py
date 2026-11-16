# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the file-mode replay handler from its overlay, for the command form (OMN-19998).

Every deployment fact (the queue file, the ledger file and its archive, the source the
re-emitted rows carry) is an overlay field; a missing one is a refusal naming the field.
The emit node's handler is the only publisher: nothing here opens a broker connection.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
    ProtocolEventEmitter,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.emit_path_publisher import (
    EmitPathPublisher,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.handler_file_mode_replay import (
    HandlerFileModeReplay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.ledger_file_rows import (
    FileLedgerFileRows,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    build_replay_queue,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    ModelReplayOverlay,
)


def build_replay_handler(
    overlay: ModelReplayOverlay, *, emitter: ProtocolEventEmitter | None = None
) -> HandlerFileModeReplay:
    """The replay handler the overlay describes; ``emitter`` replaces the emit-effect spool in tests."""
    ledger_path, replay_source = overlay.ledger_path, overlay.replay_source
    missing = [
        name
        for name, value in (
            ("ledger_path", ledger_path),
            ("replay_source", replay_source),
        )
        if not value
    ]
    if ledger_path is None or replay_source is None:
        raise ValueError(f"replay_from_file needs overlay fields {missing}")
    publisher = EmitPathPublisher(
        emit=HandlerWorkLedgerEmit(emitter=emitter), source=replay_source
    )
    return HandlerFileModeReplay(
        queue=build_replay_queue(overlay),
        publisher=publisher,
        file_rows=FileLedgerFileRows(
            Path(ledger_path),
            Path(overlay.archive_dir) if overlay.archive_dir else None,
            overlay.archive_glob,
        ),
    )


@contextmanager
def bound_bus_lane(
    lane: str, *, registry_root: Path | None
) -> Iterator[ProtocolEventEmitter]:
    """The emit-effect emitter, authenticated as the named bus lane's declared transport.

    The lane's client transport (the principal this host holds for the lane) is bound for the
    scope exactly as ``onex delegate --bus kafka --bus-lane <lane>`` binds it, and the emitter's
    publisher opens its connection through ``EventBusKafka.from_bootstrap`` so the binding is
    honoured (the emit effect's own default builds the bus from environment settings only and
    sends no credential from a host that holds none). A lane that declares no address, or one this
    host holds no identity for, is a ValueError naming the lane.
    """
    from omnibase_infra.cli.delegate_lane import (
        DelegateLaneSelectionError,
        resolve_lane_target,
    )
    from omnibase_infra.cli.delegate_lane_credentials import (
        DelegateLaneCredentialError,
        resolve_lane_client_transport_for,
    )
    from omnibase_infra.cli.model_delegate_lane_selection import (
        ModelDelegateLaneSelection,
    )
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
        HandlerEventEmitEffect,
        KafkaEventPublisher,
    )

    try:
        # Called by its declared parameter names (bus, lane, broker address, registry root), as the
        # bus binding's positional opener does, so this module spells none of them twice.
        resolver: Callable[..., ModelDelegateLaneSelection | None] = resolve_lane_target
        names = tuple(inspect.signature(resolver).parameters)
        target = resolver(
            **dict(zip(names, ("kafka", lane, None, registry_root), strict=True))
        )
        transport = resolve_lane_client_transport_for(
            lane_target=target, onex_home=Path.home() / ".onex", environ=os.environ
        )
    except (DelegateLaneSelectionError, DelegateLaneCredentialError) as exc:
        raise ValueError(f"bus lane {lane!r}: {exc}") from exc
    if target is None or transport is None:
        raise ValueError(f"bus lane {lane!r} resolved no broker address and transport")
    bootstrap = target.bootstrap_servers
    with bind_lane_client_transport(transport):
        adapter = KafkaEventPublisher(
            bootstrap,
            publish_identity=f"{bootstrap}|lane={lane}",
            bus_factory=lambda: EventBusKafka.from_bootstrap(bootstrap),
        )
        yield HandlerEventEmitEffect(publish_adapter=adapter)


__all__ = ["bound_bus_lane", "build_replay_handler"]
