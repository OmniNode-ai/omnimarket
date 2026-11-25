# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host observability overlay document is a shared model an overlay can be pinned to (OMN-24562)."""

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


def _doc() -> dict[str, Any]:
    return copy.deepcopy(_DOC)


def test_valid_document_is_accepted() -> None:
    overlay = ModelObservabilityOverlay.model_validate(_doc())
    assert overlay.observer_host == "host-a"
    assert overlay.process_exporter is None
    assert overlay.grafana.environment == {}


def test_extra_key_is_refused() -> None:
    doc = _doc()
    doc["surprise"] = 1
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)


def test_model_is_frozen() -> None:
    overlay = ModelObservabilityOverlay.model_validate(_doc())
    with pytest.raises(ValidationError):
        overlay.ticket = "OMN-2"  # type: ignore[misc]


def test_observer_must_be_a_target() -> None:
    doc = _doc()
    doc["observer_host"] = "host-z"
    with pytest.raises(ValidationError, match="not among the declared targets"):
        ModelObservabilityOverlay.model_validate(doc)


def test_processes_need_the_process_exporter() -> None:
    doc = _doc()
    doc["targets"]["host-a"]["processes"] = True
    with pytest.raises(ValidationError, match="process_exporter"):
        ModelObservabilityOverlay.model_validate(doc)


def test_swap_below_memory_is_refused() -> None:
    doc = _doc()
    doc["prometheus"]["memory_swap_gib"] = 1
    with pytest.raises(ValidationError, match="cannot be below"):
        ModelObservabilityOverlay.model_validate(doc)


def test_relative_path_and_bad_port_are_refused() -> None:
    doc = _doc()
    doc["config_root"] = "etc/obs"
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)
    doc = _doc()
    doc["targets"]["host-a"]["port"] = 70000
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)


def test_alert_percent_above_100_is_refused() -> None:
    doc = _doc()
    doc["alerts"]["disk_percent"] = 101
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(doc)
