# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An uninstall of a systemd unit resets the failed state of its service and timer.

systemctl keeps a removed unit whose last run failed in ``systemctl --user --failed`` as not-found/failed;
the uninstall runs ``reset-failed`` for both units after the daemon-reload, and ignores the non-zero exit
of a reset-failed that has nothing to reset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    HANDLER,
    LABEL,
    LINUX_PYTHON,
    Recorder,
    request,
    unit_paths,
)


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def installed_unit_files(tmp_path: Path) -> tuple[Path, Path]:
    service, timer = unit_paths(tmp_path)
    service.parent.mkdir(parents=True)
    service.write_text("service\n", encoding="utf-8")
    timer.write_text("timer\n", encoding="utf-8")
    return service, timer


def test_an_uninstall_resets_the_failed_state_of_the_service_and_timer(
    tmp_path: Path,
) -> None:
    service, timer = installed_unit_files(tmp_path)
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(request(tmp_path, mode="uninstall"))
    assert result.status == "success", result.stderr
    assert not service.exists()
    assert not timer.exists()
    calls = run.systemctl()
    reloaded = calls.index(["daemon-reload"])
    assert calls.index(["reset-failed", f"{LABEL}.service"]) > reloaded
    assert calls.index(["reset-failed", f"{LABEL}.timer"]) > reloaded


def test_an_uninstall_still_succeeds_when_reset_failed_is_refused(
    tmp_path: Path,
) -> None:
    service, timer = installed_unit_files(tmp_path)
    result = HandlerRegistryUnits(Recorder(fail="reset-failed")).handle(
        request(tmp_path, mode="uninstall")
    )
    assert result.status == "success", result.stderr
    assert not service.exists()
    assert not timer.exists()


def test_an_uninstall_of_a_unit_that_is_not_installed_still_resets_failed_state(
    tmp_path: Path,
) -> None:
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(request(tmp_path, mode="uninstall"))
    assert result.status == "success", result.stderr
    service, timer = unit_paths(tmp_path)
    assert not service.exists()
    assert not timer.exists()
    calls = run.systemctl()
    assert ["reset-failed", f"{LABEL}.service"] in calls
    assert ["reset-failed", f"{LABEL}.timer"] in calls
