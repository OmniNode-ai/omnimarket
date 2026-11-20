# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The h053 tailnet status shape, the warning boundary and the independent push."""

from __future__ import annotations

import asyncio
import json
import plistlib
import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.adapters import (
    BusLaneLedgerAppender,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect import (
    runtime_key_expiry_tick as tick,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.adapters.tailnet_status import (
    read_status,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.handlers.handler_key_expiry_alarm import (
    HandlerKeyExpiryAlarm,
)
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.models import (
    ModelKeyExpiryRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 19, 13, tzinfo=UTC)
FIXTURE = Path(__file__).with_name("status_h053_scrubbed.json")
UNIT = "tailscale-key-expiry-alarm"


def _status(*entries: tuple[str, int]) -> dict[str, Any]:
    status = json.loads(FIXTURE.read_text())
    for peer, days in entries:
        status["Peer"][peer]["KeyExpiry"] = (NOW + timedelta(days=days)).isoformat()
    return status


@pytest.mark.parametrize(("days", "expected"), [(13, 1), (14, 1), (15, 0)])
def test_key_expiry_alarm(days: int, expected: int) -> None:
    readings = read_status(
        _status(("peer-1", days), ("peer-4", 15)),
        {"lab-1": ("peer-1",), "lab-2": ("peer-4",)},
        NOW,
    )
    assert readings is not None
    result = HandlerKeyExpiryAlarm().handle(
        ModelKeyExpiryRequest(now=NOW, owner="lab-host-pool-owner", readings=readings)
    )
    assert len(result.alarms) == expected
    for row in result.alarms:
        assert row.severity == "ALARM"
        assert row.subject == "lab-1"
        assert row.owner == "lab-host-pool-owner"
        assert row.expires_at == NOW + timedelta(days=days)


def _rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], Path, Path]:
    roots = tmp_path / "overlays"
    node_overlay = roots / "node_tailscale_key_expiry_alarm_effect" / "overlay.yaml"
    node_overlay.parent.mkdir(parents=True)
    node_overlay.write_text(
        "owner: lab-host-pool-owner\nchannel_library: alert-channel.sh\n"
    )
    table = roots / "lab_hosts" / "overlay.yaml"
    table.parent.mkdir()
    table.write_text(
        "registry_root_candidates: [/example/registry]\n"
        "hosts:\n  - name: lab-1\n    target: peer-1\n    available: false\n"
    )
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    state = tmp_path / "state"
    log = tmp_path / "push.log"
    (tmp_path / "alert-channel.sh").write_text(
        'alert_channel_alarm() { printf "%s\\n" "$2" >> '
        f"{shlex.quote(str(log))}; printf '1795093200.000001\\n'; }}\n"
    )
    return ["--omni-home", str(tmp_path), "--state-dir", str(state)], state, log


def test_key_expiry_delivery_without_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("every event-bus publish is refused")

    monkeypatch.setattr(EventBusKafka, "publish", refuse)
    monkeypatch.setattr(EventBusInmemory, "publish", refuse)
    monkeypatch.setattr(BusLaneLedgerAppender, "append", refuse)
    with pytest.raises(RuntimeError, match="every event-bus publish is refused"):
        asyncio.run(EventBusInmemory().publish("control", None, b"control"))
    args, state, log = _rig(tmp_path, monkeypatch)
    monkeypatch.setattr(tick, "read_tailscale_status", lambda: _status(("peer-1", 13)))
    monkeypatch.setattr(tick, "utc_now", lambda: NOW)
    assert tick.main(args) == 0
    assert tick.main(args) == 0
    rows = list((state / "alarms").glob("*.json"))
    assert len(rows) == 1
    row = json.loads(rows[0].read_text())
    assert row["severity"] == "ALARM"
    assert row["owner"] == "lab-host-pool-owner"
    assert row["message_ref"] == "1795093200.000001"
    assert log.read_text().count("ALARM") == 1


def test_key_expiry_alarm_reads_self_and_expired_peers() -> None:
    status = _status(("peer-1", 15))
    status["Self"]["KeyExpiry"] = "2026-11-21T12:00:00Z"
    status["Peer"]["peer-1"].pop("KeyExpiry")
    status["Peer"]["peer-1"]["Expired"] = True
    readings = read_status(
        status,
        {"lab-1": ("login@peer-1.example.test",)},
        NOW,
        watching_host="operator-mac",
    )
    assert readings is not None
    result = HandlerKeyExpiryAlarm().handle(
        ModelKeyExpiryRequest(now=NOW, owner="lab-host-pool-owner", readings=readings)
    )
    assert {row.subject: row.expires_at for row in result.alarms} == {
        "lab-1": None,
        "operator-mac": datetime(2026, 11, 21, 12, tzinfo=UTC),
    }


def test_key_expiry_delivery_from_cache_when_status_command_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, state, log = _rig(tmp_path, monkeypatch)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "tailscale"
    fixture = tmp_path / "status.json"
    fixture.write_text(json.dumps(_status(("peer-1", 15))))
    binary.write_text(
        '#!/bin/sh\n[ "$*" = "status --json" ] || exit 2\n'
        f"cat {shlex.quote(str(fixture))}\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_dir), prepend=":")
    monkeypatch.setattr(tick, "utc_now", lambda: NOW)
    assert tick.main(args) == 0
    assert not log.exists()

    binary.write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setattr(tick, "utc_now", lambda: NOW + timedelta(days=1))
    assert tick.main(args) == 0
    assert tick.main(args) == 0
    assert log.read_text().count("ALARM") == 1
    run = json.loads((state / "last-run.json").read_text())
    assert run["source"] == "cache"
    assert run["cached_hosts"] == ["lab-1"]

    monkeypatch.setattr(tick, "utc_now", lambda: NOW + timedelta(hours=49))
    assert tick.main(args) == 3
    assert log.read_text().count("ALARM") == 1


def test_key_expiry_delivery_retries_refusal_without_raising_another_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, state, log = _rig(tmp_path, monkeypatch)
    library = tmp_path / "alert-channel.sh"
    success = library.read_text()
    library.write_text("alert_channel_alarm() { return 1; }\n")
    monkeypatch.setattr(tick, "read_tailscale_status", lambda: _status(("peer-1", 13)))
    monkeypatch.setattr(tick, "utc_now", lambda: NOW)
    assert tick.main(args) == 2
    (row,) = (state / "alarms").glob("*.json")
    assert json.loads(row.read_text())["message_ref"] is None
    assert not log.exists()

    library.write_text(success)
    assert tick.main(args) == 0
    assert list((state / "alarms").glob("*.json")) == [row]
    assert json.loads(row.read_text())["message_ref"] == "1795093200.000001"
    assert log.read_text().count("ALARM") == 1


def test_key_expiry_unit_renders_an_independent_six_hour_launchd_tick(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    assert (Path(__file__).resolve().parents[3] / unit["entry_file"]).is_file()
    assert unit["run_timeout_s"] < unit["schedule"]["start_interval"]
    assert unit["probe_argv"] == ["--probe"]
    request = ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
    )
    installer = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    plist = plistlib.loads(
        installer.render(request, unit, tmp_path, "uv", "python").encode()
    )
    assert plist["Label"] == "ai.omninode.tailscale-key-expiry-alarm"
    assert plist["StartInterval"] == 21600
    assert plist["RunAtLoad"] is True
    assert tick.__name__ in plist["ProgramArguments"]
    assert (
        str(tmp_path / ".local/state/omni/tailscale-key-expiry-alarm")
        in plist["ProgramArguments"]
    )
    assert plist["EnvironmentVariables"]["TAILSCALE_BE_CLI"] == "1"
