# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the host hygiene orchestrator overlay model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneBackend,
    ModelHostHygieneOrchestratorOverlay,
)


def _valid_overlay() -> dict:
    return {
        "job_id": "host_hygiene",
        "run_interval_seconds": 300,
        "grace_seconds": 60,
        "backend": "in_memory",
        "max_reading_age_seconds": 3600,
        "max_parallel_units": 4,
        "lab_work": {
            "repo": "OmniNode-ai/omnimarket",
            "commit_sha": "a" * 40,
            "lane": "hygiene-lane",
            "unit_id_prefix": "hh",
            "bus_lane": "bus-1",
        },
        "cleanups": [
            {
                "cleanup_id": "uv_cache",
                "min_used_fraction": 0.8,
                "min_interval_seconds": 3600,
                "when_unread": "dispatch",
                "argv": ["rm", "-rf", "{registry_root}/.cache/uv"],
                "timeout_seconds": 600,
                "kind": "other",
            }
        ],
        "hosts": [
            {
                "name": "host-a",
                "registry_root": "/home/omni",
                "pressure_volume": "/",
                "cleanup_ids": ["uv_cache"],
            }
        ],
    }


def test_valid_overlay() -> None:
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(_valid_overlay())
    assert overlay.backend is EnumHostHygieneBackend.IN_MEMORY
    assert overlay.lab_work is not None
    assert overlay.lab_work.repo == "OmniNode-ai/omnimarket"


def test_unknown_host_cleanup_id() -> None:
    doc = _valid_overlay()
    doc["hosts"][0]["cleanup_ids"] = ["unknown_cleanup"]
    with pytest.raises(ValidationError, match="unknown cleanup id"):
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)


def test_duplicate_host() -> None:
    doc = _valid_overlay()
    doc["hosts"].append(
        {
            "name": "host-a",
            "registry_root": "/home/omni",
            "pressure_volume": "/",
            "cleanup_ids": ["uv_cache"],
        }
    )
    with pytest.raises(ValidationError, match="duplicate host"):
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)


def test_lab_work_backend_with_both_kafka_and_bus() -> None:
    doc = _valid_overlay()
    doc["backend"] = "lab_work"
    doc["lab_work"]["kafka_bootstrap"] = "broker:9092"
    doc["lab_work"]["bus_lane"] = "bus-1"
    with pytest.raises(ValidationError, match="both"):
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)


def test_lab_work_backend_with_neither_kafka_nor_bus() -> None:
    doc = _valid_overlay()
    doc["backend"] = "lab_work"
    doc["lab_work"]["kafka_bootstrap"] = None
    doc["lab_work"]["bus_lane"] = None
    with pytest.raises(ValidationError, match="neither"):
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)


def test_bad_commit_sha() -> None:
    doc = _valid_overlay()
    doc["lab_work"]["commit_sha"] = "not-a-sha"
    with pytest.raises(ValidationError, match="commit_sha"):
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)
