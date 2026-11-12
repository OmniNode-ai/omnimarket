# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_net_units: units_file overlay selection, ModelNetUnitsOverlay validation, and the golden/error chains.

Every payload, host name and number below is fixture input written into a temporary overlay; the node holds none.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_installer_net_units.handlers.handler_net_units import (
    HandlerNetUnits,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsOverlay,
    ModelNetUnitsRequest,
)
from omnimarket.nodes.node_host_installer_net_units.node import (
    NodeHostInstallerNetUnits,
)

TEMPLATE = (
    "[Service]\n"
    "User=__SERVICE_USER__\n"
    "ExecStart=/bin/serve --gpu-memory-utilization __GPU_MEMORY_UTILIZATION__\n"
    "Restart=__RESTART_POLICY__\n"
    "RestartSec=__RESTART_SEC__\n"
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_overlay(
    path: Path, document: dict[str, Any], name: str = "units.yaml"
) -> Path:
    path.mkdir(parents=True)
    (path / "serve.service.tmpl").write_text(TEMPLATE)
    (path / name).write_text(yaml.safe_dump(document))
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


def media_document() -> dict[str, Any]:
    return {
        "schema": "host_net_units.v1",
        "units": {
            "media": {
                "kind": "sshd-dropin",
                "platform": "linux",
                "hosts": ["pool-a"],
                "files": [
                    {
                        "source": "a.conf",
                        "destination": "/etc/ssh/sshd_config.d/20-a.conf",
                        "mode": "0644",
                        "sha256": sha("A\n"),
                    },
                    {
                        "source": "b.conf",
                        "destination": "/etc/ssh/sshd_config.d/21-b.conf",
                        "mode": "0644",
                        "sha256": sha("B\n"),
                    },
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
    overlay: Path,
    unit: str,
    mode: str = "install",
    host_name: str = "pool-a",
    units_file: str = "",
) -> ModelNetUnitsRequest:
    return ModelNetUnitsRequest(
        unit=unit,
        mode=mode,
        host_name=host_name,
        overlay_root=str(overlay),
        units_file=units_file,
    )


def rendered_template() -> str:
    text = TEMPLATE
    text = text.replace("__GPU_MEMORY_UTILIZATION__", "0.83")
    text = text.replace("__SERVICE_USER__", "svc")
    text = text.replace("__RESTART_POLICY__", "always")
    text = text.replace("__RESTART_SEC__", "10")
    return text


def make_overlay_with_doc(
    tmp_path: Path, document: dict[str, Any], name: str = "units.yaml"
) -> Path:
    return make_overlay(tmp_path / "overlay", document, name)


def test_units_file_overlay_named_is_read(tmp_path: Path) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    result = handler(host, tmp_path / "fs").handle(
        request(overlay, "serve", "dry-run", units_file="overlay.yaml")
    )
    assert result.status == "success", result.stderr


def test_units_file_default_missing_is_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    result = handler(host, tmp_path / "fs").handle(request(overlay, "serve", "dry-run"))
    assert result.status == "refused"
    assert result.reason == "overlay-unreadable"


def test_units_file_path_separator_is_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc, "units.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    result = handler(host, fs).handle(
        request(overlay, "serve", "dry-run", units_file="../units.yaml")
    )
    assert result.status == "refused"
    assert result.reason == "units-file-not-allowed"
    assert not any(fs.rglob("*"))


def test_units_file_dotdot_is_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc, "units.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    result = handler(host, fs).handle(
        request(overlay, "serve", "dry-run", units_file="..")
    )
    assert result.status == "refused"
    assert result.reason == "units-file-not-allowed"
    assert not any(fs.rglob("*"))


def test_model_accepts_fixture_document() -> None:
    ModelNetUnitsOverlay.model_validate(fixture_document())


def test_model_refuses_wrong_schema() -> None:
    doc = fixture_document()
    doc["schema"] = "host_net_units.v0"
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(doc)


def test_model_refuses_unit_without_kind() -> None:
    doc = fixture_document()
    del doc["units"]["serve"]["kind"]
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(doc)


def test_model_refuses_serving_unit_with_empty_files() -> None:
    doc = fixture_document()
    doc["units"]["serve"]["files"] = []
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(doc)


def test_model_accepts_media_units_shape() -> None:
    ModelNetUnitsOverlay.model_validate(media_document())


def test_handler_overlay_invalid_is_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    del doc["units"]["serve"]["kind"]
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    result = handler(host, tmp_path / "fs").handle(
        request(overlay, "serve", "dry-run", units_file="overlay.yaml")
    )
    assert result.status == "refused"
    assert result.reason == "overlay-invalid"
    assert "units.serve.kind" in result.stderr


def test_golden_chain_overlay_file_install(tmp_path: Path) -> None:
    doc = fixture_document()
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    node = NodeHostInstallerNetUnits()
    node.handler = HandlerNetUnits(
        run=host, system=lambda: "Linux", sudo=[], fs_root=fs
    )
    result = node.handle(
        request(overlay, "serve", "install", units_file="overlay.yaml")
    )
    assert result.status == "success", result.stderr
    assert (
        fs / "etc/systemd/system/serve.service"
    ).read_bytes() == rendered_template().encode()


def test_error_chain_overlay_invalid_writes_nothing(tmp_path: Path) -> None:
    doc = fixture_document()
    del doc["units"]["serve"]["kind"]
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    node = NodeHostInstallerNetUnits()
    node.handler = HandlerNetUnits(
        run=host, system=lambda: "Linux", sudo=[], fs_root=fs
    )
    result = node.handle(
        request(overlay, "serve", "install", units_file="overlay.yaml")
    )
    assert result.status == "refused"
    assert result.reason == "overlay-invalid"
    assert not any(fs.rglob("*"))


def test_inline_content_install_writes_rendered_bytes(tmp_path: Path) -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry.pop("source")
    entry["content"] = TEMPLATE
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    result = handler(host, fs).handle(
        request(overlay, "serve", "install", units_file="overlay.yaml")
    )
    assert result.status == "success", result.stderr
    assert (
        fs / "etc/systemd/system/serve.service"
    ).read_bytes() == rendered_template().encode()


def test_inline_content_and_source_is_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry["content"] = TEMPLATE
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    result = handler(host, fs).handle(
        request(overlay, "serve", "install", units_file="overlay.yaml")
    )
    assert result.status == "refused"
    assert result.reason == "overlay-invalid"
    assert not any(fs.rglob("*"))


def test_model_refuses_file_with_neither_source_nor_content() -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry.pop("source")
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(doc)


def test_inline_content_digest_pin_is_checked(tmp_path: Path) -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry.pop("source")
    entry["content"] = TEMPLATE
    entry["sha256"] = "0" * 64
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    result = handler(host, fs).handle(
        request(overlay, "serve", "install", units_file="overlay.yaml")
    )
    assert result.status == "refused"
    assert result.reason == "digest-mismatch"
    assert not any(fs.rglob("*"))


def test_inline_content_serving_restart_refused(tmp_path: Path) -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry.pop("source")
    entry["content"] = TEMPLATE
    entry["values"]["RESTART_POLICY"] = "on-failure"
    overlay = make_overlay_with_doc(tmp_path, doc, "overlay.yaml")
    host = FakeHost()
    fs = tmp_path / "fs"
    result = handler(host, fs).handle(
        request(overlay, "serve", "install", units_file="overlay.yaml")
    )
    assert result.status == "refused"
    assert result.reason == "serving-restart-policy"
    assert not any(fs.rglob("*"))


def test_model_accepts_inline_content_without_source() -> None:
    doc = fixture_document()
    entry = doc["units"]["serve"]["files"][0]
    entry.pop("source")
    entry["content"] = TEMPLATE
    overlay = ModelNetUnitsOverlay.model_validate(doc)
    declared = overlay.units["serve"].files[0]
    assert declared.content == TEMPLATE
    assert declared.source == ""


def test_model_refuses_file_with_both_source_and_content() -> None:
    doc = fixture_document()
    doc["units"]["serve"]["files"][0]["content"] = TEMPLATE
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(doc)
