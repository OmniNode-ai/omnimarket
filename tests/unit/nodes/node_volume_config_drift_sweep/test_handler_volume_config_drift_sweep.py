# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_volume_config_drift_sweep (OMN-12958)."""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_volume_config_drift_sweep.handlers.handler_volume_config_drift_sweep import (
    STATUS_DEPLOYED_ABSENT,
    STATUS_DRIFTED,
    STATUS_IN_SYNC,
    STATUS_SOURCE_ABSENT,
    NodeVolumeConfigDriftSweep,
    VolumeConfigDriftSweepRequest,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_paths_in_sync(tmp_path: Path) -> None:
    deployed = _write(tmp_path / "deployed.yaml", "backends: []\n")
    source = _write(tmp_path / "source.yaml", "backends: []\n")
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(
            deployed_path=str(deployed), source_path=str(source)
        )
    )
    assert result.status == "clean"
    assert result.drift_count == 0
    assert result.findings[0].status == STATUS_IN_SYNC


def test_paths_drifted(tmp_path: Path) -> None:
    deployed = _write(tmp_path / "deployed.yaml", "backends: [stale]\n")
    source = _write(tmp_path / "source.yaml", "backends: []\n")
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(
            deployed_path=str(deployed),
            source_path=str(source),
            lane="stability",
        )
    )
    assert result.status == "drift_found"
    assert result.drift_count == 1
    finding = result.findings[0]
    assert finding.status == STATUS_DRIFTED
    assert finding.is_drift
    assert "stability" in finding.recommended_ticket_title()
    assert finding.deployed_sha256 != finding.source_sha256
    assert "re-seed" in finding.recommended_ticket_body()


def test_paths_deployed_absent(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.yaml", "backends: []\n")
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(
            deployed_path=str(tmp_path / "missing.yaml"),
            source_path=str(source),
        )
    )
    # Absent deployed copy is not drift — distinct status, no ticket.
    assert result.status == "clean"
    assert result.drift_count == 0
    assert result.findings[0].status == STATUS_DEPLOYED_ABSENT


def test_paths_source_absent(tmp_path: Path) -> None:
    deployed = _write(tmp_path / "deployed.yaml", "backends: []\n")
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(
            deployed_path=str(deployed),
            source_path=str(tmp_path / "missing.yaml"),
        )
    )
    assert result.status == "clean"
    assert result.findings[0].status == STATUS_SOURCE_ABSENT


def test_sidecar_drift(tmp_path: Path) -> None:
    sidecar = tmp_path / ".config_provenance.json"
    sidecar.write_text(
        json.dumps(
            {
                "config_name": "bifrost_delegation",
                "deployed_path": "/app/data/delegation/bifrost_delegation.yaml",
                "deployed_sha256": "aaa",
                "source_path": "/app/src/omnimarket/configs/bifrost_delegation.yaml",
                "source_sha256": "bbb",
            }
        ),
        encoding="utf-8",
    )
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(sidecar_path=str(sidecar), lane="prod")
    )
    assert result.status == "drift_found"
    finding = result.findings[0]
    assert finding.status == STATUS_DRIFTED
    assert finding.lane == "prod"
    assert finding.deployed_sha256 == "aaa"


def test_sidecar_in_sync(tmp_path: Path) -> None:
    sidecar = tmp_path / ".config_provenance.json"
    sidecar.write_text(
        json.dumps(
            {
                "config_name": "bifrost_delegation",
                "deployed_path": "/app/data/delegation/bifrost_delegation.yaml",
                "deployed_sha256": "same",
                "source_path": "/app/src/omnimarket/configs/bifrost_delegation.yaml",
                "source_sha256": "same",
            }
        ),
        encoding="utf-8",
    )
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(sidecar_path=str(sidecar))
    )
    assert result.status == "clean"
    assert result.findings[0].status == STATUS_IN_SYNC


def test_default_source_resolves_to_packaged_config() -> None:
    # No deployed copy on disk locally — proves the packaged source path resolves
    # from the omnimarket package without raising.
    result = NodeVolumeConfigDriftSweep().handle(
        VolumeConfigDriftSweepRequest(deployed_path="/nonexistent/deployed.yaml")
    )
    finding = result.findings[0]
    assert finding.status == STATUS_DEPLOYED_ABSENT
    assert finding.source_path.endswith("configs/bifrost_delegation.yaml")
    assert finding.source_sha256 is not None
