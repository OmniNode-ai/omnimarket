# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_net_units: systemd drop-in files as allowed destinations.

Every payload, host name and number below is fixture input written into a temporary overlay; the node holds none.
"""

from __future__ import annotations

import hashlib
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
from omnimarket.nodes.node_host_installer_net_units.node import (
    NodeHostInstallerNetUnits,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_installer_net_units"

UNIT_TEMPLATE = (
    "[Unit]\n"
    "Description=embeddings service\n"
    "\n"
    "[Service]\n"
    "User=svc\n"
    "ExecStart=/usr/local/bin/embeddings\n"
    "\n"
    "[Install]\n"
    "WantedBy=multi-user.target\n"
)

DROPCONF_CONTENT = "[Service]\nEnvironment=EMBEDDINGS_PORT=5090\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_overlay(path: Path, document: dict[str, Any]) -> Path:
    path.mkdir(parents=True)
    (path / "embeddings.service.tmpl").write_text(UNIT_TEMPLATE)
    (path / "units.yaml").write_text(yaml.safe_dump(document))
    return path


def fixture_document() -> dict[str, Any]:
    return {
        "schema": "host_net_units.v1",
        "units": {
            "embeddings": {
                "kind": "systemd-system-service",
                "platform": "linux",
                "hosts": ["pool-a"],
                "service": "embeddings.service",
                "files": [
                    {
                        "source": "embeddings.service.tmpl",
                        "destination": "/etc/systemd/system/embeddings.service",
                        "mode": "0644",
                        "sha256": sha(UNIT_TEMPLATE),
                    },
                    {
                        "content": DROPCONF_CONTENT,
                        "destination": "/etc/systemd/system/embeddings.service.d/20-5090.conf",
                        "mode": "0644",
                        "sha256": sha(DROPCONF_CONTENT),
                    },
                ],
            }
        },
    }


class FakeHost:
    """The command side of a host: systemctl, recorded; the file side is a real directory."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.states: dict[str, bool] = {"embeddings.service": True}

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        name = Path(argv[0]).name
        if name == "systemctl":
            verb = argv[1]
            unit = argv[-1]
            if verb == "is-active":
                up = self.states.get(unit, False)
                return subprocess.CompletedProcess(
                    argv, 0 if up else 3, "active\n" if up else "inactive\n", ""
                )
            if verb in ("enable", "start", "restart"):
                self.states[unit] = True
            if verb == "disable":
                self.states[unit] = False
            return subprocess.CompletedProcess(argv, 0, "", "")
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


def test_host_net_dropin_golden_install_writes_both_files(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    h = handler(host, fs)
    result = h.handle(request(overlay, "embeddings"))
    assert result.status == "success", result.stderr
    unit_file = fs / "etc/systemd/system/embeddings.service"
    dropin_file = fs / "etc/systemd/system/embeddings.service.d/20-5090.conf"
    assert unit_file.exists()
    assert dropin_file.exists()
    assert unit_file.read_bytes() == UNIT_TEMPLATE.encode()
    assert dropin_file.read_bytes() == DROPCONF_CONTENT.encode()
    assert result.digests == {
        "/etc/systemd/system/embeddings.service": sha(UNIT_TEMPLATE),
        "/etc/systemd/system/embeddings.service.d/20-5090.conf": sha(DROPCONF_CONTENT),
    }
    daemon_reload_calls = [
        c
        for c in host.calls
        if Path(c[0]).name == "systemctl" and c[1] == "daemon-reload"
    ]
    assert len(daemon_reload_calls) == 1


def test_host_net_dropin_status_and_dry_run_read_present_and_matching(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    h = handler(host, fs)
    result = h.handle(request(overlay, "embeddings"))
    assert result.status == "success", result.stderr
    unit_destination = doc["units"]["embeddings"]["files"][0]["destination"]
    dropin_destination = doc["units"]["embeddings"]["files"][1]["destination"]
    status_result = h.handle(request(overlay, "embeddings", "status"))
    assert status_result.status == "success", status_result.stderr
    assert f"identical {unit_destination}\n" in status_result.stdout
    assert f"identical {dropin_destination}\n" in status_result.stdout
    dry_run_result = h.handle(request(overlay, "embeddings", "dry-run"))
    assert dry_run_result.status == "success", dry_run_result.stderr
    assert dry_run_result.stdout == (
        f"unchanged {unit_destination}\n"
        f"unchanged {dropin_destination}\n"
        "no change: embeddings already matches its declaration\n"
    )


def test_host_net_dropin_serving_unit_with_a_dropin_without_restart_installs(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["serving"] = True
    # The unit file itself keeps Restart=always; the drop-in has none, and a drop-in
    # of a serving unit is not subject to the serving restart-policy check.
    serving_unit = UNIT_TEMPLATE.replace(
        "\n[Install]\n", "Restart=always\n\n[Install]\n"
    )
    doc["units"]["embeddings"]["files"][0]["sha256"] = sha(serving_unit)
    overlay = make_overlay_with_doc(tmp_path, doc)
    (overlay / "embeddings.service.tmpl").write_text(serving_unit)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "success", result.stderr
    assert (fs / "etc/systemd/system/embeddings.service.d/20-5090.conf").exists()


def test_host_net_dropin_serving_unit_file_without_restart_is_still_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["serving"] = True
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    result = handler(FakeHost(), fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "serving-restart-policy"
    assert not (fs / "etc/systemd/system/embeddings.service").exists()


def test_host_net_dropin_different_unit_dir_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["files"][1]["destination"] = (
        "/etc/systemd/system/other.service.d/20-5090.conf"
    )
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not (fs / "etc/systemd/system/other.service.d/20-5090.conf").exists()


def test_host_net_dropin_etc_passwd_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["files"][1]["destination"] = "/etc/passwd"
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not (fs / "etc/passwd").exists()


def test_host_net_dropin_nested_subdirectory_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["files"][1]["destination"] = (
        "/etc/systemd/system/embeddings.service.d/sub/20-5090.conf"
    )
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not (fs / "etc/systemd/system/embeddings.service.d").exists()


def test_host_net_dropin_non_conf_name_in_d_dir_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["files"][1]["destination"] = (
        "/etc/systemd/system/embeddings.service.d/20-5090.txt"
    )
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not (fs / "etc/systemd/system/embeddings.service.d").exists()


def test_host_net_dropin_path_with_dotdot_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["files"][1]["destination"] = (
        "/etc/systemd/system/../embeddings.service.d/20-5090.conf"
    )
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not fs.exists()


def test_host_net_dropin_sshd_dropin_kind_pointing_at_systemd_d_path_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["kind"] = "sshd-dropin"
    doc["units"]["embeddings"]["files"] = [
        {
            "content": DROPCONF_CONTENT,
            "destination": "/etc/systemd/system/embeddings.service.d/20-5090.conf",
            "mode": "0644",
            "sha256": sha(DROPCONF_CONTENT),
        }
    ]
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not (fs / "etc/systemd/system/embeddings.service.d").exists()


def test_host_net_dropin_failing_control_directly_under_roots_refuses_declared_dropin(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["embeddings"]["files"][1]["destination"] = (
        "/etc/systemd/system/sub/20-5090.conf"
    )
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "embeddings"))
    assert result.status == "refused"
    assert result.reason == "destination-not-allowed"
    assert not (fs / "etc/systemd/system/sub/20-5090.conf").exists()


def test_host_net_dropin_golden_chain_through_node_dry_run(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    node = NodeHostInstallerNetUnits()
    node.handler = HandlerNetUnits(
        run=host, system=lambda: "Linux", sudo=[], fs_root=fs
    )
    result = node.handle(request(overlay, "embeddings", "dry-run"))
    assert result.status == "success", result.stderr
