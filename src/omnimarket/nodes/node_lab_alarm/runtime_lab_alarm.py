# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_alarm <verb>``.

``broker-lag``  read the declared nodes' live consumer groups and keep the sample (what a schedule runs).
``probe``       the same read, keeping nothing.

``--fault`` injects a drill fault; a drill never keeps its sample.

Exit codes: 0 OK, 2 ALARM, 3 INDETERMINATE (a group or the manifest could not be read, or no baseline yet),
1 the command was refused.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    FileSamples,
    HttpManifest,
    KafkaLag,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay, load_overlay

_EXIT = {
    EnumBrokerLagOutcome.OK: 0,
    EnumBrokerLagOutcome.ALARM: 2,
    EnumBrokerLagOutcome.INDETERMINATE: 3,
}


def _inmemory_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    return HandlerBrokerLag(
        overlay=overlay,
        manifest=InMemoryManifest({"contracts": []}),
        lag=InMemoryLag({}),
        samples=InMemorySamples(),
    )


def _runtime_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    return HandlerBrokerLag(
        overlay=overlay,
        manifest=HttpManifest(overlay.manifest.url, overlay.manifest.timeout_seconds),
        lag=KafkaLag(overlay.bus.lane, overlay.lag.timeout_seconds),
        samples=FileSamples(Path(overlay.lag.state_path)),
    )


_BACKEND_BUILDERS: dict[str, Callable[[ModelLabAlarmOverlay], HandlerBrokerLag]] = {
    "inmemory": _inmemory_handler,
    "runtime": _runtime_handler,
}


def build_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    """The handler wired to the overlay's backend."""
    return _BACKEND_BUILDERS[overlay.bus.backend](overlay)


def run(
    verb: str,
    fault: EnumLabAlarmFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerBrokerLag | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    handler = handler or build_handler(overlay)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelBrokerLagCommand(
            correlation_id=f"lab-alarm-broker-lag-{now}",
            write=verb == "broker-lag",
            fault=fault,
        )
    )
    for topic, body in events_for(result, load_topics()):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    for reading in result.readings:
        out(
            f"GROUP {reading.group} state={reading.state.value} lag={reading.lag} "
            f"previous={reading.previous_lag}\n"
        )
    out(
        f"BROKER-LAG outcome={result.outcome.value} declared={len(result.declared_groups)} "
        f"fault={result.fault.value} sample_kept={result.sample_kept} "
        f"alarms={len(result.alarms)} reasons={','.join(result.reasons) or 'none'}\n"
    )
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    return _EXIT[result.outcome]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_lab_alarm")
    parser.add_argument("verb", choices=("broker-lag", "probe"))
    parser.add_argument(
        "--fault", choices=[f.value for f in EnumLabAlarmFault], default="none"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        overlay = load_overlay()
    except NodeOverlayError as exc:
        sys.stderr.write(f"LAB-ALARM-REFUSED {exc}\n")
        return 1
    return run(args.verb, EnumLabAlarmFault(args.fault), overlay=overlay)


if __name__ == "__main__":
    raise SystemExit(main())
