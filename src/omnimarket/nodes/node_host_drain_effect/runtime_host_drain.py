# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_drain_effect <verb>`` (OMN-22905).

``drain --host H``       set H draining: no new lane is placed on it; running lanes finish.
``undrain --host H``     return H to placement.
``status --host H``      read whether H is draining, its running lanes, and whether it is drained.
``check-start --host H`` ask whether a new lane may start on H (refused while draining).
``draining``             list the hosts now draining, one per line (for placement callers).
``runner-stop --runner R`` stop runner R only when it holds no job.

Exit codes: 0 done; 1 refused or failed (a typed failure code is in the JSON); 3 a deployment fact is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
    CommandLaneSource,
    CommandWorkerProbe,
    CommandWorkerStopper,
    ProcWorkerProbe,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import (
    AiokafkaStateTopicConnection,
    InMemoryDrainStore,
    JsonlDrainStore,
    RedpandaDrainStore,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_idle_runner_stop import (
    HandlerIdleRunnerStop,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    EnumRunnerStopOutcome,
    ModelHostDrainRequest,
    ModelIdleRunnerStopRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    ModelHostDrainOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_drain_effect.protocols import ProtocolDrainStore

_REFUSED = {
    EnumDrainOutcome.FAILED,
    EnumDrainOutcome.START_REFUSED,
}


class _MissingFactError(Exception):
    """A deployment fact the profile needs is not in the environment."""


def _require_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise _MissingFactError(f"environment variable {name} is not set")
    return value


def build_store_and_sink(
    overlay: ModelHostDrainOverlay, environ: Mapping[str, str]
) -> tuple[ProtocolDrainStore, ProtocolEmissionSink, Literal["in_memory", "real"]]:
    """The store and the chain-event sink of the overlay's profile, and the backend label the chains carry."""
    if overlay.profile == "in_memory":
        return InMemoryDrainStore(), InMemoryEmissionSink(), "in_memory"
    home = Path(_require_env(environ, overlay.emission.home_env))
    sink = JsonlEmissionSink(home / overlay.emission.journal_relpath)
    if overlay.profile == "jsonl":
        return JsonlDrainStore(home / overlay.jsonl_state_relpath), sink, "real"
    servers = _require_env(environ, overlay.redpanda.bootstrap_servers_env)
    client = AiokafkaStateTopicConnection(overlay.redpanda, servers)
    return RedpandaDrainStore(overlay.redpanda, client), sink, "real"


def _print(model: BaseModel) -> None:
    print(model.model_dump_json())


def run(
    argv: Sequence[str],
    *,
    overlay: ModelHostDrainOverlay,
    environ: Mapping[str, str],
) -> int:
    parser = argparse.ArgumentParser(prog="node_host_drain_effect")
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("drain", "undrain", "status", "check-start"):
        p = sub.add_parser(verb)
        p.add_argument("--host", required=True)
        p.add_argument("--reason", default="")
        p.add_argument("--correlation-id", default="")
    sub.add_parser("draining")
    stop = sub.add_parser("runner-stop")
    stop.add_argument("--runner", required=True)
    stop.add_argument("--correlation-id", default="")
    args = parser.parse_args(list(argv))
    try:
        store, sink, backend = build_store_and_sink(overlay, environ)
    except _MissingFactError as exc:
        print(f"INDETERMINATE: {exc}", file=sys.stderr)
        return 3
    if args.verb == "draining":
        for host in store.draining_hosts():
            print(host)
        return 0
    if args.verb == "runner-stop":
        stopper = HandlerIdleRunnerStop(
            overlay.runner_stop,
            probe=(
                CommandWorkerProbe(overlay.runner_stop)
                if overlay.runner_stop.probe_kind == "command"
                else ProcWorkerProbe(overlay.runner_stop)
            ),
            stopper=CommandWorkerStopper(overlay.runner_stop),
            sink=sink,
            clock=lambda: datetime.now(UTC),
            backend=backend,
        )
        stopped = stopper.handle(
            ModelIdleRunnerStopRequest(
                runner_name=args.runner, correlation_id=args.correlation_id
            )
        )
        _print(stopped)
        return 0 if stopped.outcome is EnumRunnerStopOutcome.STOPPED else 1
    drain = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=CommandLaneSource(overlay.lanes),
        sink=sink,
        clock=lambda: datetime.now(UTC),
        backend=backend,
    )
    result = drain.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp(args.verb.replace("-", "_")),
            host_name=args.host,
            correlation_id=args.correlation_id,
            reason=args.reason,
        )
    )
    _print(result)
    return 1 if result.outcome in _REFUSED else 0


def main(argv: Sequence[str] | None = None) -> int:
    environ = os.environ
    try:
        overlay = load_overlay(environ)
    except (NodeOverlayError, ValidationError) as exc:
        print(f"INDETERMINATE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return run(sys.argv[1:] if argv is None else argv, overlay=overlay, environ=environ)
