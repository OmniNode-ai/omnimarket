# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a retired launchd unit is held, not repaired.

The recorded case: a daemon booted out of a host on purpose keeps lagging its source tip, so the
throughput tick printed MISSING deploy-drift:<daemon>@<host> every tick and the lab-fill machinery
tier turned that recurring key into a repair lane, although a scope=retired-units HOLD held the
unit. The tick's deploy-drift finding and the machinery tier both read those HOLD rows now: a held
unit is a NOTE citing the HOLD on the tick and a ``held:retired-units`` skip in the fill, and a
RELEASE or an expired until= restores the MISSING line and the candidate.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillMachineryCandidates,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillMachineryCandidatesRequest,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-04T03:00:00Z"
KEY = "deploy-drift:lab-work-serve@h200"
HOLD = {
    "ts": "2026-11-04T00:05:00Z",
    "rtype": "HOLD",
    "lane": "orchestrator",
    "text": (
        "2026-11-04T00:05:00Z | HOLD | lane=orchestrator | id=2026-11-04T00:05:00Z-orchestrator | "
        "to=all | scope=retired-units | unit=lab-work-serve@h200 | release=operator | "
        "booted out on purpose; do not reinstall"
    ),
}
RELEASE = {
    "ts": "2026-11-04T02:00:00Z",
    "rtype": "RELEASE",
    "lane": "orchestrator",
    "text": "2026-11-04T02:00:00Z | RELEASE | lane=orchestrator | re=2026-11-04T00:05:00Z-orchestrator",
}


def _drift(daemon: str = "lab-work-serve", host: str = "h200") -> dict[str, Any]:
    return {
        "daemon": daemon,
        "host": host,
        "installed_revision": None,
        "source_revision": "a" * 40,
        "lagging_since": "2026-11-03T12:00:00Z",
    }


def _tick(**over: Any) -> Any:
    payload: dict[str, Any] = {
        "now": NOW,
        "launchctl": "not_loaded",
        "deploy_drifts": [_drift(), _drift(host="h201")],
        **over,
    }
    return HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(payload)
    )


def _line(result: Any, key: str) -> str:
    [line] = [x for x in result.lines if f" {key} | " in x]
    return str(line)


def test_tick_unheld_retired_unit_lag_is_missing() -> None:
    result = _tick()
    assert _line(result, KEY).startswith(f"MISSING {KEY} | ")
    assert KEY in result.missing


def test_tick_retired_units_hold_turns_the_lag_into_a_note() -> None:
    result = _tick(hold_rows=[HOLD])
    line = _line(result, KEY)
    assert line.startswith(f"NOTE {KEY} | "), line
    assert "retired unit held by" in line
    assert "scope=retired-units" in line
    assert KEY not in result.missing
    # The HOLD names the unit on h200 only; the same daemon on h201 stays MISSING.
    assert "deploy-drift:lab-work-serve@h201" in result.missing


def test_tick_bare_daemon_hold_covers_every_host() -> None:
    hold = {
        **HOLD,
        "text": HOLD["text"].replace("unit=lab-work-serve@h200", "unit=lab-work-serve"),
    }
    result = _tick(hold_rows=[hold])
    assert not [
        m for m in result.missing if m.startswith("deploy-drift:lab-work-serve@")
    ]


def test_tick_released_or_expired_hold_is_missing_again() -> None:
    assert KEY in _tick(hold_rows=[HOLD, RELEASE]).missing
    expired = {
        **HOLD,
        "text": HOLD["text"].replace("release=operator", "until=2026-11-04T01:00:00Z"),
    }
    assert KEY in _tick(hold_rows=[expired]).missing


def test_tick_other_scopes_do_not_hold_a_unit() -> None:
    fixer = {**HOLD, "text": HOLD["text"].replace("scope=retired-units", "scope=fixer")}
    assert KEY in _tick(hold_rows=[fixer]).missing


TICK_LINES = [
    {"tick": f"2026-11-04T0{h}:00:00Z", "key": key, "status": "MISSING"}
    for h in (1, 2)
    for key in (KEY, "deploy-drift:lab-work-serve@h201")
]


def _fill(**over: Any) -> Any:
    request = ModelLabFillMachineryCandidatesRequest.model_validate(
        {"observed_at": NOW, "tick_lines": TICK_LINES, **over}
    )
    return HandlerLabFillMachineryCandidates().handle(request)


def test_fill_unheld_drift_key_is_a_candidate() -> None:
    keys = {c.machinery_key for c in _fill().candidates}
    assert f"tick:{KEY}" in keys


def test_fill_skips_the_held_repair() -> None:
    found = _fill(hold_rows=[HOLD])
    keys = {c.machinery_key for c in found.candidates}
    assert f"tick:{KEY}" not in keys
    assert "tick:deploy-drift:lab-work-serve@h201" in keys
    assert {s.id: s.reason for s in found.skipped}[
        f"tick:{KEY}"
    ] == "held:retired-units"
    assert "held:1" in found.diagnostic.reasons


def test_fill_released_hold_offers_the_repair_again() -> None:
    keys = {c.machinery_key for c in _fill(hold_rows=[HOLD, RELEASE]).candidates}
    assert f"tick:{KEY}" in keys
