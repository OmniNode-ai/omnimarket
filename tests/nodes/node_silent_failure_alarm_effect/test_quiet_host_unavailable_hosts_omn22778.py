# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The quiet-host reading skips a host the lab host table declares unavailable (OMN-22778).

A host declared ``available: false`` takes no lane, so a ledger row that names it (a landing
worker's note, a timer row) is not lane work that lost its hook events, and a quiet reading of it
is false. The reading below is the same tick as test_host_table_aliases_omn22755: the control reads
the unavailable host QUIET when nothing declares it unavailable, which shows the skip comes from the
table and not from the fixture. Host names here are synthetic; the real table is deployment data.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.silent_failure_alarm import ModelDrainerObservation
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.host_table import (
    read_unavailable_hosts,
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
from tests.nodes.node_silent_failure_alarm_effect.test_host_table_aliases_omn22755 import (
    NAMES,
    _table,
)
from tests.nodes.node_silent_failure_alarm_effect.test_quiet_host_dedupe_omn22777 import (
    _events,
    _ledger,
    _stamp,
)

pytestmark = pytest.mark.unit


def _lab_hosts(path: Path, unavailable: tuple[str, ...]) -> Path:
    """A lab_hosts overlay of the recorded hosts; the named ones carry ``available: false``."""
    lines = ["hosts:"]
    for host in NAMES:
        lines += [f"  - name: {host}", f"    target: {host}.example.test", "    cap: 1"]
        if host in unavailable:
            lines += [
                "    available: false",
                '    unavailable_reason: "synthetic: out of placement"',
            ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _tick(
    tmp_path: Path,
    unavailable: tuple[str, ...] | None,
    *,
    read_error: str = "",
) -> tuple[tuple[str, ...], list[str]]:
    """One tick where no host sent a hook event, so every host that ran lanes reads quiet."""
    base = world(tmp_path)
    events = tmp_path / "events.txt"
    events.write_text("")
    options = ModelSilentFailureAlarmOptions.model_validate(
        {
            **base.model_dump(exclude={"host_aliases"}),
            "host_table": _table(tmp_path / "hosts.yaml", NAMES),
            "skip_unavailable_hosts": True,
        }
    )
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in RECORDED
    }
    declared = frozenset(unavailable or ())
    result = HandlerSilentFailureAlarm(
        options,
        host_watches=watches,
        unavailable_hosts=lambda: (declared, read_error),
    ).handle(ModelSilentFailureAlarmRequest(now=HOST_NOW))
    rows = [row for row in result.rows if "class=quiet-host" in row]
    named = re.search(r"hosts=([a-z0-9,]+)", rows[0]) if rows else None
    return (
        tuple(named.group(1).split(",")) if named else (),
        list(result.warnings),
    )


def test_unavailable_host_is_not_read_quiet(tmp_path: Path) -> None:
    quiet, warnings = _tick(tmp_path, ("h105",))
    assert "h105" not in quiet
    assert not [w for w in warnings if "lab host table" in w]


def test_unavailable_host_reads_quiet_when_the_table_does_not_declare_it_control(
    tmp_path: Path,
) -> None:
    """The failing control: the same tick over a table that declares no host unavailable."""
    quiet, _ = _tick(tmp_path, ())
    assert "h105" in quiet


def test_available_hosts_still_read_quiet_beside_the_skipped_one(
    tmp_path: Path,
) -> None:
    quiet, _ = _tick(tmp_path, ("h105",))
    assert quiet, "an available host with lane rows and no events must still read quiet"
    assert set(quiet) <= set(NAMES) - {"h105"}


def test_unreadable_lab_host_table_skips_nothing_and_says_why(tmp_path: Path) -> None:
    quiet, warnings = _tick(tmp_path, (), read_error="no lab_hosts overlay found")
    assert "h105" in quiet
    assert any(
        "lab host table" in w and "no lab_hosts overlay found" in w for w in warnings
    )


def test_read_unavailable_hosts_names_the_hosts_the_overlay_declares(
    tmp_path: Path,
) -> None:
    table = _lab_hosts(tmp_path / "overlay.yaml", ("h105", "h106"))
    names, error = read_unavailable_hosts({"ONEX_LAB_RUN_HOSTS": str(table)})
    assert error == ""
    assert names == frozenset({"h105", "h106"})


def test_read_unavailable_hosts_reports_an_unreadable_table(tmp_path: Path) -> None:
    names, error = read_unavailable_hosts(
        {"ONEX_LAB_RUN_HOSTS": str(tmp_path / "missing.yaml")}
    )
    assert names == frozenset()
    assert error


def test_options_that_do_not_ask_skip_no_host_and_read_no_table_control(
    tmp_path: Path,
) -> None:
    """The default is hermetic: no ask, no read of any table, no warning, every host counted."""
    base = world(tmp_path)
    (tmp_path / "events.txt").write_text("")
    options = ModelSilentFailureAlarmOptions.model_validate(
        {
            **base.model_dump(exclude={"host_aliases"}),
            "host_table": _table(tmp_path / "hosts.yaml", NAMES),
        }
    )
    assert options.skip_unavailable_hosts is False
    result = HandlerSilentFailureAlarm(
        options,
        host_watches={
            host: Watch(
                ModelDrainerObservation(job_state="not-running", status_readable=False)
            )
            for host in RECORDED
        },
    ).handle(ModelSilentFailureAlarmRequest(now=HOST_NOW))
    assert not [w for w in result.warnings if "lab host table" in w]
    assert any("class=quiet-host" in row and "h105" in row for row in result.rows)


# The recorded h051 shape of 2026-11-08: three lanes placed before a planned shutdown, their closing rows
# (ssh timed out) landing after the host's last hook event. The 15:36Z and 17:47Z quiet-host rows read these
# as lanes that ran on the host and sent nothing.
CLAIM = (
    "{stamp} | CLAIM | lane={lane} | ticket=OMN-17427 | actor=script:onex-remote-lane | "
    "host=h051 | remote lane on h051"
)
FAILED_TERMINAL = (
    "{stamp} | TERMINAL | lane={lane} | ticket=OMN-17427 | actor=claude:sonnet:remote-lane | "
    "outcome=failed | host=h051 | remote lane on h051 ended status=no-result engine_rc=255; "
    "ssh: connect to host port 22: Operation timed out"
)


def _shut_down_host_tick(
    tmp_path: Path, unavailable: tuple[str, ...]
) -> tuple[str, ...]:
    """One tick over the recorded h051 shape, with ``unavailable`` the hosts the table declares out."""
    base = world(tmp_path)
    last_event = HOST_NOW - timedelta(minutes=190)
    _events(base, [f"h051|{_stamp(last_event)}"])
    with _ledger(base).open("a") as handle:
        for number, lane in enumerate(("fill-a", "fill-b", "fill-c")):
            handle.write(
                CLAIM.format(
                    stamp=_stamp(last_event - timedelta(minutes=10)), lane=lane
                )
                + "\n"
            )
            handle.write(
                FAILED_TERMINAL.format(
                    stamp=_stamp(last_event + timedelta(minutes=14 + number)),
                    lane=lane,
                )
                + "\n"
            )
    options = ModelSilentFailureAlarmOptions.model_validate(
        {
            **base.model_dump(exclude={"host_aliases"}),
            "host_table": _table(tmp_path / "hosts.yaml", NAMES),
            "skip_unavailable_hosts": True,
        }
    )
    result = HandlerSilentFailureAlarm(
        options,
        host_watches={
            host: Watch(
                ModelDrainerObservation(job_state="not-running", status_readable=False)
            )
            for host in RECORDED
        },
        unavailable_hosts=lambda: (frozenset(unavailable), ""),
    ).handle(ModelSilentFailureAlarmRequest(now=HOST_NOW))
    rows = [row for row in result.rows if "class=quiet-host" in row]
    named = re.search(r"hosts=([a-z0-9,]+)", rows[0]) if rows else None
    return tuple(named.group(1).split(",")) if named else ()


def test_closing_rows_of_lanes_that_could_not_reach_a_shut_down_host_are_not_quiet(
    tmp_path: Path,
) -> None:
    assert "h051" not in _shut_down_host_tick(tmp_path, ("h051",))


def test_closing_rows_of_lanes_on_a_host_the_table_does_not_declare_out_control(
    tmp_path: Path,
) -> None:
    """The failing control: the same rows, the table declaring nothing out, read h051 quiet (the false reading)."""
    assert "h051" in _shut_down_host_tick(tmp_path, ())
