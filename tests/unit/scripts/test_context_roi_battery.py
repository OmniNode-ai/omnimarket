# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/run_context_roi_battery.py (OMN-12949).

The battery orchestrator drives a full (task x arm x trial) context-ROI
experiment: it builds the E1-E6 difficulty ladder, constructs arms from the
canonical factor matrix, invokes the runner EFFECT, translates the runner rows
into scorer rows, feeds the COMPUTE scorer, and writes the result artifact plus
a correlation-ID manifest.

These tests are pure: a FakeBus stand-in (publisher + consumer) drives the
runner without any Kafka traffic, and dry-run mode is asserted to publish
nothing at all.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiRunRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_context_roi_battery.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_context_roi_battery", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_context_roi_battery"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def battery() -> Any:
    return _load_script_module()


# ---------------------------------------------------------------------------
# Fake bus: a deterministic terminal-event generator so the runner can run a
# full matrix in-process with zero Kafka traffic. The "model" succeeds on
# attempt 1 for ON arms and needs two attempts for the off baseline, so the
# scorer produces a non-trivial first-pass-rate delta.
# ---------------------------------------------------------------------------


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._pending: dict[str, dict[str, Any]] = {}

    def publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))
        if "node-generation-requested" in topic:
            data = json.loads(payload.decode("utf-8"))
            corr = data["correlation_id"]
            has_context = bool(data.get("context_pack"))
            self._pending[corr] = {
                "attempt_count": 1 if has_context else 2,
                "first_pass_success": has_context,
                "contract_passed": True,
                "prompt_tokens": 1000 if has_context else 400,
                "completion_tokens": 300,
                "cost_inference_usd": 0.0012,
                "model_id": "qwen3-coder-30b",
                "provider": "local",
                "endpoint_class": "local-coder",
            }

    def consume(
        self, topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        return self._pending.pop(correlation_id, None)


# ---------------------------------------------------------------------------
# Battery construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_e1_e6_difficulty_ladder_is_the_default_task_battery(battery: Any) -> None:
    tasks = battery.build_difficulty_ladder()
    task_ids = [t.task_id for t in tasks]
    assert task_ids == ["E1", "E2", "E3", "E4", "E5", "E6"]
    # Every task on the ladder requires the golden chain (the spine factor) so
    # ON arms are comparable and escalation-relevant cells exist.
    for task in tasks:
        assert "golden_chain" in task.required_factors


@pytest.mark.unit
def test_arms_come_from_the_canonical_seven_arm_matrix(battery: Any) -> None:
    arms = battery.build_arms()
    labels = [a.label for a in arms]
    assert labels == [member.value for member in EnumArmLabel]
    # off baseline present, negative control present.
    assert "off" in labels
    assert "full_guidance_negative_control" in labels


@pytest.mark.unit
def test_build_run_request_spans_all_cells(battery: Any) -> None:
    request = battery.build_run_request(trials_per_cell=2)
    assert isinstance(request, ModelContextRoiRunRequest)
    assert len(request.tasks) == 6
    assert len(request.arms) == 7
    assert request.trials_per_cell == 2


# ---------------------------------------------------------------------------
# Dry-run mode: no bus traffic, wiring validated
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_publishes_nothing(battery: Any, tmp_path: Path) -> None:
    fake_bus = FakeBus()
    report = battery.run_battery(
        trials_per_cell=1,
        dry_run=True,
        output_dir=tmp_path,
        bus_factory=lambda: fake_bus,
    )
    assert fake_bus.published == []
    assert report.dry_run is True
    # The dry run still validates the full wiring: expected cell count is
    # tasks x arms x trials.
    assert report.expected_cell_count == 6 * 7 * 1


@pytest.mark.unit
def test_dry_run_writes_no_result_artifact(battery: Any, tmp_path: Path) -> None:
    fake_bus = FakeBus()
    battery.run_battery(
        trials_per_cell=1,
        dry_run=True,
        output_dir=tmp_path,
        bus_factory=lambda: fake_bus,
    )
    # Dry run must not write the heavy result artifact (wiring only).
    assert not (tmp_path / "context_roi_battery_result.json").exists()


# ---------------------------------------------------------------------------
# Live (FakeBus) battery: rows collected, scorer fed, artifacts written
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_full_battery_collects_rows_and_scores(battery: Any, tmp_path: Path) -> None:
    fake_bus = FakeBus()
    report = battery.run_battery(
        trials_per_cell=2,
        dry_run=False,
        output_dir=tmp_path,
        bus_factory=lambda: fake_bus,
    )
    assert report.dry_run is False
    # One generation command per (task x arm x trial), minus the off arm which
    # injects no context but still runs generation. All 7 arms run generation.
    assert report.run_result.total_trials == 6 * 7 * 2
    # The scorer ran in fixture mode over the collected rows.
    assert report.score_result.status == "ok"
    assert report.score_result.arm_summary


@pytest.mark.unit
def test_result_artifact_and_manifest_written(battery: Any, tmp_path: Path) -> None:
    fake_bus = FakeBus()
    battery.run_battery(
        trials_per_cell=1,
        dry_run=False,
        output_dir=tmp_path,
        bus_factory=lambda: fake_bus,
    )
    result_path = tmp_path / "context_roi_battery_result.json"
    manifest_path = tmp_path / "context_roi_battery_manifest.json"
    assert result_path.exists()
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Manifest tags every cell with experiment + arm + cell identity.
    assert manifest["experiment_id"]
    assert manifest["cells"]
    first = manifest["cells"][0]
    assert "correlation_id" in first
    assert "arm_label" in first
    assert "task_id" in first
    assert "trial_index" in first
    # Context Authority Rule: experiment arms are labelled as experiment
    # assignment, not as a resolved selection.
    assert first["selection_reason"] == "experiment_assignment"
    assert first["experiment_cohort"] == manifest["experiment_id"]


@pytest.mark.unit
def test_correlation_ids_unique_per_cell(battery: Any, tmp_path: Path) -> None:
    fake_bus = FakeBus()
    battery.run_battery(
        trials_per_cell=2,
        dry_run=False,
        output_dir=tmp_path,
        bus_factory=lambda: fake_bus,
    )
    manifest = json.loads(
        (tmp_path / "context_roi_battery_manifest.json").read_text(encoding="utf-8")
    )
    corr_ids = [c["correlation_id"] for c in manifest["cells"]]
    assert len(corr_ids) == len(set(corr_ids))
    assert len(corr_ids) == 6 * 7 * 2


# ---------------------------------------------------------------------------
# Architecture conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_script_imports_no_kafka_runner_or_generation_consumer(battery: Any) -> None:
    """The battery drives the runner EFFECT; it must not import the in-process
    generation consumer or a kafka runner."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    joined = "\n".join(imports)
    assert "GenerationConsumer" not in joined
    assert "kafka_runner" not in joined
    assert "node_generation_consumer" not in joined


@pytest.mark.unit
def test_models_resolve_when_loaded_by_arbitrary_module_name(tmp_path: Path) -> None:
    """The script is loaded by path (importlib). Pydantic forward references in
    the manifest/report models must resolve regardless of the module name the
    loader assigns - guards the model_rebuild() wiring."""
    spec = importlib.util.spec_from_file_location("arbitrary_battery_name", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["arbitrary_battery_name"] = module
    spec.loader.exec_module(module)

    fake_bus = FakeBus()
    report = module.run_battery(
        trials_per_cell=1,
        dry_run=False,
        output_dir=tmp_path,
        bus_factory=lambda: fake_bus,
    )
    assert report.manifest is not None
    assert report.manifest.cells


@pytest.mark.unit
def test_script_has_no_hardcoded_artifact_paths(battery: Any) -> None:
    """Content resolution must not hardcode artifact file paths; the script body
    must contain no absolute machine paths."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/Users/" not in source  # test-literal-ok: asserting absence of abs path
    assert "/Volumes/" not in source  # test-literal-ok: asserting absence of abs path
