# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_net_units: serving units with gpu_budget, render: values, and the restart-policy rule.

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

TEMPLATE = (
    "[Service]\n"
    "User=__SERVICE_USER__\n"
    "ExecStart=/bin/serve --gpu-memory-utilization __GPU_MEMORY_UTILIZATION__\n"
    "Restart=__RESTART_POLICY__\n"
    "RestartSec=__RESTART_SEC__\n"
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_overlay(path: Path, document: dict[str, Any]) -> Path:
    path.mkdir(parents=True)
    (path / "serve.service.tmpl").write_text(TEMPLATE)
    (path / "units.yaml").write_text(yaml.safe_dump(document))
    return path


def fixture_document() -> dict[str, Any]:
    return {
        "schema": "host_net_units.v1",
        "units": {
            "serve": {
                "kind": "systemd-system-service",
                "platform": "linux",
                "hosts": ["pool-a"],
                "service": "serve.service",
                "serving": True,
                "gpu_budget": {
                    "total_gib": 31.3564,
                    "engine_resident_gib": 28.26,
                    "engine_fraction_measured": 0.86,
                    "co_resident_gib": 2.98,
                    "margin_gib": 1.0,
                    "kv_cache_tokens_measured": 177163,
                    "kv_bytes_per_token": 65536,
                    "max_model_len": 131072,
                },
                "files": [
                    {
                        "source": "serve.service.tmpl",
                        "destination": "/etc/systemd/system/serve.service",
                        "mode": "0644",
                        "render": "values",
                        "values": {
                            "SERVICE_USER": "svc",
                            "RESTART_POLICY": "always",
                            "RESTART_SEC": "10",
                        },
                    }
                ],
            }
        },
    }


class FakeHost:
    """The command side of a host: systemctl, recorded; the file side is a real directory."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.states: dict[str, bool] = {"serve.service": True}

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


def rendered_template() -> str:
    text = TEMPLATE
    text = text.replace("__GPU_MEMORY_UTILIZATION__", "0.83")
    text = text.replace("__SERVICE_USER__", "svc")
    text = text.replace("__RESTART_POLICY__", "always")
    text = text.replace("__RESTART_SEC__", "10")
    return text


def make_overlay_with_doc(tmp_path: Path, document: dict[str, Any]) -> Path:
    return make_overlay(tmp_path / "overlay", document)


def test_host_net_serving_golden_install_writes_unit_with_expected_bytes(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    h = handler(host, fs)
    result = h.handle(request(overlay, "serve"))
    assert result.status == "success", result.stderr
    assert result.gpu_memory_utilization == "0.83"
    unit_file = fs / "etc/systemd/system/serve.service"
    content = unit_file.read_text()
    assert "Restart=always" in content
    assert "--gpu-memory-utilization 0.83" in content
    expected = rendered_template()
    assert unit_file.read_bytes() == expected.encode()


def test_host_net_serving_on_failure_restart_policy_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["files"][0]["values"]["RESTART_POLICY"] = "on-failure"
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "serving-restart-policy"
    assert result.exit_code == 2
    assert not (fs / "etc/systemd/system/serve.service").exists()


def test_host_net_serving_static_payload_with_on_failure_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    # Use a static (unrendered) payload: write the file directly with Restart=on-failure
    # and remove render/values so the handler reads the raw bytes
    doc["units"]["serve"]["files"][0].pop("render", None)
    doc["units"]["serve"]["files"][0].pop("values", None)
    overlay = make_overlay_with_doc(tmp_path, doc)
    # Write the static payload with on-failure
    static_content = (
        "[Service]\n"
        "User=svc\n"
        "ExecStart=/bin/serve --gpu-memory-utilization 0.83\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
    )
    (overlay / "serve.service.tmpl").write_text(static_content)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "serving-restart-policy"


def test_host_net_serving_static_payload_without_restart_line_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["files"][0].pop("render", None)
    doc["units"]["serve"]["files"][0].pop("values", None)
    overlay = make_overlay_with_doc(tmp_path, doc)
    static_content = (
        "[Service]\n"
        "User=svc\n"
        "ExecStart=/bin/serve --gpu-memory-utilization 0.83\n"
        "RestartSec=10\n"
    )
    (overlay / "serve.service.tmpl").write_text(static_content)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "serving-restart-policy"


def test_host_net_serving_false_allows_on_failure_restart(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["serving"] = False
    doc["units"]["serve"]["files"][0]["values"]["RESTART_POLICY"] = "on-failure"
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "success", result.stderr
    content = (fs / "etc/systemd/system/serve.service").read_text()
    assert "Restart=on-failure" in content


def test_host_net_serving_value_without_placeholder_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["files"][0]["values"]["EXTRA_KEY"] = "extra"
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "value-without-placeholder"


def test_host_net_serving_placeholder_without_value_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    del doc["units"]["serve"]["files"][0]["values"]["SERVICE_USER"]
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "placeholder-without-value"


def test_host_net_serving_gpu_budget_incomplete_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    del doc["units"]["serve"]["gpu_budget"]["margin_gib"]
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "gpu-budget-incomplete"


def test_host_net_serving_gpu_budget_no_room_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["gpu_budget"]["margin_gib"] = 40.0
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "gpu-budget-no-room"


def test_host_net_serving_gpu_budget_kv_floor_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["gpu_budget"]["max_model_len"] = 400000
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "gpu-budget-kv-floor"


def test_host_net_serving_gpu_budget_not_declared_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    del doc["units"]["serve"]["gpu_budget"]
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "gpu-budget-not-declared"


def test_host_net_serving_more_co_resident_gib_gives_smaller_fraction(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["gpu_budget"]["co_resident_gib"] = 3.5
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "success", result.stderr
    fraction = float(result.gpu_memory_utilization)
    assert fraction < 0.83


def test_host_net_serving_a_neighbour_that_squeezes_kv_below_one_context_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["gpu_budget"]["co_resident_gib"] = 5.0
    overlay = make_overlay_with_doc(tmp_path, doc)
    result = handler(FakeHost(), tmp_path / "fs").handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "gpu-budget-kv-floor"


def test_host_net_serving_larger_margin_gib_gives_smaller_fraction(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    doc["units"]["serve"]["gpu_budget"]["margin_gib"] = 2.0
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "success", result.stderr
    fraction = float(result.gpu_memory_utilization)
    assert fraction < 0.83


def test_host_net_serving_digest_mismatch_is_refused(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry["sha256"] = "0" * 64  # wrong digest
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "refused"
    assert result.reason == "digest-mismatch"


def test_host_net_serving_correct_digest_passes(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry["sha256"] = sha(rendered_template())
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    result = handler(host, fs).handle(request(overlay, "serve"))
    assert result.status == "success", result.stderr


def test_host_net_serving_uninstall_removes_the_file(
    tmp_path: Path,
) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc)
    fs = tmp_path / "fs"
    host = FakeHost()
    h = handler(host, fs)
    # First install
    result = h.handle(request(overlay, "serve"))
    assert result.status == "success", result.stderr
    unit_file = fs / "etc/systemd/system/serve.service"
    assert unit_file.exists()
    # Now uninstall
    result = h.handle(request(overlay, "serve", "uninstall"))
    assert result.status == "success", result.stderr
    assert not unit_file.exists()


def test_host_net_serving_golden_chain_through_node_dry_run(
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
    result = node.handle(request(overlay, "serve", "dry-run"))
    assert result.status == "success", result.stderr
    assert result.gpu_memory_utilization == "0.83"
