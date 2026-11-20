# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998 seam tests: record compute -> recorded terminal -> projection fold.

The change is one edge: ``node_experiment_result_record_compute`` publishes
its recorded terminal and ``node_projection_experiment_result`` folds it. Each
test below pins ONE seam of that edge and goes red when EITHER side changes
shape -- a renamed field on the event side, a re-pointed topic on either
contract, a column dropped from the served exposure.

S1 pins the topic wiring from both contracts on disk. S2 and S3 boot ONLY the
record node through the runtime's own wiring (``wire_from_manifest`` over its
contract, ``EventBusInmemory``, ``MessageDispatchEngine``) and fold what
actually arrives on the terminal topics -- the projection side is exercised
through its real request model and real fold, never a re-implementation. S4
pins the field sets with no bus at all. S5 is a failing control per seam: a
named injected break that must turn the check red while the unbroken contract
stays green.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.topic_constants import derive_event_type_alias_for_topic
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from pydantic import ValidationError

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResult,
    ModelExperimentArmResultRecorded,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_record_status import (
    EnumExperimentResultRecordStatus,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_refusal import (
    EnumExperimentResultRefusal,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_record_request import (
    ModelExperimentResultRecordRequest,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_record_result import (
    ModelExperimentResultRecordResult,
)
from omnimarket.nodes.node_projection_experiment_result.handlers import (
    HandlerProjectionExperimentResult,
)
from omnimarket.nodes.node_projection_experiment_result.models import (
    ModelExperimentResultProjectionRequest,
    ModelExperimentResultRow,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
RECORD_NODE = NODES / "node_experiment_result_record_compute"
PROJECTION_NODE = NODES / "node_projection_experiment_result"

# The record node's policy z, from its contract config section (the packaged
# values its own unit test pins); the interval must be recomputed with it.
_Z = 1.959963984540054
_TENANT = "11111111-1111-1111-1111-111111111111"
_T0 = dt.datetime(2026, 11, 13, tzinfo=dt.UTC)


def _contract(node: Path) -> dict[str, Any]:
    loaded = yaml.safe_load((node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _wilson(correct: int, n: int, z: float = _Z) -> tuple[float, float]:
    """Independent Wilson interval helper used only by these tests."""
    p = correct / n
    denominator = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _a_result(**updates: Any) -> dict[str, Any]:
    """One ModelExperimentArmResult-shaped dict; the Wilson interval recomputed."""
    n = updates.get("n", 60)
    correct = updates.get("correct", 48)
    wilson_low, wilson_high = _wilson(correct, n)
    return {
        "tenant_id": _TENANT,
        "experiment_id": "exp-seam-1",
        "arm": "arm-a",
        "model_id": "model-x",
        "engine": "local",
        "n": n,
        "correct": correct,
        "wilson_low": wilson_low,
        "wilson_high": wilson_high,
        "baseline_accuracy": 0.7,
        "latency_p50_ms": 200.0,
        "latency_p90_ms": 900.0,
        "gpu_seconds": 12.5,
        "corpus_sha256": "a" * 64,
        "truth_sha256": "b" * 64,
        "report_ref": "",
        "measured_at": _T0.isoformat(),
    } | updates


# --- S1: the topic seam, both contracts read from disk ----------------------


def topic_seam_problems(
    record_contract: dict[str, Any], projection_contract: dict[str, Any]
) -> list[str]:
    """Every way the record->projection topic wiring can be wrong.

    The projection must subscribe to the record node's SUCCESS terminal (it is
    the recorded event the fold consumes) and must NOT subscribe to its FAILURE
    terminal (a refused result is not a recorded one; folding it would write a
    row the check node refused).
    """
    terminals = record_contract["runtime_dispatch"]["terminal_events"]
    success = terminals["success"]
    failure = terminals["failure"]
    subscribed = projection_contract["event_bus"]["subscribe_topics"]
    problems: list[str] = []
    if success not in subscribed:
        problems.append(
            f"the projection does not subscribe to the record node's success "
            f"terminal {success!r}"
        )
    if failure in subscribed:
        problems.append(
            f"the projection subscribes to the record node's failure terminal "
            f"{failure!r}"
        )
    return problems


def test_the_projection_subscribes_to_the_recorded_terminal_only() -> None:
    assert topic_seam_problems(_contract(RECORD_NODE), _contract(PROJECTION_NODE)) == []


# --- S2/S3: the payload and error-chain seams on the in-memory bus -----------


async def _drive(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Boot ONLY the record node, publish one command, collect the terminals.

    The wiring is the runtime's own: discovery over the contract on disk,
    ``wire_from_manifest``, a real ``MessageDispatchEngine`` and an
    ``EventBusInmemory``. The command envelope is built and published the way a
    bus publisher does (a ``ModelEventEnvelope`` carrying the request dump,
    stamped with the topic-derived event_type alias); the probe subscribers are
    attached BEFORE the wiring so a terminal published during wiring is still
    seen.
    """
    record_contract = _contract(RECORD_NODE)
    command_topic = record_contract["runtime_dispatch"]["command_topic"]
    terminals = record_contract["runtime_dispatch"]["terminal_events"]
    collect_topics = (terminals["success"], terminals["failure"])

    manifest = discover_contracts_from_paths([RECORD_NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="experiment-result-seam", group="record-node")
    await bus.start()
    seen: dict[str, list[dict[str, Any]]] = {topic: [] for topic in collect_topics}
    arrived = asyncio.Event()

    def _collector(topic: str) -> Any:
        async def collect(message: object) -> None:
            seen[topic].append(json.loads(bytes(getattr(message, "value", b""))))
            arrived.set()

        return collect

    for topic in collect_topics:
        await bus.subscribe(
            topic, group_id=f"seam-probe-{topic}", on_message=_collector(topic)
        )

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 1
    engine.freeze()

    alias = derive_event_type_alias_for_topic(command_topic)
    assert alias is not None
    request = ModelExperimentResultRecordRequest.model_validate({"result": result})
    command: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=request.model_dump(mode="json"),
        correlation_id=uuid4(),
        event_type=alias,
    )
    await bus.publish(
        command_topic, None, command.model_dump_json().encode("utf-8"), None
    )
    # A timeout here is not a test error: the assertions below report what did
    # or did not arrive, which is the finding either way.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=30)
    for _ in range(40):
        if any(seen[topic] for topic in collect_topics):
            break
        await asyncio.sleep(0.05)
    await bus.close()
    return seen


def _terminals() -> tuple[str, str]:
    terminals = _contract(RECORD_NODE)["runtime_dispatch"]["terminal_events"]
    return terminals["success"], terminals["failure"]


async def test_the_terminal_payload_folds_into_a_projection_row() -> None:
    success, failure = _terminals()
    result = _a_result()
    seen = await _drive(result)

    assert len(seen[success]) == 1, "no terminal arrived on the success topic"
    assert seen[failure] == []

    # The real shape on the bus (read off the runtime's publish path,
    # DispatchResultApplier.apply -> EventBusInmemory.publish_envelope): a
    # ModelEventEnvelope whose ``payload`` is the record node's WHOLE output
    # model in JSON mode -- ``status``, ``result_id``, ``result``, ``accuracy``
    # and ``refusals`` -- beside the envelope's own keys. The projection's
    # request model reads that payload as a whole and ignores the keys it does
    # not consume (status, refusals), which is why extra="ignore".
    payload = seen[success][0]["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "completed"
    assert payload["refusals"] == []

    request = ModelExperimentResultProjectionRequest.model_validate(payload)
    assert request.result_id == UUID(payload["result_id"])
    assert request.accuracy == pytest.approx(result["correct"] / result["n"])
    row = HandlerProjectionExperimentResult().handle(request).row
    assert row.accuracy == pytest.approx(result["correct"] / result["n"])
    # Every other row field is the command's result field, unchanged.
    sent = ModelExperimentArmResult.model_validate(result)
    for name, value in sent.model_dump().items():
        assert getattr(row, name) == value, name


async def test_a_mismatched_interval_lands_on_the_failure_terminal() -> None:
    success, failure = _terminals()
    low, _high = _wilson(48, 60)
    result = _a_result(wilson_low=low - 0.05)  # beyond the 0.001 tolerance
    seen = await _drive(result)

    assert seen[success] == [], "a refused result must not reach the recorded topic"
    assert len(seen[failure]) == 1, "no terminal arrived on the failure topic"

    payload = seen[failure][0]["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "failed"
    codes = {refusal["refusal"] for refusal in payload["refusals"]}
    assert EnumExperimentResultRefusal.INTERVAL_MISMATCH.value in codes


# --- S4: the field seams, no bus --------------------------------------------


def column_seam_problems(projection_contract: dict[str, Any]) -> list[str]:
    """Ways the served exposure columns can drift from the row model."""
    (exposure,) = projection_contract["projection_api"]["exposures"]
    expected = [*ModelExperimentResultRow.model_fields, "projection_cursor"]
    columns = exposure["columns"]
    problems: list[str] = []
    if columns != expected:
        problems.append(
            f"exposure columns {columns!r} are not the row model's fields "
            f"{list(ModelExperimentResultRow.model_fields)!r} plus "
            f"'projection_cursor' (in order)"
        )
    return problems


def test_the_three_models_field_sets_line_up() -> None:
    recorded_fields = set(ModelExperimentArmResultRecorded.model_fields)
    output_fields = set(ModelExperimentResultRecordResult.model_fields)
    assert recorded_fields <= output_fields
    # The output is the recorded payload plus its own status and refusals.
    assert output_fields - recorded_fields == {"status", "refusals"}
    # The projection request reads exactly the recorded payload's fields.
    assert set(ModelExperimentResultProjectionRequest.model_fields) == recorded_fields
    # The row is the arm result plus the three fields the fold derives.
    assert set(ModelExperimentResultRow.model_fields) == (
        set(ModelExperimentArmResult.model_fields)
        | {"accuracy", "advantage_over_baseline", "clears_baseline"}
    )


def test_the_exposure_columns_are_the_row_fields_plus_the_cursor() -> None:
    assert column_seam_problems(_contract(PROJECTION_NODE)) == []


# --- S5: failing controls, one named injected break per seam -----------------


def _the_recorded_payload() -> dict[str, Any]:
    """The recorded payload as the record node's own output model dumps it."""
    return ModelExperimentResultRecordResult(
        status=EnumExperimentResultRecordStatus.COMPLETED,
        result_id=uuid4(),
        result=ModelExperimentArmResult.model_validate(_a_result()),
        accuracy=0.8,
    ).model_dump(mode="json")


def test_control_a_renamed_field_on_the_event_side() -> None:
    # The record node renames `correct` to `right`: the projection's request
    # model refuses the payload (correct is required, right is ignored extra),
    # so the event lands on the projection's DLQ instead of folding.
    payload = _the_recorded_payload()
    payload["result"]["right"] = payload["result"].pop("correct")
    with pytest.raises(ValidationError):
        ModelExperimentResultProjectionRequest.model_validate(payload)
    # The unbroken payload still validates: the rename is what refuses it.
    assert ModelExperimentResultProjectionRequest.model_validate(
        _the_recorded_payload()
    )


def test_control_a_projection_that_subscribes_to_the_failure_topic() -> None:
    record_contract = _contract(RECORD_NODE)
    projection_contract = _contract(PROJECTION_NODE)
    assert topic_seam_problems(record_contract, projection_contract) == []

    mutated = copy.deepcopy(projection_contract)
    failure = record_contract["runtime_dispatch"]["terminal_events"]["failure"]
    mutated["event_bus"]["subscribe_topics"].append(failure)
    assert topic_seam_problems(record_contract, mutated) != []


def test_control_a_column_dropped_from_the_exposure() -> None:
    projection_contract = _contract(PROJECTION_NODE)
    assert column_seam_problems(projection_contract) == []

    mutated = copy.deepcopy(projection_contract)
    mutated["projection_api"]["exposures"][0]["columns"].remove("accuracy")
    assert column_seam_problems(mutated) != []
