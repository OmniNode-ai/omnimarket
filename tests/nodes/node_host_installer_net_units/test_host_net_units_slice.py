# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_net_units: a systemd system slice declared as data, installed with its limits read back.

Every payload, host name and number below is fixture input written into a temporary overlay; the node holds none.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_host_installer_net_units.handlers.handler_net_units import (
    HandlerNetUnits,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsRequest,
)

SLICE = "fixture-pool.slice"

TEMPLATE = (
    "[Slice]\n"
    "MemoryMax=16106127360\n"
    "MemorySwapMax=0\n"
    "CPUQuotaPerSecUSec=200%\n"
    "TasksMax=8192\n"
)


def make_overlay(path: Path, document: dict[str, Any]) -> Path:
    path.mkdir(parents=True)
    (path / SLICE).write_text(TEMPLATE)
    (path / "units.yaml").write_text(yaml.safe_dump(document))
    return path


def fixture_document() -> dict[str, Any]:
    return {
        "schema": "host_net_units.v1",
        "units": {
            "pool": {
                "kind": "systemd-system-slice",
                "platform": "linux",
                "hosts": ["pool-a"],
                "slice": SLICE,
                "files": [
                    {
                        "source": SLICE,
                        "destination": f"/etc/systemd/system/{SLICE}",
                        "mode": "0644",
                    }
                ],
            }
        },
    }


class FakeHost:
    """The command side of a host: systemctl recorded; `show` answers from a table the test edits."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.properties: dict[str, str] = {
            "MemoryMax": "16106127360",
            "MemorySwapMax": "0",
            "CPUQuotaPerSecUSec": "2000000",
            "TasksMax": "8192",
        }

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if Path(argv[0]).name != "systemctl":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "show":
            flags = argv[3:]  # ["-p", Name, "-p", Name, ...] after the unit
            out = "".join(
                f"{name}={self.properties.get(name, '[not set]')}\n"
                for name in flags[1::2]
            )
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def handler(host: FakeHost, fs: Path) -> HandlerNetUnits:
    return HandlerNetUnits(run=host, system=lambda: "Linux", sudo=[], fs_root=fs)


def request(
    overlay: Path, unit: str, mode: str = "install", host_name: str = "pool-a"
) -> ModelNetUnitsRequest:
    return ModelNetUnitsRequest(
        unit=unit, mode=mode, host_name=host_name, overlay_root=str(overlay)
    )


def make_overlay_with_doc(tmp_path: Path, document: dict[str, Any]) -> Path:
    return make_overlay(tmp_path / "overlay", document)


def verbs(host: FakeHost) -> list[str]:
    return [call[1] for call in host.calls if call[0] == "systemctl"]


def test_host_net_slice_golden_install_then_no_change(tmp_path: Path) -> None:
    overlay = make_overlay_with_doc(tmp_path, fixture_document())
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "pool"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    unit_file = fs / "etc/systemd/system/fixture-pool.slice"
    assert unit_file.read_bytes() == TEMPLATE.encode()
    assert verbs(host) == ["daemon-reload", "start", "show"]
    assert host.calls[-1][2] == SLICE
    assert "MemoryMax=16106127360\n" in result.stdout
    assert "MemorySwapMax=0\n" in result.stdout
    # Second install: no file differs and the slice already reads bounded.
    again = handler(host, fs).handle(request(overlay, "pool"))
    assert again.status == "success", again.stderr
    assert again.changed is False
    assert again.stdout.startswith("no change:")
    assert verbs(host) == ["daemon-reload", "start", "show", "show"]


def test_host_net_slice_unbounded_memory_max_fails_the_install(tmp_path: Path) -> None:
    overlay = make_overlay_with_doc(tmp_path, fixture_document())
    host = FakeHost()
    host.properties["MemoryMax"] = "infinity"
    result = handler(host, tmp_path / "fs").handle(request(overlay, "pool"))
    assert result.status == "failure"
    assert result.exit_code == 1
    assert result.reason == "slice-limit-unapplied"
    assert "MemoryMax" in result.stderr


def test_host_net_slice_nonzero_swap_max_fails_the_install(tmp_path: Path) -> None:
    overlay = make_overlay_with_doc(tmp_path, fixture_document())
    host = FakeHost()
    host.properties["MemorySwapMax"] = "1073741824"
    result = handler(host, tmp_path / "fs").handle(request(overlay, "pool"))
    assert result.status == "failure"
    assert result.exit_code == 1
    assert result.reason == "slice-limit-unapplied"
    assert "MemorySwapMax" in result.stderr


def test_host_net_slice_without_a_slice_declaration_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    del doc["units"]["pool"]["slice"]
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    result = handler(FakeHost(), fs).handle(request(overlay, "pool"))
    assert result.status == "refused"
    assert result.reason == "slice-not-declared"
    assert result.exit_code == 2
    assert not (fs / "etc/systemd/system/fixture-pool.slice").exists()


def test_host_net_slice_name_that_is_no_file_basename_is_refused(
    tmp_path: Path,
) -> None:
    for wrong in ("other-pool.slice", "fixture-pool"):
        doc = fixture_document()
        doc["units"]["pool"]["slice"] = wrong
        overlay = make_overlay(tmp_path / f"overlay-{wrong}", doc)
        fs = tmp_path / "fs"
        result = handler(FakeHost(), fs).handle(request(overlay, "pool"))
        assert result.status == "refused", wrong
        assert result.reason == "slice-file-mismatch", wrong
        assert result.exit_code == 2, wrong
        assert not (fs / "etc/systemd/system/fixture-pool.slice").exists()


def test_host_net_slice_dry_run_writes_and_starts_nothing(tmp_path: Path) -> None:
    overlay = make_overlay_with_doc(tmp_path, fixture_document())
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "pool", "dry-run"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    assert host.calls == []
    assert not (fs / "etc/systemd/system/fixture-pool.slice").exists()
    assert "would write /etc/systemd/system/fixture-pool.slice" in result.stdout


def test_host_net_slice_status_reads_limits_and_fails_when_unbounded(
    tmp_path: Path,
) -> None:
    overlay = make_overlay_with_doc(tmp_path, fixture_document())
    fs = tmp_path / "fs"
    host = FakeHost()
    h = handler(host, fs)
    assert h.handle(request(overlay, "pool")).status == "success"
    fine = h.handle(request(overlay, "pool", "status"))
    assert fine.status == "success", fine.stderr
    assert "MemorySwapMax=0\n" in fine.stdout
    host.properties["MemoryMax"] = "infinity"
    result = h.handle(request(overlay, "pool", "status"))
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "MemoryMax=infinity\n" in result.stdout
    assert "fixture-pool.slice MemoryMax is not limited as declared\n" in result.stdout


def test_host_net_slice_uninstall_stops_removes_and_reloads(
    tmp_path: Path,
) -> None:
    overlay = make_overlay_with_doc(tmp_path, fixture_document())
    fs = tmp_path / "fs"
    host = FakeHost()
    h = handler(host, fs)
    assert h.handle(request(overlay, "pool")).status == "success"
    result = h.handle(request(overlay, "pool", "uninstall"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    assert not (fs / "etc/systemd/system/fixture-pool.slice").exists()
    assert verbs(host) == ["daemon-reload", "start", "show", "stop", "daemon-reload"]
    assert any(call[1] == "stop" and call[2] == SLICE for call in host.calls)


def test_host_net_slice_unknown_kind_is_still_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    doc["units"]["pool"]["kind"] = "systemd-system-timer"
    overlay = make_overlay_with_doc(tmp_path, doc)
    result = handler(FakeHost(), tmp_path / "fs").handle(request(overlay, "pool"))
    assert result.status == "refused"
    assert result.reason == "kind-not-supported"
