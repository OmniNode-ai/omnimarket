# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The quiet-host alarm raises on a host that goes quiet again after an alarm covered it (OMN-22777).

On 2026-11-06 the alarm unit read ``quiet_host=deduped`` and wrote no row while h201 read quiet. The
delivery state held one open alarm that began 2026-10-28 and had been escalated the same day, and its
host set was the union of every host any row of that episode had named. The set only grew, and the
episode never ended because some host is always quiet, so a host that recovered and went quiet again
(or that read unattributed before its alias existed) counted as already raised. The control in each
reading below is the same tick on the unfixed rule: the host must stay silent there, which shows the
reading depends on the rule and not on the fixture. Host ids here are the recorded ones; they are
labels, not machine data.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.silent_failure_alarm import ModelDrainerObservation
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    RECORDED,
    Watch,
    world,
)

pytestmark = pytest.mark.unit

ROW = (
    "{stamp} | FRICTION | lane=silent-failure-alarm | ticket=OMN-18603 | actor=launchd:silent-failure-alarm | "
    "class=quiet-host | hosts={hosts} | cost=est ~60 minutes of lane work with no captured hook events | recorded"
)


def _stamp(at) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tick(options: ModelSilentFailureAlarmOptions, at) -> tuple[str, ...]:
    """One applying tick at ``at``; the hosts of the quiet-host row it wrote, empty when it wrote none."""
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in options.host_aliases
    }
    result = HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=at)
    )
    assert result.exit_code == 0, result.warnings
    rows = [row for row in result.rows if "class=quiet-host" in row]
    if not rows:
        return ()
    hosts = rows[0].split("hosts=", 1)[1].split(" | ", 1)[0]
    return tuple(hosts.split(","))


def _events(options: ModelSilentFailureAlarmOptions, lines: list[str]) -> None:
    events = Path(options.hook_events_command[-1])  # type: ignore[index]
    events.write_text("".join(f"{line}\n" for line in lines))


def _claim(options: ModelSilentFailureAlarmOptions, host: str, at) -> None:
    with options.ledger.open("a") as handle:  # type: ignore[union-attr]
        handle.write(
            f"{_stamp(at)} | CLAIM | lane=recorded | host={host} | more work\n"
        )


def _open_alarm(options: ModelSilentFailureAlarmOptions) -> dict[str, object]:
    return json.loads(options.delivery_state_file.read_text())["open"]["quiet-host"]


def test_same_quiet_hosts_on_the_next_tick_stay_deduped(tmp_path: Path) -> None:
    options = world(tmp_path)
    assert set(_tick(options, HOST_NOW)) == set(RECORDED)
    assert _tick(options, HOST_NOW + timedelta(minutes=10)) == ()
    assert options.ledger.read_text().count("class=quiet-host") == 1  # type: ignore[union-attr]


def test_quiet_host_dedupe_host_that_recovered_and_went_quiet_again_raises(
    tmp_path: Path,
) -> None:
    options = world(tmp_path)
    assert set(_tick(options, HOST_NOW)) == set(RECORDED)

    # h101 sends an event and reads healthy: the alarm stays open for the other four, no new row.
    live = HOST_NOW + timedelta(minutes=9)
    _events(options, [f"h101|{_stamp(live)}"])
    assert _tick(options, HOST_NOW + timedelta(minutes=10)) == ()
    assert "h101" not in _open_alarm(options)["hosts"]  # type: ignore[operator]

    # It runs lanes again after that event and sends none for half an hour: quiet again.
    _claim(options, "h101", HOST_NOW + timedelta(minutes=20))
    _claim(options, "h101", HOST_NOW + timedelta(minutes=45))
    again = _tick(options, HOST_NOW + timedelta(minutes=50))
    assert "h101" in again, (
        "a host quiet again after an alarm covered it must raise a row"
    )
    assert options.ledger.read_text().count("class=quiet-host") == 2  # type: ignore[union-attr]
    # The row is now the record: the next tick on the same hosts is deduped.
    assert _tick(options, HOST_NOW + timedelta(minutes=55)) == ()


def test_quiet_host_dedupe_recorded_state_names_the_host_no_row_named(
    tmp_path: Path,
) -> None:
    """The 2026-11-06 state: an eight-host episode from 10-28, newest row without h201, h201 quiet now."""
    base = world(tmp_path)
    hosts = ("h053", "h106", "h201", "h202")
    options = base.model_copy(update={"host_aliases": {h: h for h in hosts}})
    options.ledger.write_text(  # type: ignore[union-attr]
        "".join(
            f"{_stamp(HOST_NOW - timedelta(minutes=minutes))} | CLAIM | lane=recorded | host={host} | work\n"
            for host in hosts
            for minutes in (400, 5)
        )
        + ROW.format(
            stamp=_stamp(HOST_NOW - timedelta(hours=3)), hosts="h051,h053,h106,h202"
        )
        + "\n"
    )
    options.delivery_state_file.write_text(
        json.dumps(
            {
                "pending": {},
                "delivered": {},
                "open": {
                    "quiet-host": {
                        "started": "2026-10-28T05:05:05Z",
                        "escalated": "2026-10-28T07:09:39Z",
                        "hosts": [
                            "h051",
                            "h053",
                            "h101",
                            "h105",
                            "h106",
                            "h200",
                            "h201",
                            "h202",
                        ],
                    }
                },
            }
        )
    )
    raised = _tick(options, HOST_NOW)
    assert set(raised) == set(hosts), "h201 reads quiet and no row names it: raise one"
    assert set(_open_alarm(options)["hosts"]) == set(hosts)  # type: ignore[arg-type]
    # Control: once a row names all four, the same tick is deduped.
    assert _tick(options, HOST_NOW + timedelta(minutes=10)) == ()


def test_quiet_host_dedupe_host_the_newest_row_names_stays_deduped(
    tmp_path: Path,
) -> None:
    options = world(tmp_path)
    named = ",".join(sorted(RECORDED))
    with options.ledger.open("a") as handle:  # type: ignore[union-attr]
        handle.write(
            ROW.format(stamp=_stamp(HOST_NOW - timedelta(hours=3)), hosts=named) + "\n"
        )
    options.delivery_state_file.write_text(
        json.dumps(
            {
                "pending": {},
                "delivered": {},
                "open": {
                    "quiet-host": {
                        "started": _stamp(HOST_NOW - timedelta(hours=3)),
                        "escalated": _stamp(HOST_NOW - timedelta(hours=1)),
                        "hosts": sorted(RECORDED),
                    }
                },
            }
        )
    )
    assert _tick(options, HOST_NOW) == ()
    assert options.ledger.read_text().count("class=quiet-host") == 1  # type: ignore[union-attr]
