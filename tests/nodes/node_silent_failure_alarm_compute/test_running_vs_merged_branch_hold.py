# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A recorded controller branch hold is an expected difference, never a MISSING line (OMN-19998).

The landing controller runs a branch install on purpose (install.sh --branch-hours records
``branch_until`` and ``branch_source`` in landing_install.json). While ``branch_until`` is ahead the
running-vs-merged decision reports the hold as a note and sends no repair; once it has passed the
difference is MISSING again.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_running_vs_merged_alarm import (
    HandlerRunningVsMergedAlarm,
    request_from_fleet,
)

HOLD_UNTIL = "2026-10-25T08:24:49Z"
OBSERVED = "matches none of the last 50 controller commits on origin/main; installed 53aef241cbce"


def _fleet(
    record: dict[str, object] | None, observed: str = OBSERVED
) -> dict[str, object]:
    item: dict[str, object] = {
        "key": "controller:landing-controller",
        "kind": "controller",
        "observed": observed,
        "first_seen_at": "2026-10-23T11:21:40Z",
        "verdict": "DRIFT",
    }
    if record is not None:
        item["install_record"] = record
    return {
        "folded_at": "2026-10-25T05:15:37Z",
        "hosts": [{"host": "h200", "open": [item]}],
    }


def _record(tmp_path: Path, **fields: str) -> dict[str, object]:
    path = tmp_path / "landing_install.json"
    path.write_text(json.dumps({"clone": "plugin-clone", **fields}), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _decide(fleet: dict[str, object], now: str):
    fleet = {**fleet, "folded_at": now}
    request = request_from_fleet(
        fleet, datetime.fromisoformat(now.replace("Z", "+00:00"))
    )
    return HandlerRunningVsMergedAlarm().handle(request)


def test_a_live_branch_hold_is_a_note_with_its_expiry_and_no_missing(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path, branch_until=HOLD_UNTIL, branch_source="bfe3a404")
    result = _decide(_fleet(record), "2026-10-25T05:20:00Z")
    assert result.state == "OK"
    assert result.findings == ()
    (note,) = result.acknowledged
    assert note.level == "note"
    assert note.loop == "running-vs-merged:controller:h200:landing-controller"
    assert HOLD_UNTIL in note.why
    assert "bfe3a404" in note.why
    assert note.fix == ""


def test_the_same_difference_is_missing_once_branch_until_has_passed(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path, branch_until=HOLD_UNTIL, branch_source="bfe3a404")
    result = _decide(_fleet(record), "2026-10-25T08:24:50Z")
    assert result.state == "MISSING"
    assert [f.loop for f in result.findings] == [
        "running-vs-merged:controller:h200:landing-controller"
    ]
    assert result.acknowledged == ()


def test_a_branch_until_that_does_not_parse_holds_nothing(tmp_path: Path) -> None:
    record = _record(tmp_path, branch_until="soon")
    result = _decide(_fleet(record), "2026-10-25T05:20:00Z")
    assert result.state == "MISSING"
    assert result.acknowledged == ()


def test_no_recorded_hold_stays_missing() -> None:
    result = _decide(_fleet(None), "2026-10-25T05:20:00Z")
    assert result.state == "MISSING"


def test_the_fold_text_naming_a_live_hold_is_read_when_no_record_rides_with_the_item() -> (
    None
):
    observed = (
        f"{OBSERVED}; branch-held until {HOLD_UNTIL} by INSTALLED-FROM-BRANCHES.txt"
    )
    result = _decide(_fleet(None, observed), "2026-10-25T05:20:00Z")
    assert result.state == "OK"
    assert HOLD_UNTIL in result.acknowledged[0].why


def test_a_hold_on_the_controller_never_excuses_another_surface(tmp_path: Path) -> None:
    record = _record(tmp_path, branch_until=HOLD_UNTIL, branch_source="bfe3a404")
    fleet = _fleet(record)
    hosts = fleet["hosts"]
    assert isinstance(hosts, list)
    hosts[0]["open"].append(
        {
            "key": "dispatch-venv:/srv/venv",
            "kind": "dispatch-venv",
            "observed": "behind",
            "first_seen_at": "2026-10-25T01:00:00Z",
            "install_record": record,
        }
    )
    result = _decide(fleet, "2026-10-25T05:20:00Z")
    assert result.state == "MISSING"
    assert [f.loop for f in result.findings] == [
        "running-vs-merged:dispatch-venv:h200:/srv/venv"
    ]
    assert len(result.acknowledged) == 1
