# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic live assignment, incident attribution and binding controls."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    ModelBindingRenderRequest,
    ModelCompactionClientBinding,
)
from omnimarket.nodes.node_claude_code_binding_render_compute.handlers.handler_binding_render import (
    HandlerClaudeCodeBindingRender,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    load_client_section,
)
from omnimarket.nodes.node_compaction_arm_assignment_compute.handlers import (
    handler_arm_assignment as implementation,
)
from omnimarket.nodes.node_compaction_arm_assignment_compute.handlers.handler_arm_assignment import (
    HandlerCompactionArmAssignment,
)
from omnimarket.nodes.node_compaction_arm_assignment_compute.models.model_arm_assignment import (
    ModelArmAssignmentRequest,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_compaction_arm_assignment_compute"
NOW = datetime(2026, 11, 1, tzinfo=UTC)
HASH = "a" * 64


def request(**updates: object) -> ModelArmAssignmentRequest:
    payload: dict[str, object] = {
        "experiment_id": "scratch-experiment",
        "seed": "preregistered-seed",
        "window_turns": 25,
        "ledger_lane": "scratch-lane",
        "ledger_ticket": "TKT-1",
        "friction_cost_estimate": "est ~1 lane-minutes; safety-stop handling",
        "hook_events": [
            {
                "event_id": f"e{i:02}",
                "event_time": NOW,
                "session_id": f"s{i}",
                "role": "orchestrator",
            }
            for i in range(6)
        ],
    }
    payload.update(updates)
    return ModelArmAssignmentRequest.model_validate(payload)


def decide(**updates: object):
    return HandlerCompactionArmAssignment().handle(request(**updates))


def on_session() -> str:
    return next(a.session_id for a in decide().assignments if a.arm == "on")


def boundary(index: int = 0, **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "session_id": on_session(),
        "boundary_uuid": f"b{index}",
        "event_id": f"boundary-{index}",
        "event_time": NOW + timedelta(minutes=1),
        "assistant_turn": 10,
        "package_id": "package",
        "installed_messages_hash": HASH,
    }
    row.update(updates)
    return row


def manifest(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "package_id": "package",
        "installed_messages_hash": HASH,
        "source_ref": "pack-build/package/manifest.json",
        "rendered_stamps": {},
    }
    row.update(updates)
    return row


def signal(item: str = "ruling-a", **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "event_id": f"act-{item}",
        "event_time": NOW + timedelta(minutes=2),
        "session_id": on_session(),
        "boundary_uuid": "b0",
        "item_key": item,
        "assistant_turn": 11,
        "kind": "act_against_ruling",
        "in_force": True,
        "expected_stamp": "keep the approved scope",
        "source": "hook_event",
    }
    row.update(updates)
    return row


def incident_case(**updates: object):
    payload: dict[str, object] = {
        "boundaries": [boundary()],
        "build_manifests": [manifest()],
        "signals": [signal(), signal("hold-b", kind="act_against_hold")],
    }
    payload.update(updates)
    return decide(**payload)


def assert_tripped(result) -> None:
    assert result.kill_switch is True
    assert len(result.incidents) == 2
    assert {a.arm for a in result.assignments} == {"off"}
    assert len(result.ledger_rows) == 1
    assert " | FRICTION | " in result.ledger_rows[0]
    assert "experiment_id=scratch-experiment" in result.ledger_rows[0]


def assert_not_tripped(result, count: int) -> None:
    assert result.kill_switch is False
    assert len(result.incidents) == count
    assert result.ledger_rows == ()


def test_projection_order_seeded_blocks_and_two_readers() -> None:
    original = request()
    shuffled = original.model_copy(
        update={"hook_events": tuple(reversed(original.hook_events))}
    )
    first = HandlerCompactionArmAssignment().handle(original)
    assert first == HandlerCompactionArmAssignment().handle(shuffled)
    for block in range(3):
        rows = first.assignments[block * 2 : block * 2 + 2]
        assert {a.arm for a in rows} == {"on", "off"}
        digest = hashlib.sha256(
            json.dumps(
                [original.seed, original.experiment_id, block], separators=(",", ":")
            ).encode()
        ).digest()
        assert rows[0].arm == ("on", "off")[digest[0] & 1]


def test_first_hook_event_only_and_eligibility() -> None:
    events = [e.model_dump() for e in request().hook_events]
    events += [
        dict(events[0], event_id="later", event_time=NOW + timedelta(seconds=1)),
        dict(events[0], session_id="worker", event_id="worker", role="worker"),
    ]
    result = decide(hook_events=events)
    assert len(result.assignments) == len(result.markers) == 6
    assert [m.event_id for m in result.markers] == [f"e{i:02}" for i in range(6)]
    assert all(
        m.experiment_start and m.experiment_id == "scratch-experiment"
        for m in result.markers
    )
    assert [m.arm for m in result.markers] == [
        a.assigned_arm for a in result.assignments
    ]


def test_event_time_precedes_event_id_in_projection_order() -> None:
    hooks = [e.model_dump() for e in request().hook_events]
    hooks[0]["event_time"] = NOW + timedelta(hours=1)
    result = decide(hook_events=hooks)
    assert [a.session_id for a in result.assignments] == [
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s0",
    ]


def test_same_item_in_distinct_boundaries_is_two_incidents() -> None:
    second = signal(event_id="other-boundary-act", boundary_uuid="b1")
    assert_tripped(
        incident_case(boundaries=[boundary(), boundary(1)], signals=[signal(), second])
    )


def test_conflicting_manifests_are_unavailable_in_both_reader_orders() -> None:
    manifests = [
        manifest(),
        manifest(rendered_stamps={"ruling-a": "keep the approved scope"}),
    ]
    result = incident_case(build_manifests=manifests)
    assert result == incident_case(build_manifests=list(reversed(manifests)))
    assert result.boundary_evidence[0].status == "UNAVAILABLE"
    assert result.incidents == ()


def test_conflicting_projection_identity_is_refused() -> None:
    hooks = [e.model_dump() for e in request().hook_events]
    hooks.append(dict(hooks[0], session_id="conflicting-session"))
    with pytest.raises(ValueError, match="conflicting hook event identity"):
        decide(hook_events=hooks)
    with pytest.raises(ValueError, match="conflicting boundary identity"):
        decide(boundaries=[boundary(), boundary(installed_messages_hash="b" * 64)])
    with pytest.raises(ValueError, match="conflicting incident event identity"):
        incident_case(signals=[signal(), signal(item="other", event_id="act-ruling-a")])


def test_two_distinct_incidents_disable_and_write_friction() -> None:
    result = incident_case()
    assert_tripped(result)
    assert [i.dedup_key for i in result.incidents] == [
        ("scratch-experiment", on_session(), "b0", "hold-b"),
        ("scratch-experiment", on_session(), "b0", "ruling-a"),
    ]
    assert result == incident_case(
        signals=[signal("hold-b", kind="act_against_hold"), signal()]
    )


def test_two_incidents_append_one_friction_row_through_existing_effect(
    tmp_path: Path,
) -> None:
    """Exercise the canonical writer and its idempotency against a scratch ledger."""
    from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
    from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
        HandlerWorkLedgerAppendEffect,
    )
    from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
        ModelAppendCommandResult,
    )

    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text("")

    class ScratchLedger:
        def append(self, rows: str) -> ModelAppendCommandResult:
            with ledger.open("a") as stream:
                stream.write(rows)
            return ModelAppendCommandResult(exit_code=0)

        def read_text(self) -> str:
            return ledger.read_text()

    result = incident_case()
    assert_tripped(result)
    assert result.ledger_request_id is not None
    port = ScratchLedger()
    writer = HandlerWorkLedgerAppendEffect(
        runner=port,
        reader=port,
        host_name="scratch-host",
        writer_principals=["scratch-principal"],
    )
    command = ModelWorkLedgerAppendRequest(
        request_id=result.ledger_request_id,
        rows="\n".join(result.ledger_rows) + "\n",
        requested_by_lane="scratch-lane",
        requesting_host="scratch-host",
        requested_at=NOW,
    )
    first = writer.handle(command, writer_principal="scratch-principal")
    assert first.status.value == "accepted", first.message
    repeated = writer.handle(command, writer_principal="scratch-principal")
    assert repeated.status.value == "duplicate", repeated.message
    rows = ledger.read_text().splitlines()
    assert len(rows) == 1
    assert " | FRICTION | " in rows[0]
    refused = writer.handle(
        command.model_copy(
            update={
                "rows": command.rows.replace(f" | req={result.ledger_request_id}", "")
            }
        ),
        writer_principal="scratch-principal",
    )
    assert refused.status.value == "refused"


def test_duplicate_key_counts_once() -> None:
    assert_not_tripped(incident_case(signals=[signal(), signal(event_id="retry")]), 1)


@pytest.mark.parametrize("kind", ["question_guard_refusal", "operator_restatement"])
def test_signal_alone_is_not_an_incident(kind: str) -> None:
    assert_not_tripped(
        incident_case(signals=[signal(kind=kind), signal("other", kind=kind)]), 0
    )


@pytest.mark.parametrize(
    "change",
    [
        {"in_force": False},
        {"assistant_turn": 10},
        {"assistant_turn": 36},
        {"session_id": "missing"},
        {"boundary_uuid": "missing"},
        {"event_time": NOW},
    ],
)
def test_incident_requires_attribution_and_window(change: dict[str, object]) -> None:
    assert_not_tripped(incident_case(signals=[signal(**change)]), 0)


def test_present_stamp_is_not_attributed_but_contradicted_stamp_is() -> None:
    result = incident_case(
        build_manifests=[
            manifest(
                contradicted_item_keys=["hold-b"],
                rendered_stamps={
                    "ruling-a": "keep the approved scope",
                    "hold-b": "ignore the hold",
                },
            )
        ]
    )
    assert_not_tripped(result, 1)
    assert result.incidents[0].stamp_evidence == "CONTRADICTED"


def test_different_wording_alone_does_not_establish_a_contradicted_stamp() -> None:
    result = incident_case(
        signals=[signal()],
        build_manifests=[
            manifest(rendered_stamps={"ruling-a": "retain the approved scope"})
        ],
    )
    assert_not_tripped(result, 0)


@pytest.mark.parametrize(
    "manifests",
    [[], [manifest(installed_messages_hash="b" * 64)], [manifest(package_id="other")]],
)
def test_missing_or_wrong_manifest_is_unavailable_not_absent(manifests) -> None:
    result = incident_case(build_manifests=manifests)
    assert_not_tripped(result, 0)
    assert result.boundary_evidence[0].status == "UNAVAILABLE"
    assert result.hold_reasons == ("NO_PACKAGE_EVIDENCE",)
    assert result.status == "HOLD"


def test_unreadable_rate_is_strictly_greater_than_ten_percent() -> None:
    rows = [
        boundary(i, installed_messages_hash=("b" * 64 if i == 0 else HASH))
        for i in range(10)
    ]
    result = decide(boundaries=rows, build_manifests=[manifest()])
    assert result.unreadable_on_boundaries == 1
    assert result.on_boundaries == 10
    assert result.status == "READY"
    result = decide(boundaries=rows[:9], build_manifests=[manifest()])
    assert result.status == "HOLD"
    assert result.hold_reasons == ("NO_PACKAGE_EVIDENCE",)
    assert decide().status == "READY"


def test_duplicate_boundary_cannot_dilute_unreadable_rate() -> None:
    unreadable = boundary(0, installed_messages_hash="b" * 64)
    good = boundary(1)
    result = decide(
        boundaries=[unreadable] + [good] * 100, build_manifests=[manifest()]
    )
    assert result.on_boundaries == 2
    assert result.unreadable_on_boundaries == 1
    assert result.status == "HOLD"


def test_off_boundary_cannot_create_an_incident_or_enter_evidence_denominator() -> None:
    off = next(a.session_id for a in decide().assignments if a.arm == "off")
    result = decide(
        boundaries=[boundary(session_id=off)], signals=[signal(session_id=off)]
    )
    assert_not_tripped(result, 0)
    assert result.on_boundaries == 0


def test_later_session_starts_off_after_trip() -> None:
    hooks = [e.model_dump() for e in request().hook_events]
    hooks += [
        dict(
            hooks[0],
            session_id="late",
            event_id="late",
            event_time=NOW + timedelta(hours=1),
        )
    ]
    result = incident_case(hook_events=hooks)
    assert_tripped(result)
    assert result.markers[-1].session_id == "late"
    assert result.markers[-1].arm == "off"


def test_ac2_ignore_incident_count_mutant_is_killed(monkeypatch) -> None:
    monkeypatch.setattr(implementation, "_kill_switch_reached", lambda _keys: False)
    with pytest.raises(AssertionError):
        assert_tripped(incident_case())


def test_ac2_count_duplicate_mutant_is_killed(monkeypatch) -> None:
    monkeypatch.setattr(
        implementation,
        "_incident_key",
        lambda experiment, row: (
            experiment,
            row.session_id,
            row.boundary_uuid,
            row.event_id,
        ),
    )
    with pytest.raises(AssertionError):
        assert_not_tripped(
            incident_case(signals=[signal(), signal(event_id="retry")]), 1
        )


def test_ac2_count_refusal_mutant_is_killed(monkeypatch) -> None:
    monkeypatch.setattr(implementation, "_is_incident_kind", lambda _kind: True)
    with pytest.raises(AssertionError):
        assert_not_tripped(
            incident_case(
                signals=[
                    signal(kind="question_guard_refusal"),
                    signal("other", kind="question_guard_refusal"),
                ]
            ),
            0,
        )


def test_binding_off_renders_native_without_adapter_process() -> None:
    row = load_client_section().bindings[0].model_copy(update={"arm": "off"})
    rendered = (
        HandlerClaudeCodeBindingRender()
        .handle(
            ModelBindingRenderRequest(
                bindings=(row,), max_mode=EnumClientMaxMode.REQUIRED
            )
        )
        .module_text
    )
    assert 'const ARM_compaction = "off"' in rendered
    assert "return next(e)" in rendered
    callback = rendered.split('on("session.compact"', 1)[1]
    assert "$.process.run" not in callback


def test_binding_on_and_default_keep_existing_adapter_call() -> None:
    row = load_client_section().bindings[0]
    assert row.arm == "on"
    assert ModelCompactionClientBinding.model_validate(row.model_dump()).arm == "on"
    rendered = (
        HandlerClaudeCodeBindingRender()
        .handle(
            ModelBindingRenderRequest(
                bindings=(row,), max_mode=EnumClientMaxMode.OBSERVE
            )
        )
        .module_text
    )
    assert 'const ARM_compaction = "on"' in rendered
    assert "$.process.run" in rendered


def test_contract_bus_topics_and_entrypoint() -> None:
    import importlib
    import tomllib

    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["descriptor"]["purity"] == "pure"
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.compaction-arms-assigned.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    module = project["project"]["entry-points"]["onex.nodes"][
        "node_compaction_arm_assignment_compute"
    ]
    assert importlib.import_module(module).NodeCompactionArmAssignmentCompute


def test_off_adapter_never_publishes_or_reads_a_barrier() -> None:
    from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
        HandlerClaudeCodeHookAdapter,
    )
    from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
        ModelHookAdapterRequest,
    )

    binding = load_client_section().bindings[0].model_copy(update={"arm": "off"})
    result = HandlerClaudeCodeHookAdapter().handle(
        ModelHookAdapterRequest(binding=binding, hook_input={})
    )
    assert result.client_verdict.action.value == "native"
    assert result.hook_output is None


@pytest.mark.parametrize("arm", ["on", "off"])
def test_binding_arm_survives_bus_roundtrip_and_render(arm: str) -> None:
    from omnimarket.nodes.node_claude_code_binding_install_effect.models import (
        ModelCompactionBindingInstallRequest,
    )

    binding = load_client_section().bindings[0].model_copy(update={"arm": arm})
    original = ModelCompactionBindingInstallRequest(
        bindings=(binding,),
        max_mode=EnumClientMaxMode.REQUIRED,
        mods_dir="/scratch/mods",
        settings_path="/scratch/settings.json",
        supported_probe_rows=binding.probe_rows,
    )
    restored = ModelCompactionBindingInstallRequest.model_validate_json(
        original.model_dump_json()
    )
    assert isinstance(restored.bindings[0], ModelCompactionClientBinding)
    assert restored.bindings[0].arm == arm
    rendered = (
        HandlerClaudeCodeBindingRender()
        .handle(
            ModelBindingRenderRequest(
                bindings=restored.bindings, max_mode=restored.max_mode
            )
        )
        .module_text
    )
    assert f'const ARM_compaction = "{arm}"' in rendered
    callback = rendered.split('on("session.compact"', 1)[1]
    assert ("$.process.run" in callback) is (arm == "on")


@pytest.mark.asyncio
async def test_same_fixture_under_selected_bus_profile(tmp_path: Path) -> None:
    """Default in-memory; the lab selects redpanda on this exact fixture/test.

    A refused or unreachable broker fails this test. It never becomes a skip or
    silently substitutes the in-memory bus for the selected Redpanda profile.
    """
    import os

    from tests.runtime_local_compat import RuntimeLocal

    profiles = yaml.safe_load(Path(__file__).with_name("profiles.yaml").read_text())
    profile = profiles["profiles"][
        os.environ.get("ONEX_COMPACTION_TEST_PROFILE", profiles["default"])
    ]
    backends = {"event_bus": profile["event_bus"]}
    if profile["event_bus"] == "kafka":
        from tests.bus_lane_guard import (
            assert_broker_in_declared_lane,
            resolve_test_bus_broker,
        )

        assert profile["broker_source"] == "declared_test_bus_lane"
        broker = resolve_test_bus_broker()
        assert_broker_in_declared_lane(broker)
        backends["kafka_bootstrap"] = broker
    fixture = request(
        boundaries=[boundary()],
        build_manifests=[manifest()],
        signals=[signal(), signal("hold-b", kind="act_against_hold")],
    )
    input_path = tmp_path / "request.json"
    input_path.write_text(fixture.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=NODE / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides=backends,
        timeout=30,
    )
    await runtime.run_async()
    result = runtime.handler_result
    assert result is not None
    assert_tripped(result)
    assert result.model_dump(
        exclude={"correlation_id"}
    ) == HandlerCompactionArmAssignment().handle(fixture).model_dump(
        exclude={"correlation_id"}
    )
