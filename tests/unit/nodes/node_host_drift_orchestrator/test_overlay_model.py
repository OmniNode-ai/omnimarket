# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay model of the host drift orchestrator (OMN-24197): both shipped profiles load, and a bad overlay is refused."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_host_drift_orchestrator import (
    EnumHostDriftBackend,
    EnumHostDriftHostVerdict,
    ModelHostDriftHostVerdict,
    ModelHostDriftOrchestratorOverlay,
    failed_hosts,
    run_outcome,
)

pytestmark = pytest.mark.unit

OVERLAYS = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_host_drift_orchestrator"
    / "overlays"
)


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((OVERLAYS / name).read_text())
    return loaded


def test_both_shipped_profiles_load_with_their_backend() -> None:
    assert (
        ModelHostDriftOrchestratorOverlay.model_validate(
            _load("in_memory.yaml")
        ).backend
        is EnumHostDriftBackend.IN_MEMORY
    )
    assert (
        ModelHostDriftOrchestratorOverlay.model_validate(
            _load("lab_work.example.yaml")
        ).backend
        is EnumHostDriftBackend.LAB_WORK
    )


def test_a_duplicate_host_is_refused() -> None:
    data = _load("in_memory.yaml")
    data["hosts"] = [data["hosts"][0], data["hosts"][0]]
    with pytest.raises(ValidationError, match="duplicate host"):
        ModelHostDriftOrchestratorOverlay.model_validate(data)


def test_a_lab_work_block_with_two_addresses_is_refused() -> None:
    data = _load("in_memory.yaml")
    data["lab_work"]["kafka_bootstrap"] = "example-broker:9092"
    with pytest.raises(ValidationError, match="both"):
        ModelHostDriftOrchestratorOverlay.model_validate(data)


def test_an_unknown_overlay_key_is_refused() -> None:
    data = _load("in_memory.yaml")
    data["mode"] = "act"
    with pytest.raises(ValidationError):
        ModelHostDriftOrchestratorOverlay.model_validate(data)


def _verdict(host: str, verdict: EnumHostDriftHostVerdict) -> ModelHostDriftHostVerdict:
    return ModelHostDriftHostVerdict(host=host, verdict=verdict, work_unit_id="u")


def test_run_outcome_is_failed_without_verdicts_and_completed_only_when_every_host_answered() -> (
    None
):
    answered = (
        _verdict("a", EnumHostDriftHostVerdict.IN_SYNC),
        _verdict("b", EnumHostDriftHostVerdict.DRIFTED),
    )
    mixed = (*answered, _verdict("c", EnumHostDriftHostVerdict.CHECK_FAILED))
    assert run_outcome(()).value == "failed"
    assert run_outcome(answered).value == "completed"
    assert run_outcome(mixed).value == "failed"
    assert failed_hosts(mixed) == ("c",)
