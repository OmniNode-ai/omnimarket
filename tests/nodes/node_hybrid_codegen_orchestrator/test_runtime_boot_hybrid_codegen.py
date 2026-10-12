# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime boot: one hybrid-codegen-start command reaches hybrid-codegen-completed.

Loads the seven factory contracts from disk, boots them through
``wire_from_manifest`` (the call the runtime kernel makes), publishes a
``ModelCodegenSpec`` start command on the start topic and reads the terminal
event off the bus. Only the bus is in-memory and the inference call is pinned to
a deterministic stub; the contracts, the handler instances the resolver builds
(one per routing entry), the dispatch engine and the dispatch-result applier are
the runtime's own.

The older golden chain and the real-dispatch routing test hand-roll the
publish step (their adapter turns a handler's tuple into events), so they pass
while the runtime drops the tuple, publishes nothing and commits the start
command at lag 0. This test is the one that sees the runtime's own behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.codegen.models import ModelCodegenSpec
from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_llm_codegen_effect.handlers import handler_llm_codegen

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
# The in-memory bus delivers a message to its subscribers inline, one after the
# other in subscription order, so the whole chain downstream of the orchestrator
# runs inside the orchestrator's delivery of codegen-llm-generated. The reducer
# is wired first so its seed of the run's state is delivered before the
# orchestrator's validate command can produce a verdict, which is the order the
# real consumers see in the ordinary case. Kafka does not enforce that order: the
# seed and the verdict are read by independent consumers.
FACTORY_NODES = (
    "node_codegen_outcome_reducer",
    "node_hybrid_codegen_orchestrator",
    "node_llm_codegen_effect",
    "node_generated_code_validator",
    "node_mypy_check_effect",
    "node_contract_serialize_compute",
    "node_codegen_file_writer_effect",
)

T_START = "onex.cmd.omnimarket.hybrid-codegen-start.v1"
T_COMPLETED = "onex.evt.omnimarket.hybrid-codegen-completed.v1"
T_PROBED = (
    "onex.cmd.omnimarket.codegen-llm-generate.v1",
    "onex.evt.omnimarket.codegen-llm-generated.v1",
    "onex.cmd.omnimarket.generated-code-validation-requested.v1",
    "onex.evt.omnimarket.generated-code-validation-completed.v1",
    "onex.evt.omnimarket.codegen-validation-outcome.v1",
    "onex.cmd.omnimarket.mypy-check-requested.v1",
    "onex.evt.omnimarket.mypy-check-completed.v1",
    "onex.evt.omnimarket.codegen-typecheck-outcome.v1",
    "onex.cmd.omnimarket.contract-serialize-requested.v1",
    "onex.evt.omnimarket.contract-serialize-completed.v1",
    "onex.evt.omnimarket.codegen-serialize-outcome.v1",
    "onex.cmd.omnimarket.codegen-file-write.v1",
    "onex.evt.omnimarket.codegen-files-written.v1",
    T_COMPLETED,
)

# Self-contained sources: the real type check runs on the text alone, so a base
# class imported from a package would be judged on that package's own generics.
_GOOD_SOURCE = """\
class NodeCompute:
    \"\"\"Minimal base class for the generated node.\"\"\"


class UpperCaseText(NodeCompute):
    \"\"\"Upper-case a string.\"\"\"

    def handle(self, text: str) -> str:
        return text.upper()
"""

# Parses, defines the class and the method, but the body is a stub: the real
# validator rejects it.
_STUB_SOURCE = """\
class NodeCompute:
    pass


class UpperCaseText(NodeCompute):
    def handle(self, text: str) -> str:
        ...
"""

# Passes validation (parses, class, method, real body) and fails the real type check.
_TYPE_ERROR_SOURCE = """\
class NodeCompute:
    pass


class UpperCaseText(NodeCompute):
    def handle(self, text: str) -> str:
        return 123
"""


class _FixedInference(ModelInferenceAdapter):
    """Deterministic stand-in for the lab model: only the inference call is faked."""

    def __init__(self, source_text: str) -> None:
        self._source_text = source_text

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        response_format: object | None = None,
        protocol_selection: object | None = None,
    ) -> str:
        return self._source_text


def _spec(target_root: Path, correlation_id: str) -> ModelCodegenSpec:
    return ModelCodegenSpec(
        node_name="UpperCaseText",
        namespace="demo.fictional.textutils",
        archetype="compute",
        base_class="NodeCompute",
        handler_method="handle",
        description="Upper-cases an input string.",
        prompt_hint="Return the input text upper-cased.",
        target_root=str(target_root),
        correlation_id=correlation_id,
    )


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    spec: ModelCodegenSpec,
    source_text: str,
    *,
    wait_seconds: float = 30,
) -> tuple[dict[str, list[dict[str, object]]], bool]:
    """Publish one start command; return every probed topic's payloads and whether
    the terminal event arrived."""
    monkeypatch.setattr(
        handler_llm_codegen,
        "build_lab_inference_adapter",
        lambda: _FixedInference(source_text),
    )
    manifest = discover_contracts_from_paths(
        [NODES / name / "contract.yaml" for name in FACTORY_NODES]
    )
    assert not manifest.errors, manifest.errors
    assert {c.name for c in manifest.contracts} == set(FACTORY_NODES)

    bus = EventBusInmemory(environment="hcodegen-boot", group="hcodegen-boot")
    await bus.start()
    seen: dict[str, list[dict[str, object]]] = {topic: [] for topic in T_PROBED}
    completed = asyncio.Event()

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            wire = json.loads(bytes(getattr(message, "value", b"")))
            seen[topic].append(wire.get("payload", wire))
            if topic == T_COMPLETED:
                completed.set()

        return _collect

    for topic in T_PROBED:
        await bus.subscribe(
            topic, on_message=_collector(topic), group_id=f"probe-{topic}"
        )

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == len(FACTORY_NODES)
    engine.freeze()

    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=spec.model_dump(mode="json"), correlation_id=uuid4()
    )
    await bus.publish(T_START, None, envelope.model_dump_json().encode("utf-8"), None)
    arrived = False
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(completed.wait(), timeout=wait_seconds)
        arrived = True
    await bus.close()
    return seen, arrived


def _stops_at(seen: dict[str, list[dict[str, object]]]) -> str:
    """The last probed topic that carried a message, for a readable failure."""
    reached = [topic for topic in T_PROBED if seen[topic]]
    return reached[-1] if reached else T_START


async def test_start_command_reaches_completed_with_files_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "generated"
    spec = _spec(target, correlation_id=str(uuid4()))

    seen, arrived = await _drive(monkeypatch, spec, _GOOD_SOURCE)

    assert arrived, (
        f"no hybrid-codegen-completed event; the chain stopped after {_stops_at(seen)}"
    )
    assert len(seen[T_COMPLETED]) == 1
    completed = seen[T_COMPLETED][0]
    assert completed["status"] == "completed", completed
    written = [Path(str(p)) for p in cast("list[object]", completed["written_paths"])]
    assert {p.name for p in written} == {
        "handler.py",
        "contract.yaml",
        "metadata.yaml",
        ".rsd_provenance.json",
    }
    assert all(p.is_file() and target in p.parents for p in written)
    assert (target / "handler.py").read_text() == _GOOD_SOURCE


async def test_every_hop_publishes_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path / "generated", correlation_id=str(uuid4()))

    seen, arrived = await _drive(monkeypatch, spec, _GOOD_SOURCE)

    assert arrived, f"the chain stopped after {_stops_at(seen)}"
    counts = {topic: len(seen[topic]) for topic in T_PROBED}
    assert counts == dict.fromkeys(T_PROBED, 1), counts


async def test_validation_rejection_completes_rejected_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "generated"
    spec = _spec(target, correlation_id=str(uuid4()))

    seen, arrived = await _drive(monkeypatch, spec, _STUB_SOURCE)

    assert arrived, (
        f"no hybrid-codegen-completed event; the chain stopped after {_stops_at(seen)}"
    )
    completed = seen[T_COMPLETED][0]
    assert completed["status"] == "rejected_validation", completed
    assert completed["written_paths"] == []
    assert completed["issues"], "a rejection must say why"
    assert not target.exists() or not any(target.rglob("*"))
    assert seen["onex.cmd.omnimarket.mypy-check-requested.v1"] == []
    assert seen["onex.cmd.omnimarket.codegen-file-write.v1"] == []


async def test_typecheck_rejection_completes_rejected_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "generated"
    spec = _spec(target, correlation_id=str(uuid4()))

    seen, arrived = await _drive(monkeypatch, spec, _TYPE_ERROR_SOURCE)

    assert arrived, (
        f"no hybrid-codegen-completed event; the chain stopped after {_stops_at(seen)}"
    )
    completed = seen[T_COMPLETED][0]
    assert completed["status"] == "rejected_typecheck", completed
    assert completed["written_paths"] == []
    assert completed["issues"], "a rejection must say why"
    assert not target.exists() or not any(target.rglob("*"))
    assert seen["onex.cmd.omnimarket.contract-serialize-requested.v1"] == []
    assert seen["onex.cmd.omnimarket.codegen-file-write.v1"] == []
