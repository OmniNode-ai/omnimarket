# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the experiment result record compute node."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResult,
    experiment_arm_result_id_for,
)
from omnimarket.nodes.node_experiment_result_record_compute.handlers.handler_experiment_result_record import (
    HandlerExperimentResultRecord,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_record_status import (
    EnumExperimentResultRecordStatus,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_refusal import (
    EnumExperimentResultRefusal,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_policy import (
    ModelExperimentResultPolicy,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_record_request import (
    ModelExperimentResultRecordRequest,
)
from omnimarket.nodes.node_experiment_result_record_compute.overlay import load_policy

pytestmark = pytest.mark.unit

_TENANT = uuid.uuid5(uuid.NAMESPACE_URL, "test-tenant")
_Z = 1.959963984540054


def _wilson(correct: int, n: int, z: float) -> tuple[float, float]:
    """Independent Wilson interval helper for the tests."""
    if not 0 <= correct <= n:
        raise ValueError("Wilson counts must satisfy 0 <= correct <= n")
    if n == 0:
        return 0.0, 1.0
    p = correct / n
    denominator = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _make_result(
    *,
    n: int = 60,
    skip_wilson: bool = False,
    correct: int = 48,
    wilson_low: float | None = None,
    wilson_high: float | None = None,
    baseline_accuracy: float = 0.7,
    latency_p50_ms: float = 200.0,
    latency_p90_ms: float = 900.0,
    gpu_seconds: float = 12.5,
    engine: str = "local",
    corpus_sha256: str = "a" * 64,
    truth_sha256: str = "b" * 64,
) -> ModelExperimentArmResult:
    if skip_wilson:
        wilson_low = 0.0 if wilson_low is None else wilson_low
        wilson_high = 1.0 if wilson_high is None else wilson_high
    elif wilson_low is None or wilson_high is None:
        lo, hi = _wilson(correct, n, _Z)
        wilson_low = lo if wilson_low is None else wilson_low
        wilson_high = hi if wilson_high is None else wilson_high
    return ModelExperimentArmResult(
        tenant_id=_TENANT,
        experiment_id="exp-test",
        arm="arm-a",
        model_id="model-x",
        engine=engine,
        n=n,
        correct=correct,
        wilson_low=wilson_low,
        wilson_high=wilson_high,
        baseline_accuracy=baseline_accuracy,
        latency_p50_ms=latency_p50_ms,
        latency_p90_ms=latency_p90_ms,
        gpu_seconds=gpu_seconds,
        corpus_sha256=corpus_sha256,
        truth_sha256=truth_sha256,
        measured_at=datetime(2026, 11, 13, tzinfo=UTC),
    )


def _policy(
    *,
    interval_tolerance: float = 0.001,
    confidence_z: float = _Z,
    refused_engines: tuple[str, ...] = ("claude", "codex"),
) -> ModelExperimentResultPolicy:
    return ModelExperimentResultPolicy(
        interval_tolerance=interval_tolerance,
        confidence_z=confidence_z,
        refused_engines=refused_engines,
    )


def _request(result: ModelExperimentArmResult) -> ModelExperimentResultRecordRequest:
    return ModelExperimentResultRecordRequest(result=result)


# --- golden ---------------------------------------------------------------


def test_golden_valid_result_completed() -> None:
    result = _make_result()
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.COMPLETED
    assert out.refusals == ()
    assert out.accuracy == pytest.approx(0.8)
    assert out.result_id == experiment_arm_result_id_for(result)


# --- error chain, one test per refusal code --------------------------------


def test_correct_exceeds_n() -> None:
    result = _make_result(n=60, correct=61, skip_wilson=True)
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.FAILED
    assert len(out.refusals) == 1
    assert out.refusals[0].refusal is EnumExperimentResultRefusal.CORRECT_EXCEEDS_N
    assert "61" in out.refusals[0].detail
    assert "60" in out.refusals[0].detail


def test_interval_mismatch() -> None:
    lo, hi = _wilson(48, 60, _Z)
    result = _make_result(wilson_low=lo - 0.05, wilson_high=hi)
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.FAILED
    assert len(out.refusals) == 1
    assert out.refusals[0].refusal is EnumExperimentResultRefusal.INTERVAL_MISMATCH


def test_latency_order() -> None:
    result = _make_result(latency_p50_ms=900.0, latency_p90_ms=200.0)
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.FAILED
    assert len(out.refusals) == 1
    assert out.refusals[0].refusal is EnumExperimentResultRefusal.LATENCY_ORDER


def test_engine_not_allowed() -> None:
    result = _make_result(engine="Claude")
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.FAILED
    assert len(out.refusals) == 1
    assert out.refusals[0].refusal is EnumExperimentResultRefusal.ENGINE_NOT_ALLOWED
    assert "Claude" in out.refusals[0].detail


def test_two_violations_at_once() -> None:
    lo, hi = _wilson(48, 60, _Z)
    result = _make_result(
        wilson_low=lo - 0.05,
        wilson_high=hi,
        latency_p50_ms=900.0,
        latency_p90_ms=200.0,
    )
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.FAILED
    codes = {r.refusal for r in out.refusals}
    assert EnumExperimentResultRefusal.INTERVAL_MISMATCH in codes
    assert EnumExperimentResultRefusal.LATENCY_ORDER in codes
    assert len(out.refusals) == 2


# --- failing control -------------------------------------------------------


def test_control_a_policy_that_accepts_any_interval_misses_the_mismatch() -> None:
    lo, hi = _wilson(48, 60, _Z)
    result = _make_result(wilson_low=lo - 0.05, wilson_high=hi)
    # A policy with a very loose tolerance accepts the shifted interval.
    loose = HandlerExperimentResultRecord(policy=_policy(interval_tolerance=0.1))
    out_loose = loose.handle(_request(result))
    assert out_loose.status is EnumExperimentResultRecordStatus.COMPLETED
    assert out_loose.refusals == ()

    # The default tolerance (0.001) rejects it.
    strict = HandlerExperimentResultRecord(policy=_policy())
    out_strict = strict.handle(_request(result))
    assert out_strict.status is EnumExperimentResultRecordStatus.FAILED
    assert len(out_strict.refusals) == 1
    assert (
        out_strict.refusals[0].refusal is EnumExperimentResultRefusal.INTERVAL_MISMATCH
    )


# --- policy validation ------------------------------------------------------


def test_policy_refuses_empty_refused_engines() -> None:
    with pytest.raises(ValidationError):
        ModelExperimentResultPolicy(
            interval_tolerance=0.001,
            confidence_z=_Z,
            refused_engines=(),
        )


def test_policy_refuses_missing_confidence_z() -> None:
    with pytest.raises(ValidationError):
        ModelExperimentResultPolicy(
            **{"interval_tolerance": 0.001, "refused_engines": ("claude",)}
        )


# --- load_policy -----------------------------------------------------------


def test_load_policy_no_overlay_returns_contract_values() -> None:
    policy = load_policy(environ={})
    assert policy.interval_tolerance == pytest.approx(0.001)
    assert policy.confidence_z == pytest.approx(1.959963984540054)
    assert policy.refused_engines == ("claude", "codex")


def test_load_policy_overlay_replaces_contract_values(tmp_path: Path) -> None:
    overlay_dir = tmp_path / "node_experiment_result_record_compute"
    overlay_dir.mkdir()
    overlay = {
        "policy": {
            "interval_tolerance": 0.001,
            "confidence_z": 1.959963984540054,
            "refused_engines": ["openrouter"],
        }
    }
    (overlay_dir / "overlay.yaml").write_text(yaml.dump(overlay), encoding="utf-8")
    environ = {OVERLAY_ROOTS_ENV: str(tmp_path)}
    policy = load_policy(environ=environ)
    assert policy.refused_engines == ("openrouter",)


def test_load_policy_invalid_overlay_raises(tmp_path: Path) -> None:
    overlay_dir = tmp_path / "node_experiment_result_record_compute"
    overlay_dir.mkdir()
    overlay = {
        "policy": {
            "interval_tolerance": 0.001,
            "refused_engines": ["claude"],
            # missing confidence_z
        }
    }
    (overlay_dir / "overlay.yaml").write_text(yaml.dump(overlay), encoding="utf-8")
    environ = {OVERLAY_ROOTS_ENV: str(tmp_path)}
    with pytest.raises(NodeOverlayError):
        load_policy(environ=environ)


# --- handler with no arguments ---------------------------------------------


def test_handler_no_args_loads_contract_policy() -> None:
    handler = HandlerExperimentResultRecord()
    result = _make_result()
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.COMPLETED


# --- contract terminal topics ------------------------------------------------


_RECORDED_TOPIC = "onex.evt.omnimarket.experiment-arm-result-recorded.v1"  # onex-topic-allow: asserted against the contract
_REFUSED_TOPIC = "onex.evt.omnimarket.experiment-arm-result-refused.v1"  # onex-topic-allow: asserted against the contract


def test_the_contract_declares_the_recorded_and_refused_terminal_topics() -> None:
    root = Path(__file__).resolve().parents[0]
    while not (root / "pyproject.toml").is_file():
        root = root.parent
    contract_path = (
        root
        / "src/omnimarket/nodes/node_experiment_result_record_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == _RECORDED_TOPIC
    assert contract["runtime_dispatch"]["terminal_events"]["failure"] == _REFUSED_TOPIC
    assert _RECORDED_TOPIC in contract["event_bus"]["publish_topics"]
    assert _REFUSED_TOPIC in contract["event_bus"]["publish_topics"]
    result = _make_result(n=60, correct=61, skip_wilson=True)
    handler = HandlerExperimentResultRecord(policy=_policy())
    out = handler.handle(_request(result))
    assert out.status is EnumExperimentResultRecordStatus.FAILED
