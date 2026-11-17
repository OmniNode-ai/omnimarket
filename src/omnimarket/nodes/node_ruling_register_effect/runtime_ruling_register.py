# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ruling_register_effect``: take the register's readings.

Runs each row's read of the standing-rulings register through its port ONCE, judges the
readings with ``node_ruling_register_compute``'s ``HandlerRulingRegister`` and prints one
``MISSING ruling-not-running:<row> | ... | FIX: ...`` line per row that is not TRUE on
stdout, and one ``RULING-REGISTER `` JSON line of the judged result on stderr. The exit
code is 0 when every row is TRUE, 1 when any is FALSE or UNREAD (a refused read is never
TRUE) and 3 when an overlay is missing or refuses to validate. The in-memory route builds
its port with no fixtures: on the CLI path only the command route takes real readings.
A journal write that failed is one ``RULING-REGISTER-JOURNAL-ERROR <text>`` line on
stderr per failed row; it changes no exit code.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_ruling_register import (
    ModelRulingReadRequest,
    ModelRulingRegisterRequest,
    ModelRulingRegisterResult,
)
from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)
from omnimarket.nodes.node_ruling_register_compute.overlay import (
    load_ruling_register_policy,
)
from omnimarket.nodes.node_ruling_register_effect.adapters import (
    CommandRulingReadPort,
    InMemoryRulingReadPort,
    InMemoryRulingSink,
    JournalRulingSink,
)
from omnimarket.nodes.node_ruling_register_effect.handlers.handler_ruling_register_read import (
    HandlerRulingRegisterRead,
    load_topics,
)
from omnimarket.nodes.node_ruling_register_effect.models import EnumRouteKind
from omnimarket.nodes.node_ruling_register_effect.overlay import load_route
from omnimarket.nodes.node_ruling_register_effect.protocols import (
    ProtocolRulingReadPort,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def read_register(
    environ: Mapping[str, str],
    *,
    port: ProtocolRulingReadPort | None = None,
    clock: Callable[[], datetime] = utc_now,
    now: datetime | None = None,
) -> tuple[ModelRulingRegisterResult, tuple[str, ...]]:
    """The register's judgement under ``environ``'s overlays, and its journal write failures.

    Loads the policy and the route from the overlay roots, builds the read port (the
    injected one, else an in-memory port with no fixtures for an in-memory route, else the
    route's command port), builds the journal sink from the route's ``journal_env``, runs
    the read handler and then the judge, and returns the judged result with the read's
    ``sink_errors``. A missing or invalid overlay raises ``NodeOverlayError``; the caller
    decides the exit code.

    ``now`` is the instant the readings are judged at; unset, it is the clock once the reads have ended. A tick
    reads for as long as its slowest row (the ledger-reader rows one after another: 720 s at most), so a fast row's
    reading is that much older than ``now`` by the time it is judged. The age bound guards against a reading older
    than the tick, so the tick's own span is added to it: a reading taken inside the tick is never stale for the
    tick's length, and a reading older than the bound before the tick began still is.
    """
    policy = load_ruling_register_policy(environ)
    route = load_route(environ)
    if port is not None:
        read_port = port
    elif route.kind is EnumRouteKind.INMEMORY:
        # No fixtures on the runtime path: an in-memory route reads UNREAD until a caller
        # injects a port that holds the readings.
        read_port = InMemoryRulingReadPort({}, clock=clock)
    else:
        # The host the register runs on reads its own rows locally (LAB_WORK_HOST_NAME, the
        # name every host unit declares), not over ssh to itself.
        read_port = CommandRulingReadPort(
            route,
            clock=clock,
            local_host_name=environ.get("LAB_WORK_HOST_NAME", "").strip(),
        )
    journal = environ.get(route.journal_env, "").strip()
    sink = JournalRulingSink(Path(journal)) if journal else InMemoryRulingSink()
    correlation_id = uuid.uuid4().hex
    started = clock()
    read = HandlerRulingRegisterRead(
        read_port, sink, load_topics(), clock=clock
    ).handle(ModelRulingReadRequest(correlation_id=correlation_id, policy=policy))
    ended = clock()
    span_seconds = max(0, math.ceil((ended - started).total_seconds()))
    judged = HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id=correlation_id,
            now=ended if now is None else now,
            policy=policy.model_copy(
                update={
                    "max_reading_age_seconds": policy.max_reading_age_seconds
                    + span_seconds
                }
            ),
            readings=read.readings,
        )
    )
    # ModelRulingReadResult reports its journal write failures as `sink_errors`
    # (`<row_id>: <exception name>`), one per row whose event could not be journalled.
    return judged, read.sink_errors


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    port: ProtocolRulingReadPort | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> int:
    parser = argparse.ArgumentParser(prog="node_ruling_register_effect")
    parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        judged, sink_errors = read_register(env, port=port, clock=clock)
    except NodeOverlayError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
    for error in sink_errors:
        sys.stderr.write(f"RULING-REGISTER-JOURNAL-ERROR {error}\n")
    for line in judged.lines():
        sys.stdout.write(line + "\n")
    sys.stderr.write(f"RULING-REGISTER {judged.model_dump_json()}\n")
    return 0 if judged.passed else 1
