# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R5 (OMN-23183): one run writes one STATUS row and emits one result on the bus.

The default profile is in-memory: a scripted key lists the models, a scripted registry stores the ordered list, a scripted
delegator walks it. Nothing opens a socket and no key is read. The delegation request admits the pinned ``model`` key
(OMN-20844 step 2), so the unmodified production run is green; the error chains are named injected breaks: each makes exactly
one thing wrong and shows the scenario red for that reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_runtime import (
    InMemoryScenarioEventSink,
    JournalScenarioEventSink,
    render_systemd,
)
from omnimarket.models.delegation.wire.model_probe_faults import ModelProbeFaults
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_r5_effect.handlers.handler_scenario_r5 import (
    CONTRACT_PATH,
    NODE_DIR,
    BackendFactory,
    HandlerScenarioR5,
    ModelKeysRegistry,
    ModelPinRefusedError,
    ModelR5Attempt,
    ModelR5Backend,
    ModelR5Inputs,
    ModelR5Receipt,
    ModelR5Runtime,
    ModelRegistryRefusedError,
    ModelScenarioR5RunCommand,
    ScriptedDelegator,
    ScriptedModelList,
    load_inputs,
    load_runtime,
    load_schedule,
    wire_pin_refusal,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 9, 3, 41, 30, tzinfo=UTC)
RUNTIME = load_runtime()
INPUTS = NODE_DIR / RUNTIME.inputs_relpath
PROBE_KEY = "probe-key-value-that-must-never-leave-the-process"
PINNED_RED = [
    "-pinned_model_accepted_a",
    "-pinned_receipt_names_model_a",
    "-pinned_model_accepted_b",
    "-pinned_receipt_names_model_b",
]


class Rows:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def __call__(self, row: str) -> None:
        self.rows.append(row)


def refuse_model_key(inputs: ModelR5Inputs, model: str) -> None:
    """The break that reproduces the old request model: it refuses the ``model`` key."""
    del inputs, model
    raise ModelPinRefusedError("the request refuses the model key: ValidationError")


def admitting_backend(inputs: ModelR5Inputs) -> ModelR5Backend:
    registry = ScriptedModelList(inputs.key_listed_models)
    return ModelR5Backend(
        registry=registry,
        delegator=ScriptedDelegator(registry, inputs, admit=wire_pin_refusal),
    )


def build(
    tmp_path: Path,
    backend: BackendFactory | None = None,
    *,
    runtime: ModelR5Runtime = RUNTIME,
    environment: Mapping[str, str] | None = None,
    inputs_path: Path = INPUTS,
    appender: Callable[[str], None] | None = None,
    sink: InMemoryScenarioEventSink | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> tuple[HandlerScenarioR5, Rows, InMemoryScenarioEventSink]:
    rows = Rows()
    events = sink or InMemoryScenarioEventSink()
    handler = HandlerScenarioR5(
        runtime,
        home=tmp_path,
        environment=environment or {},
        appender=appender or rows,
        sink=events,
        terminal_topic=TERMINAL,
        backend_factory=backend or admitting_backend,
        trigger=trigger,
        clock=lambda: NOW,
        inputs_path=inputs_path,
    )
    return handler, rows, events


def failed(assertions: tuple[str, ...]) -> list[str]:
    return [item for item in assertions if item.startswith("-")]


def changed_inputs(tmp_path: Path, **changes: object) -> Path:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw.update(changes)
    path = tmp_path / "changed.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


GREEN = (
    "inputs_readable",
    "models_added",
    "listing_a_then_b",
    "pinned_model_accepted_a",
    "pinned_receipt_names_model_a",
    "pinned_model_accepted_b",
    "pinned_receipt_names_model_b",
    "forced_first_attempt_a_rate_limited",
    "forced_second_attempt_b_accepted",
    "forced_answer_bytes",
    "unlisted_id_refused_by_add",
    "unlisted_id_stored_nothing",
    "reorder_listing_b_then_a",
    "reorder_answers_on_b",
    "reorder_no_attempt_on_a",
)


# -- golden chain -------------------------------------------------------------------------------------------------------


def test_golden_chain_every_assertion_held_and_one_row_and_one_event(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(tmp_path)

    result = handler.handle(ModelScenarioR5RunCommand())

    assert failed(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert result.assertions == tuple(f"+{name}" for name in GREEN)
    assert len(rows.rows) == 1
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R5"
    assert parsed.slot_start == datetime(2026, 11, 9, 3, 0, tzinfo=UTC)
    assert " | lane=node_delegation_scenario_r5 | " in rows.rows[0]
    assert [(topic, payload["scenario_id"]) for topic, payload in events.events] == [
        (TERMINAL, "R5")
    ]


def test_golden_chain_the_production_run_is_green_pinned_legs_included(
    tmp_path: Path,
) -> None:
    """The request model admits the pinned ``model`` key (OMN-20844 step 2): the production run is green, pinned legs included."""
    handler, rows, _ = build(tmp_path, scripted_default())
    result = handler.run()
    assert failed(result.assertions) == []
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert "+pinned_model_accepted_a" in result.assertions
    assert "+pinned_receipt_names_model_b" in result.assertions
    assert len(rows.rows) == 1


def scripted_default() -> BackendFactory:
    from omnimarket.nodes.node_delegation_scenario_r5_effect.handlers.handler_scenario_r5 import (
        scripted_backend,
    )

    return scripted_backend


def test_error_chain_a_request_that_refuses_the_model_key_turns_exactly_the_pinned_legs_red(
    tmp_path: Path,
) -> None:
    """The failing control for the fix: the old request model refuses the model key, so exactly the pinned legs are red."""

    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = ScriptedModelList(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=ScriptedDelegator(registry, inputs, admit=refuse_model_key),
        )

    result = build(tmp_path, backend)[0].run()
    assert failed(result.assertions) == PINNED_RED
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "pinned_model_accepted_a" in result.detail


def test_golden_chain_manual_trigger_is_recorded_as_manual(tmp_path: Path) -> None:
    handler, rows, _ = build(tmp_path, trigger=EnumScenarioTrigger.MANUAL)
    handler.run()
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.trigger is EnumScenarioTrigger.MANUAL


def test_golden_chain_event_is_journalled(tmp_path: Path) -> None:
    journal = tmp_path / "events.jsonl"
    handler = HandlerScenarioR5(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=Rows(),
        sink=JournalScenarioEventSink(journal),
        terminal_topic=TERMINAL,
        backend_factory=admitting_backend,
        clock=lambda: NOW,
    )
    handler.run()
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["topic"] == TERMINAL


def test_golden_chain_the_forced_history_is_a_rate_limited_then_b_accepted(
    tmp_path: Path,
) -> None:
    inputs = load_inputs(INPUTS)
    backend = admitting_backend(inputs)
    backend.registry.add(inputs.provider, (inputs.model_a, inputs.model_b))
    receipt = backend.delegator.forced(inputs.faults())
    assert [(a.model_id, a.failure_class, a.accepted) for a in receipt.attempts] == [
        (inputs.model_a, "rate_limited", False),
        (inputs.model_b, None, True),
    ]


# -- error chains: one named injected break each ---------------------------------------------------------------------------


class AcceptsAnyId(ScriptedModelList):
    """Break: the add no longer checks the key's list."""

    def add(self, provider: str, models: tuple[str, ...]) -> None:
        self._stored[provider] = models


def test_error_chain_an_add_that_takes_an_id_the_key_does_not_list(
    tmp_path: Path,
) -> None:
    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = AcceptsAnyId(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=ScriptedDelegator(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert "-unlisted_id_refused_by_add" in result.assertions
    assert "-unlisted_id_stored_nothing" in result.assertions
    assert "+listing_a_then_b" in result.assertions


class IgnoresReorder(ScriptedDelegator):
    """Break: the run always walks the order of the first add, whatever the registry lists now."""

    def __init__(
        self,
        registry: ScriptedModelList,
        inputs: ModelR5Inputs,
        *,
        admit: Callable[[ModelR5Inputs, str], None] = wire_pin_refusal,
    ) -> None:
        super().__init__(registry, inputs, admit=admit)
        self._first: tuple[str, ...] | None = None

    def forced(self, faults: ModelProbeFaults) -> ModelR5Receipt:
        listing = self._registry.listing(self._inputs.provider)
        self._first = self._first or listing
        frozen = ScriptedModelList(self._inputs.key_listed_models)
        frozen.add(self._inputs.provider, self._first)
        return ScriptedDelegator(frozen, self._inputs).forced(faults)


def test_error_chain_a_run_that_ignores_the_reorder_still_attempts_a(
    tmp_path: Path,
) -> None:
    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = ScriptedModelList(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=IgnoresReorder(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert failed(result.assertions) == ["-reorder_no_attempt_on_a"]
    assert "+reorder_listing_b_then_a" in result.assertions
    assert "+forced_first_attempt_a_rate_limited" in result.assertions


class StoresReversed(ScriptedModelList):
    """Break: the registry stores the first add in reverse order."""

    def add(self, provider: str, models: tuple[str, ...]) -> None:
        super().add(provider, models)
        if len(self._stored) and not hasattr(self, "_seen"):
            self._seen = True
            self._stored[provider] = tuple(reversed(models))


def test_error_chain_a_listing_in_the_wrong_order(tmp_path: Path) -> None:
    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = StoresReversed(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=ScriptedDelegator(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert "-listing_a_then_b" in result.assertions
    assert "-forced_first_attempt_a_rate_limited" in result.assertions


class IgnoresFaults(ScriptedDelegator):
    """Break: the faults file is not applied, so model A answers."""

    def forced(self, faults: ModelProbeFaults) -> ModelR5Receipt:
        return super().forced(
            ModelProbeFaults.model_construct(mode="faults", faults=())
        )


def test_error_chain_a_faults_file_that_is_not_applied(tmp_path: Path) -> None:
    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = ScriptedModelList(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=IgnoresFaults(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert "-forced_first_attempt_a_rate_limited" in result.assertions
    assert "-forced_second_attempt_b_accepted" in result.assertions
    assert "-reorder_no_attempt_on_a" not in result.assertions


def test_error_chain_a_fault_status_that_types_as_unavailable_is_not_rate_limited(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(
        tmp_path, inputs_path=changed_inputs(tmp_path, fault_status_code=503)
    )
    result = handler.run()
    assert failed(result.assertions) == ["-forced_first_attempt_a_rate_limited"]


class PinnedAnswersOnA(ScriptedDelegator):
    """Break: a delegation pinned to B runs on A."""

    def pinned(self, model: str) -> ModelR5Receipt:
        return super().pinned(self._inputs.model_a)


def test_error_chain_a_pinned_receipt_that_names_another_model(tmp_path: Path) -> None:
    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = ScriptedModelList(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=PinnedAnswersOnA(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert failed(result.assertions) == ["-pinned_receipt_names_model_b"]


class WrongBytes(ScriptedDelegator):
    """Break: the forced run returns another answer than the fixed one."""

    def forced(self, faults: ModelProbeFaults) -> ModelR5Receipt:
        return (
            super()
            .forced(faults)
            .model_copy(update={"final_answer": "### ANSWER\nsomething else"})
        )


def test_error_chain_a_wrong_answer_is_caught_by_the_byte_comparison(
    tmp_path: Path,
) -> None:
    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = ScriptedModelList(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=WrongBytes(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert "-forced_answer_bytes" in result.assertions


def test_error_chain_an_add_the_registry_refuses_is_a_red_run(tmp_path: Path) -> None:
    class RefusesAll(ScriptedModelList):
        def add(self, provider: str, models: tuple[str, ...]) -> None:
            raise ModelRegistryRefusedError("refused")

    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        registry = RefusesAll(inputs.key_listed_models)
        return ModelR5Backend(
            registry=registry,
            delegator=ScriptedDelegator(registry, inputs, admit=wire_pin_refusal),
        )

    result = build(tmp_path, backend)[0].run()
    assert "-models_added" in result.assertions
    assert "-listing_a_then_b" in result.assertions


def test_error_chain_unreadable_inputs_is_a_failure_and_still_writes_one_row(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(tmp_path, inputs_path=tmp_path / "missing.yaml")
    result = handler.run()
    assert result.assertions == ("-inputs_readable",)
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_error_chain_a_ledger_append_that_fails_raises_and_emits_nothing(
    tmp_path: Path,
) -> None:
    def refuse(row: str) -> None:
        raise RuntimeError("the ledger append was rejected")

    handler, _, events = build(tmp_path, appender=refuse)
    with pytest.raises(RuntimeError, match="ledger append"):
        handler.run()
    assert events.events == []


def test_error_chain_a_bus_emit_that_fails_raises_after_the_row_is_written(
    tmp_path: Path,
) -> None:
    class Down:
        def emit(self, topic: str, payload: Mapping[str, object]) -> None:
            raise RuntimeError("the bus emit failed")

    rows = Rows()
    handler = HandlerScenarioR5(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows,
        sink=Down(),
        terminal_topic=TERMINAL,
        backend_factory=admitting_backend,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="bus emit"):
        handler.run()
    assert len(rows.rows) == 1


# -- the real-backend profile: the paid probe key --------------------------------------------------------------------------


REAL = RUNTIME.model_copy(update={"model_backend": "real"})


def test_error_chain_an_empty_probe_key_in_the_real_backend_is_red_never_skipped(
    tmp_path: Path,
) -> None:
    built: list[str] = []

    def backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        built.append("backend")
        return admitting_backend(inputs)

    handler, rows, events = build(
        tmp_path,
        backend,
        runtime=REAL,
        environment={REAL.openrouter_probe_key_env: "  "},
    )
    result = handler.run()
    assert result.assertions == ("+inputs_readable", "-probe_key_present")
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert built == []
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_golden_chain_the_probe_key_never_reaches_a_row_an_event_or_the_result(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(
        tmp_path,
        runtime=REAL,
        environment={REAL.openrouter_probe_key_env: PROBE_KEY},
    )
    result = handler.run()
    assert "+probe_key_present" in result.assertions
    blob = json.dumps(
        [rows.rows, events.events, result.model_dump(mode="json")], default=str
    )
    assert PROBE_KEY not in blob


# -- the real adapters, with their functions injected ---------------------------------------------------------------------------


class FakeStatus:
    def __init__(self, provider: str, models: tuple[str, ...]) -> None:
        self.provider = provider
        self.models = models


def test_the_real_registry_drives_the_add_and_list_functions_with_the_key_read_at_the_call(
    tmp_path: Path,
) -> None:
    from omnimarket.handlers import handler_model_keys as model_keys

    seen: list[tuple[str, str, tuple[str, ...], str | None]] = []
    stored: list[tuple[str, ...]] = []

    def add_key(provider: str, key: str, models: tuple[str, ...]) -> None:
        if "bad" in models:
            raise model_keys.ModelKeyRefusedError(f"{key} is refused")
        seen.append((provider, key, models, os.environ.get("HOME")))
        stored.append(models)

    def list_providers() -> tuple[object, ...]:
        return (FakeStatus("gemini", ("x",)), FakeStatus("openrouter", stored[-1]))

    home = tmp_path / "fresh"
    before = os.environ.get("HOME")
    registry = ModelKeysRegistry(
        home=home,
        read_key=lambda: PROBE_KEY,
        add_key=add_key,
        list_providers=list_providers,
    )
    registry.reset()
    registry.add("openrouter", ("a", "b"))
    assert seen == [("openrouter", PROBE_KEY, ("a", "b"), str(home))]
    assert registry.listing("openrouter") == ("a", "b")
    assert os.environ.get("HOME") == before
    with pytest.raises(ModelRegistryRefusedError) as refusal:
        registry.add("openrouter", ("a", "bad"))
    assert PROBE_KEY not in str(refusal.value)
    assert os.environ.get("HOME") == before


def test_the_production_request_admits_a_pinned_model() -> None:
    inputs = load_inputs(INPUTS)
    wire_pin_refusal(inputs, inputs.model_a)
    wire_pin_refusal(inputs, inputs.model_b)


def test_the_production_request_still_refuses_an_empty_model() -> None:
    inputs = load_inputs(INPUTS)
    with pytest.raises(ModelPinRefusedError, match="refuses the model key"):
        wire_pin_refusal(inputs, "")


def test_the_attempt_and_receipt_models_are_frozen_and_typed() -> None:
    receipt = ModelR5Receipt(
        status="completed",
        attempts=(ModelR5Attempt(model_id="m", failure_class=None, accepted=True),),
        final_answer="x",
    )
    assert receipt.accepted_model == "m"
    assert receipt.walked == ("m",)


def test_the_schedule_block_renders_an_hourly_minute_05_user_timer() -> None:
    schedule = load_schedule(CONTRACT_PATH)
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r5_effect",
        omni_root=Path("/opt/omni"),
        home=Path("/home/lab"),
        python=Path("/opt/omni/omnimarket/.venv/bin/python"),
        environment={"ONEX_HOST_NAME": "lab-a"},
    )
    assert "OnCalendar=*-*-* *:05:00 UTC" in timer
    assert "-m omnimarket.nodes.node_delegation_scenario_r5_effect run" in service
    assert 'Environment="ONEX_HOST_NAME=lab-a"' in service
