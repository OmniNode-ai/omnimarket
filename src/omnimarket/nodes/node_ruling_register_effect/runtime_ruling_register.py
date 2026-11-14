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
"""

from __future__ import annotations

import argparse
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


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    port: ProtocolRulingReadPort | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="node_ruling_register_effect")
    parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        policy = load_ruling_register_policy(env)
        route = load_route(env)
    except NodeOverlayError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
    if port is not None:
        read_port = port
    elif route.kind is EnumRouteKind.INMEMORY:
        # No fixtures on the CLI path: an in-memory route reads UNREAD until a caller
        # injects a port that holds the readings.
        read_port = InMemoryRulingReadPort({}, clock=clock)
    else:
        read_port = CommandRulingReadPort(route, clock=clock)
    journal = env.get(route.journal_env, "").strip()
    sink = JournalRulingSink(Path(journal)) if journal else InMemoryRulingSink()
    correlation_id = uuid.uuid4().hex
    read = HandlerRulingRegisterRead(
        read_port, sink, load_topics(), clock=clock
    ).handle(ModelRulingReadRequest(correlation_id=correlation_id, policy=policy))
    judged = HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id=correlation_id,
            now=clock(),
            policy=policy,
            readings=read.readings,
        )
    )
    for line in judged.lines():
        sys.stdout.write(line + "\n")
    sys.stderr.write(f"RULING-REGISTER {judged.model_dump_json()}\n")
    return 0 if judged.passed else 1
