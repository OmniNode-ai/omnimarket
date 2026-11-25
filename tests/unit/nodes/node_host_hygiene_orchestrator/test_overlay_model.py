# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the host hygiene orchestrator overlay model."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
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


def test_event_bus_key_is_accepted() -> None:
    doc = _valid_overlay()
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert overlay.event_bus is not None
    assert overlay.event_bus.type == "kafka"
    assert overlay.event_bus.lane == "dev"


def test_overlay_without_event_bus_leaves_it_none() -> None:
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(_valid_overlay())
    assert overlay.event_bus is None


def test_lab_work_event_bus_lane_matching_bus_lane_is_accepted() -> None:
    doc = _valid_overlay()
    doc["backend"] = "lab_work"
    doc["event_bus"] = {"type": "kafka", "lane": "bus-1"}
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert overlay.event_bus is not None
    assert overlay.event_bus.lane == "bus-1"


def test_lab_work_event_bus_lane_differing_from_bus_lane_is_refused() -> None:
    doc = _valid_overlay()
    doc["backend"] = "lab_work"
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    with pytest.raises(
        ValidationError, match="differs from lab_work bus_lane"
    ) as caught:
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    message = str(caught.value)
    assert "dev" in message
    assert "bus-1" in message


def test_in_memory_backend_does_not_tie_the_lanes() -> None:
    doc = _valid_overlay()
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert overlay.backend is EnumHostHygieneBackend.IN_MEMORY
    assert overlay.event_bus is not None
    assert overlay.event_bus.lane == "dev"


def test_lab_work_kafka_bootstrap_has_no_lane_to_tie() -> None:
    doc = _valid_overlay()
    doc["backend"] = "lab_work"
    del doc["lab_work"]["bus_lane"]
    doc["lab_work"]["kafka_bootstrap"] = "broker:9092"
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert overlay.lab_work.bus_lane is None
    assert overlay.lab_work.kafka_bootstrap == "broker:9092"


def test_lab_work_with_inmemory_event_bus_is_accepted() -> None:
    doc = _valid_overlay()
    doc["backend"] = "lab_work"
    doc["event_bus"] = {"type": "inmemory"}
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert overlay.backend is EnumHostHygieneBackend.LAB_WORK
    assert overlay.event_bus is not None
    assert overlay.event_bus.type == "inmemory"


def test_event_bus_unknown_key_refused() -> None:
    doc = _valid_overlay()
    doc["event_bus"] = {"type": "kafka", "lane": "dev", "nonsense": 1}
    with pytest.raises(ValidationError) as caught:
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert [e["loc"] for e in caught.value.errors()] == [("event_bus", "nonsense")]


def test_shared_file_is_read_alike_by_the_model_and_the_bus_reader(
    tmp_path: Path,
) -> None:
    """Seam: the overlay file the bus reader needs (event_bus beside every setting) is a valid orchestrator overlay.

    The deployed overlay holds the schedule, the cleanups and the ``event_bus`` block in one file. The bus overlay reader
    (behind ``onex run-node``) takes the block; this model takes the file. The test reads one document through both and
    fails when either side changes shape; it fails on the model without the field with an extra-forbidden refusal.
    """
    node_dir = tmp_path / "node_host_hygiene_orchestrator"
    node_dir.mkdir()
    document = _valid_overlay()
    document["event_bus"] = {"type": "kafka", "lane": "dev"}
    path = node_dir / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    overlay = load_structured_overlay(
        "node_host_hygiene_orchestrator",
        ModelHostHygieneOrchestratorOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    bus = read_node_event_bus(path)
    assert bus is not None
    assert overlay.event_bus == bus
