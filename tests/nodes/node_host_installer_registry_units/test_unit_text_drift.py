# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the status of a systemd unit compares its installed files with the render (OMN-23956).

A host ran a unit file that differed from what its declaration renders (a 120 s run limit against 600 s, one missing
setting) and the status check, which only read the timer and the heartbeat, passed. The status now prints one
``UNIT TEXT`` line per installed file and fails when a file differs from the text the installer renders today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    HANDLER,
    LINUX_PYTHON,
    UNIT,
    Recorder,
    make_checkout,
    request,
    unit_paths,
)

pytestmark = pytest.mark.unit

HEARTBEAT = ".local/state/omni/overlay-store-refresh/last-run.json"


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def heartbeat(tmp_path: Path) -> None:
    evidence = tmp_path / "home" / HEARTBEAT
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("{}\n", encoding="utf-8")


def install_rendered(tmp_path: Path, branch: Path) -> tuple[str, str]:
    """Write the unit files the installer would write today and return their text."""
    run = Recorder()
    service, timer = HandlerRegistryUnits(run).render_systemd(
        request(tmp_path, project_path=str(branch)),
        load_units()[UNIT],
        branch,
        "/usr/bin/uv",
        LINUX_PYTHON,
    )
    service_path, timer_path = unit_paths(tmp_path)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service, encoding="utf-8")
    timer_path.write_text(timer, encoding="utf-8")
    return service, timer


def test_golden_status_reads_in_sync_when_the_installed_text_equals_the_render(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    make_checkout(branch)
    install_rendered(tmp_path, branch)
    heartbeat(tmp_path)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, mode="status", project_path=str(branch))
    )
    assert result.status == "success", result.stdout
    assert result.stdout.count("UNIT TEXT IN SYNC") == 2
    assert "DIFFERS" not in result.stdout


def test_error_status_fails_and_names_the_differing_line_when_the_text_differs(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    make_checkout(branch)
    service, _timer = install_rendered(tmp_path, branch)
    heartbeat(tmp_path)
    service_path, _ = unit_paths(tmp_path)
    exec_line = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    service_path.write_text(
        service.replace(exec_line, exec_line + " --stale"), encoding="utf-8"
    )
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, mode="status", project_path=str(branch))
    )
    assert result.status == "failure"
    assert f"UNIT DIFFERS FROM RENDER {service_path}" in result.stdout
    assert f"  installed: {exec_line} --stale" in result.stdout
    assert f"  rendered: {exec_line}" in result.stdout
    assert result.stdout.count("UNIT TEXT IN SYNC") == 1


def test_a_branch_install_is_judged_against_the_project_its_record_names(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    make_checkout(branch)
    install_rendered(tmp_path, branch)
    heartbeat(tmp_path)
    record = (
        tmp_path
        / "omni"
        / ".onex_state/overlay-store-refresh"
        / f"{UNIT}.installed.json"
    )
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f'{{"project": "{branch}", "revision": "x"}}\n', encoding="utf-8")
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, mode="status"))
    assert result.status == "success", result.stdout
    assert result.stdout.count("UNIT TEXT IN SYNC") == 2


def test_error_status_names_a_file_it_cannot_compare(tmp_path: Path) -> None:
    heartbeat(tmp_path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, mode="status"))
    assert result.status == "success", result.stdout
    assert result.stdout.count("UNIT TEXT NOT COMPARED") == 2
    assert "unreadable" in result.stdout


def test_error_status_names_a_unit_it_cannot_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat(tmp_path)
    monkeypatch.setattr(f"{HANDLER}.interpreter_present", lambda _path: False)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, mode="status"))
    assert "UNIT TEXT NOT COMPARED" in result.stdout
    assert "interpreters" in result.stdout
