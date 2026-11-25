# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command of the delegation check regression run: replay a recorded transition through the node.

``--replay-fixture`` names a recorded incident (readings, the window they cover, the recorded changes). The checks are
answered from the recorded readings, never invented. ``--delivery inmemory`` (the default) hands the alarms to the lab alarm
delivery with its in-memory ledger and channel, so a replay writes no row to the live ledger and posts nothing;
``--delivery overlay`` uses the backend the deployed delivery overlay names.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_delegation_check_regression_effect.adapters.replay_source import (
    ReplayCheckSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_check_regression import (
    HandlerCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumChangeKind,
    ModelCheckRegressionRequest,
    ModelCheckRegressionResult,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    DEFAULTS_PATH,
    load_settings,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import load_overlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler,
)

_CHANGES = {
    "settings_store": "settings_change",
    "host_model": "host_model_change",
    "release": "release_change",
}


@dataclass
class _Replay:
    result: ModelCheckRegressionResult
    ledger_rows: list[tuple[str, str]]
    posts: list[tuple[str, str]]


def _request(
    incident: dict[str, Any], change: str, target: str
) -> ModelCheckRegressionRequest:
    record = incident[_CHANGES[change]]
    if change == "settings_store":
        return ModelCheckRegressionRequest(
            correlation_id=f"delegation-check-regression-replay-{change}",
            change_kind=EnumChangeKind.SETTINGS_STORE,
            subject=record["overlay_id"],
            previous_document=record["previous_document"],
            new_document=record["new_document"],
            applied_at=datetime.fromisoformat(record["applied_at"]),
            actor=record["loaded_by"],
            target=target,
        )
    return ModelCheckRegressionRequest(
        correlation_id=f"delegation-check-regression-replay-{change}",
        change_kind=EnumChangeKind(record["change_kind"]),
        subject=record["subject"],
        changed_keys=tuple(record["changed_keys"]),
        applied_at=datetime.fromisoformat(record["applied_at"]),
        actor=record["actor"],
        target=target,
    )


def replay(
    fixture: Path, change: str, target: str, delivery: str, settings_path: Path
) -> _Replay:
    incident = json.loads(fixture.read_text(encoding="utf-8"))
    window = incident["transition_window"]
    source = ReplayCheckSource.from_readings(
        incident["readings"],
        previous_through=window["green_through"],
        current_through=window["red_from"],
    )
    # The replay reads the recorded red as of the recorded red publish: the clock stands there.
    stamped = datetime.fromisoformat(window["red_from"].replace("Z", "+00:00"))
    clock_at = stamped if stamped.tzinfo else stamped.replace(tzinfo=UTC)
    overlay = load_overlay()
    ledger = InMemoryAlarmLedger()
    channel = InMemoryAlarmChannel()
    if delivery == "inmemory":
        port: HandlerLabAlarmDeliver = HandlerLabAlarmDeliver(
            overlay=overlay.model_copy(update={"backend": "inmemory"}),
            channel=channel,
            ledger=ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: clock_at,
        )
    else:
        port = build_handler(overlay)
    request = _request(incident, change, target)
    handler = HandlerCheckRegression(
        settings=load_settings(settings_path),
        runner=source,
        history=source,
        delivery=port,
        clock=lambda: clock_at,
    )
    return _Replay(handler.handle(request), ledger.rows, channel.posts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_delegation_check_regression_effect")
    parser.add_argument("--replay-fixture", type=Path, required=True)
    parser.add_argument("--change", choices=sorted(_CHANGES), default="host_model")
    parser.add_argument("--target", default="dev")
    parser.add_argument(
        "--delivery", choices=("inmemory", "overlay"), default="inmemory"
    )
    parser.add_argument("--settings", type=Path, default=DEFAULTS_PATH)
    args = parser.parse_args(argv)
    done = replay(
        args.replay_fixture, args.change, args.target, args.delivery, args.settings
    )
    r = done.result
    out = sys.stdout.write
    out(
        f"outcome={r.outcome.value} selected={','.join(r.selected_checks)} failure={r.failure or '-'}\n"
    )
    for v in r.verdicts:
        out(
            f"verdict check={v.check_id} previous={v.previous.value if v.previous else 'none'} current={v.verdict.value} run={v.run_ref or '-'}\n"
        )
    for a in r.alarms:
        out(
            f"alarm check={a.check_id} owner={a.owner} raised_after_s={a.raised_after.total_seconds():.0f} late={a.late}\n"
        )
    for request_id, row in done.ledger_rows:
        out(f"ledger-row req={request_id} {row}\n")
    return 0 if not r.failure else 1


if __name__ == "__main__":
    sys.exit(main())
