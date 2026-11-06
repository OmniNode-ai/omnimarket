# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm reads each pool host's hook-event name from the host table (OMN-22755).

On 2026-11-06 the quiet-host alarm that ran on the operator host held a hand list of five hosts.
Events sent by three other pool hosts were dropped as "a hostname nobody declared", so those hosts
read QUIET whenever a lane row named them, whatever their events showed. The aliases are now
derived from the host table the deployment names. The control in each reading below is the same
tick over a table without the host: it must read QUIET, which shows the live reading depends on
the table and not on the fixture. Host names here are synthetic; the real table is deployment data.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.silent_failure_alarm import ModelDrainerObservation
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.host_table import (
    HostTableError,
    read_host_aliases,
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

# Each recorded host answers to one machine name; h053 also answers to a second, as a host with two
# names would.
NAMES = {
    "h051": ["alpha-box"],
    "h053": ["beta-box", "Beta-Box.example.test"],
    "h101": ["gamma"],
    "h105": ["delta"],
    "h106": ["epsilon"],
}


def _table(path: Path, names: dict[str, list[str]]) -> Path:
    lines = ["schema: host-table.v1", "hosts:"]
    for host, hostnames in names.items():
        lines += [
            f"  {host}:",
            "    role: lab",
            f"    hostnames: [{', '.join(hostnames)}]",
        ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _tick(
    tmp_path: Path, table: Path, event_host: str
) -> tuple[tuple[str, ...], list[str]]:
    """One tick whose only hook event came from ``event_host`` a minute before the ledger rows end."""
    base = world(tmp_path)
    stamp = (HOST_NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = tmp_path / "events.txt"
    events.write_text(f"{event_host}|{stamp}\n")
    options = ModelSilentFailureAlarmOptions.model_validate(
        {
            **base.model_dump(exclude={"host_aliases"}),
            "host_table": table,
        }
    )
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in RECORDED
    }
    result = HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW)
    )
    quiet = result.decision.quiet_hosts if result.decision is not None else ()
    return quiet, list(result.warnings)


def test_host_table_yields_each_hosts_event_labels(tmp_path: Path) -> None:
    aliases = read_host_aliases(_table(tmp_path / "hosts.yaml", NAMES))
    assert aliases["h051"] == ("alpha-box",)
    # A second name of the same label collapses; a different DNS suffix is not a different label.
    assert aliases["h053"] == ("beta-box",)
    assert set(aliases) == set(NAMES)


def test_host_table_change_flows_into_the_aliases(tmp_path: Path) -> None:
    table = _table(tmp_path / "hosts.yaml", NAMES)
    before = ModelSilentFailureAlarmOptions(registry_root=tmp_path, host_table=table)
    assert "h300" not in before.host_aliases
    _table(table, {**NAMES, "h300": ["zeta"], "h106": ["epsilon-two", "epsilon"]})
    after = ModelSilentFailureAlarmOptions(registry_root=tmp_path, host_table=table)
    assert after.host_aliases["h300"] == ("zeta",)
    assert after.host_aliases["h106"] == ("epsilon-two", "epsilon")
    assert after.host_table_error == ""


def test_host_table_change_flows_into_the_quiet_host_reading(tmp_path: Path) -> None:
    table = _table(tmp_path / "hosts.yaml", NAMES)
    quiet, warnings = _tick(tmp_path, table, "Beta-Box.example.test")
    assert warnings == []
    assert "h053" not in quiet
    assert (
        "h051" in quiet
    )  # no event of its own, so still quiet: the reading is per host


def test_host_table_without_the_host_reads_it_quiet_control(tmp_path: Path) -> None:
    """The failing control: the same event, a table that does not declare the sender."""
    table = _table(
        tmp_path / "hosts.yaml", {h: n for h, n in NAMES.items() if h != "h053"}
    )
    quiet, _ = _tick(tmp_path, table, "Beta-Box.example.test")
    assert "h053" in quiet


def test_host_table_unreadable_leaves_events_unattributed_and_says_why(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no-such-table.yaml"
    options = ModelSilentFailureAlarmOptions(registry_root=tmp_path, host_table=missing)
    assert options.host_aliases == {}
    assert "no-such-table.yaml" in options.host_table_error
    quiet, warnings = _tick(tmp_path, missing, "beta-box")
    assert quiet == ()  # UNKNOWN names no host as quiet and no host as live
    assert any("host table unreadable" in w for w in warnings), warnings


def test_host_table_unreadable_is_refused_for_each_malformed_shape(
    tmp_path: Path,
) -> None:
    for text in (
        "hosts: []\n",
        "not: a table\n",
        "hosts:\n  h1: {role: lab}\n",
        ": : :\n",
    ):
        bad = tmp_path / "bad.yaml"
        bad.write_text(text)
        with pytest.raises(HostTableError):
            read_host_aliases(bad)


def test_host_table_label_shared_by_two_hosts_is_refused(tmp_path: Path) -> None:
    table = _table(tmp_path / "hosts.yaml", {"h1": ["same"], "h2": ["Same.local"]})
    with pytest.raises(HostTableError, match="belongs to h1 and h2"):
        read_host_aliases(table)
    assert (
        "belongs to h1 and h2"
        in ModelSilentFailureAlarmOptions(
            registry_root=tmp_path, host_table=table
        ).host_table_error
    )


def test_host_table_and_listed_aliases_together_are_refused(tmp_path: Path) -> None:
    table = _table(tmp_path / "hosts.yaml", NAMES)
    with pytest.raises(ValidationError, match="not both"):
        ModelSilentFailureAlarmOptions(
            registry_root=tmp_path, host_table=table, host_aliases={"h051": "alpha-box"}
        )


def test_host_table_relative_path_is_read_from_the_configuration_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "state"
    config_dir.mkdir()
    _table(config_dir / "hosts.yaml", NAMES)
    config = config_dir / "config.json"
    config.write_text(
        json.dumps({"registry_root": str(tmp_path), "host_table": "hosts.yaml"})
    )
    monkeypatch.setenv("ONEX_SILENT_FAILURE_ALARM_CONFIG", str(config))
    options = ModelSilentFailureAlarmOptions.from_environment()
    assert options.host_aliases["h106"] == ("epsilon",)
    assert options.host_table_error == ""
