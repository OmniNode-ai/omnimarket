# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host observability overlay document takes a launchd exporter and a per target textfile directory (OMN-26662)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_observability_overlay import (
    ModelObservabilityOverlay,
)

_CONTAINER: dict[str, Any] = {
    "image": "img:1",
    "container": "c1",
    "unit": "u1.service",
    "address": "host-a.example",
    "port": 9090,
    "network": "net",
    "memory_gib": 2,
    "memory_swap_gib": 2,
}

_DOC: dict[str, Any] = {
    "ticket": "OMN-1",
    "observer_host": "host-a",
    "config_root": "/etc/obs",
    "docker_binary": "/usr/bin/docker",
    "restart_policy": "always",
    "restart_seconds": 5,
    "stop_timeout_seconds": 10,
    "probe_wait_seconds": 30,
    "max_reading_age_seconds": 120,
    "targets": {
        "host-a": {"address": "host-a.example", "exporter": "install", "port": 9100},
    },
    "exporter": {
        "image": "exp:1",
        "container": "exp",
        "unit": "exp.service",
        "rootfs_mount": "/",
        "textfile_directory": "/var/lib/textfile",
        "network": "host",
        "memory_gib": 1,
        "memory_swap_gib": 1,
    },
    "prometheus": {
        **_CONTAINER,
        "retention_time": "15d",
        "retention_size": "10GB",
        "scrape_interval_seconds": 15,
        "job_name": "hosts",
        "data_volume": "prom-data",
    },
    "grafana": {
        **_CONTAINER,
        "port": 3000,
        "data_volume": "graf-data",
        "dashboard_title": "Lab",
        "refresh_seconds": 30,
        "reading_window_minutes": 15,
    },
    "alerts": {
        "host_down_minutes": 2,
        "disk_percent": 90,
        "inode_percent": 90,
        "load_minutes": 10,
        "failed_service_minutes": 5,
        "reading_stale_minutes": 10,
    },
}

_LAUNCHD: dict[str, Any] = {
    "label": "test.example.node-exporter",
    "binary": "/opt/example/bin/node_exporter",
    "plist_directory": "/Library/LaunchDaemons",
    "log_directory": "/var/log/example",
    "textfile_directory": "/opt/example/textfile",
    "domain": "system",
    "enable_collectors": ["textfile"],
}


def _doc() -> dict[str, Any]:
    return copy.deepcopy(_DOC)


def _with_mac(doc: dict[str, Any]) -> dict[str, Any]:
    doc["targets"]["host-m"] = {
        "address": "host-m.example",
        "exporter": "launchd",
        "port": 9100,
    }
    doc["launchd_exporter"] = copy.deepcopy(_LAUNCHD)
    return doc


def test_launchd_target_with_its_section_is_accepted() -> None:
    overlay = ModelObservabilityOverlay.model_validate(_with_mac(_doc()))
    assert overlay.targets["host-m"].exporter == "launchd"
    assert overlay.launchd_exporter is not None
    assert overlay.launchd_exporter.domain == "system"


def test_launchd_target_without_its_section_is_refused() -> None:
    doc = _with_mac(_doc())
    del doc["launchd_exporter"]
    with pytest.raises(ValidationError, match="launchd_exporter"):
        ModelObservabilityOverlay.model_validate(doc)


def test_per_target_textfile_directory_is_optional_and_kept() -> None:
    overlay = ModelObservabilityOverlay.model_validate(_doc())
    assert overlay.targets["host-a"].textfile_directory is None
    doc = _doc()
    doc["targets"]["host-a"]["textfile_directory"] = "/srv/own/textfile"
    overlay = ModelObservabilityOverlay.model_validate(doc)
    assert overlay.targets["host-a"].textfile_directory == "/srv/own/textfile"


def test_relative_per_target_textfile_directory_is_refused() -> None:
    doc = _doc()
    doc["targets"]["host-a"]["textfile_directory"] = "relative/dir"
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)


def test_unknown_exporter_value_is_still_refused() -> None:
    doc = _doc()
    doc["targets"]["host-a"]["exporter"] = "systemd"
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)


def test_launchd_section_refuses_a_relative_binary() -> None:
    doc = _with_mac(_doc())
    doc["launchd_exporter"]["binary"] = "node_exporter"
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)
