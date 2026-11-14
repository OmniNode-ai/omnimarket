# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: uninstalling a systemd unit also clears its failed state (OMN-23961).

``systemctl --user --failed`` kept listing the service and timer of a unit the installer had removed on purpose, which
lengthened the list of failing jobs and hid the ones that really fail.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (  # noqa: F401  (linux_host is the shared fixture that makes the host a Linux pool host)
    LABEL,
    Recorder,
    linux_host,
    request,
    unit_paths,
)


def installed_pair(tmp_path: Path) -> tuple[Path, Path]:
    service, timer = unit_paths(tmp_path)
    service.parent.mkdir(parents=True)
    service.write_text("service\n", encoding="utf-8")
    timer.write_text("timer\n", encoding="utf-8")
    return service, timer


def test_uninstall_clears_the_failed_state_of_the_service_and_the_timer(
    tmp_path: Path,
) -> None:
    service, timer = installed_pair(tmp_path)
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(request(tmp_path, mode="uninstall"))
    assert result.status == "success", result.stderr
    assert not service.exists()
    assert not timer.exists()
    resets = [c for c in run.systemctl() if "reset-failed" in c]
    named = {name for c in resets for name in c if name.startswith(LABEL)}
    assert named == {f"{LABEL}.service", f"{LABEL}.timer"}, run.systemctl()
    # the failed mark of a unit file that is gone is cleared after the file is removed and systemd has reread
    calls = run.systemctl()
    assert calls.index(["daemon-reload"]) < calls.index(resets[0])


def test_uninstall_still_succeeds_when_the_host_refuses_to_clear_the_failed_state(
    tmp_path: Path,
) -> None:
    service, timer = installed_pair(tmp_path)
    run = Recorder(fail="reset-failed")
    result = HandlerRegistryUnits(run).handle(request(tmp_path, mode="uninstall"))
    assert result.status == "success", result.stderr
    assert result.exit_code == 0
    assert not service.exists()
    assert not timer.exists()
    assert any("reset-failed" in c for c in run.systemctl())
