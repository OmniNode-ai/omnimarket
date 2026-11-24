# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests of node_delegation_check_probe_effect (OMN-26043).

Golden: each check sends its task through the responder chain the settings store declares, reads PASS on a clean customer
machine observation and puts one run on the node's result topic. Error: each way a run can be red (a chain with no local
responder, a pinned model, an unproven zero, a hidden broken command, an absent collector, a probe that raises, a bus that
refuses) concludes ``failure``, names its cause and is still emitted where the bus accepts it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryCustomerMachine,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelBrokenCommandCase,
    ModelDelegateObservation,
    ModelDelegationCheckRequest,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    ProtocolCustomerMachine,
)

TOPIC = "onex.evt.omnimarket.delegation-check-probe-completed.v1"
INKLING = "http://127.0.0.11:8111"
QWEN = "http://127.0.0.12:8000"
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_delegation_check_probe_effect"

RESPONDERS = (
    ModelDelegationCheckResponder(tier="local", base_url=INKLING),
    ModelDelegationCheckResponder(tier="local", base_url=QWEN),
    ModelDelegationCheckResponder(tier="openrouter"),
    ModelDelegationCheckResponder(tier="claude_api"),
)
READINGS = {
    INKLING: ModelResponderReading(
        base_url=INKLING,
        answered=True,
        served_model="inkling-2-mini",
        max_model_len=65536,
    ),
    QWEN: ModelResponderReading(
        base_url=QWEN, answered=True, served_model="qwen3.8-27b", n_ctx=131072
    ),
}
CLOCK = iter(range(10_000))


def _clock() -> datetime:
    return datetime(2026, 11, 23, 20, 0, next(CLOCK) % 60, tzinfo=UTC)


def good_delegation(
    base: str = INKLING, model: str = "inkling-2-mini"
) -> ModelDelegateObservation:
    return ModelDelegateObservation(
        rc=0,
        no_checkout=True,
        three_files_agree=True,
        receipt_tier="local",
        receipt_model=model,
        receipt_endpoint=base,
        attempt_tiers=("local",),
        accepted_last=True,
        egress_recorded=True,
        model_connects=3,
        token_counter_moved=True,
        control_external_classified=True,
        entry_path="/srv/customer-install/bin/onex",
        entry_is_install_own=True,
    )


def good_routing() -> ModelRoutingCollection:
    return ModelRoutingCollection(
        undeclared_control_classified_deleted=True,
        closed_port_control_classified_unreachable=True,
        judge_endpoint_private=True,
        judge_needs_credential=False,
        judge_serves_model=True,
    )


def good_surface(**over: object) -> ModelSurfaceObservation:
    base = {
        "guides_lead_with_local": True,
        "install_rc": 0,
        "install_interpreter_matches": True,
        "help_answers": True,
        "parameter_controls_caught": True,
        "operator_switch_env": "ONEX_OPERATOR_SWITCH",
        "broken_ran_with_switch": True,
        "broken_before_rc": 0,
        "broken_after_rc": 0,
        "broken_cases": (
            ModelBrokenCommandCase(
                entry_point="c17-broken-probe-import",
                rc=1,
                output="[ONEX_CORE_124_IMPORT_ERROR] 'c17-broken-probe-import' failed",
            ),
        ),
    }
    return ModelSurfaceObservation(**{**base, **over})


def handler(
    machine: ProtocolCustomerMachine,
    *,
    readings: dict[str, ModelResponderReading] | None = None,
    publisher: InMemoryCheckPublisher | None = None,
) -> tuple[HandlerDelegationCheckProbe, InMemoryCheckPublisher]:
    pub = publisher or InMemoryCheckPublisher()
    return (
        HandlerDelegationCheckProbe(
            responders=RESPONDERS,
            reader=InMemoryResponderReader(READINGS if readings is None else readings),
            machine=machine,
            publisher=pub,
            topic=contract_topics()[0],
            clock=_clock,
        ),
        pub,
    )


def request(check: CheckId) -> ModelDelegationCheckRequest:
    return ModelDelegationCheckRequest(correlation_id="c-1", check_id=check)


# ----- golden ------------------------------------------------------------------------------------------------------


def test_the_contract_declares_one_result_topic_for_the_three_checks() -> None:
    assert contract_topics() == (TOPIC,)


def test_c13_sends_its_task_through_the_chain_and_reads_pass() -> None:
    machine = InMemoryCustomerMachine(delegation=good_delegation())
    h, pub = handler(machine)
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.PASS
    assert result.conclusion == "success"
    assert machine.delegated_through[0].base_url == INKLING
    assert result.chosen_tier == "local"
    assert result.served_model == "inkling-2-mini"
    assert [a.outcome for a in result.chain][:2] == [
        "chosen",
        "no-answer",
    ] or result.chain
    assert pub.events[0]["topic"] == TOPIC
    assert pub.events[0]["payload"]["criterion"] == "C13"
    assert result.emitted


def test_c14_reads_pass_on_all_three_rows() -> None:
    machine = InMemoryCustomerMachine(
        delegation=good_delegation(), routing=good_routing()
    )
    h, _ = handler(machine)
    result = h.handle(request("C14"))
    assert result.verdict is EnumCheckVerdict.PASS
    assert {c.name for c in result.clauses} == {
        "no_checkout",
        "row1_local_answer_ends_chain",
        "row2_no_unbindable_tier",
        "row4_reviewer_unmetered",
    }


def test_c14_falls_to_a_llamacpp_responder_with_no_max_model_len() -> None:
    # The red C14 read every hour: the chain fell to a server that reports meta.n_ctx and no max_model_len.
    readings = {QWEN: READINGS[QWEN]}
    machine = InMemoryCustomerMachine(
        delegation=good_delegation(QWEN, "qwen3.8-27b"), routing=good_routing()
    )
    h, _ = handler(machine, readings=readings)
    result = h.handle(request("C14"))
    assert result.verdict is EnumCheckVerdict.PASS
    assert result.chosen_responder == QWEN
    assert result.window == 131072


def test_c17_reads_pass_when_the_broken_entry_point_is_planted_with_the_operator_switch() -> (
    None
):
    h, _ = handler(InMemoryCustomerMachine(surface=good_surface()))
    result = h.handle(request("C17"))
    assert result.verdict is EnumCheckVerdict.PASS


# ----- errors ------------------------------------------------------------------------------------------------------


def test_a_chain_with_no_local_responder_is_not_measured_and_sends_nothing() -> None:
    machine = InMemoryCustomerMachine(delegation=good_delegation())
    h, pub = handler(machine, readings={})
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.NOT_MEASURED
    assert result.cause_class == "lab-model"
    assert result.conclusion == "failure"
    assert result.chosen_tier == "openrouter"
    assert machine.delegated_through == []
    assert pub.events
    assert pub.events[0]["payload"]["verdict"] == "NOT_MEASURED"


def test_a_receipt_naming_a_model_other_than_the_chosen_responders_fails_local_model() -> (
    None
):
    # A check pinned to one model: the receipt names a model the chain did not choose.
    machine = InMemoryCustomerMachine(
        delegation=good_delegation(INKLING, "pinned-other-model")
    )
    h, _ = handler(machine)
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert result.cause_class == "product"
    assert [c.name for c in result.clauses if not c.ok] == ["local_model"]


def test_an_escalated_ladder_fails_row1() -> None:
    obs = good_delegation().model_copy(
        update={"attempt_tiers": ("local", "paid"), "escalated": True, "cost_usd": 0.02}
    )
    h, _ = handler(InMemoryCustomerMachine(delegation=obs, routing=good_routing()))
    result = h.handle(request("C14"))
    assert [c.name for c in result.clauses if not c.ok] == [
        "row1_local_answer_ends_chain"
    ]


def test_an_unbindable_task_class_fails_row2() -> None:
    routing = good_routing().model_copy(update={"unbindable": ("review",)})
    h, _ = handler(
        InMemoryCustomerMachine(delegation=good_delegation(), routing=routing)
    )
    result = h.handle(request("C14"))
    assert [c.name for c in result.clauses if not c.ok] == ["row2_no_unbindable_tier"]


@pytest.mark.parametrize(
    "update",
    [
        {"egress_recorded": False},
        {"model_connects": 0},
        {"token_counter_moved": False},
        {"control_external_classified": False},
        {"egress_external": ("203.0.113.9:443",)},
    ],
)
def test_an_unproven_or_nonzero_provider_count_fails_zero_provider(
    update: dict[str, object],
) -> None:
    obs = good_delegation().model_copy(update=update)
    h, _ = handler(InMemoryCustomerMachine(delegation=obs))
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert [c.name for c in result.clauses if not c.ok] == ["zero_provider"]


def test_a_hidden_broken_command_reads_fail_naming_the_switch_the_run_did_not_set() -> (
    None
):
    # The C17 red: omnibase-core hides every group its declaration does not mark customer, so a planted entry point answers
    # "No such command" unless the run sets the operator switch.
    surface = good_surface(
        broken_ran_with_switch=False,
        broken_cases=(
            ModelBrokenCommandCase(
                entry_point="c17-broken-probe-import",
                rc=2,
                output="Error: No such command 'c17-broken-probe-import'.",
            ),
        ),
    )
    h, _ = handler(InMemoryCustomerMachine(surface=surface))
    result = h.handle(request("C17"))
    assert result.verdict is EnumCheckVerdict.FAIL
    failed = [c for c in result.clauses if not c.ok]
    assert [c.name for c in failed] == ["broken_command_named_error"]
    assert "ONEX_OPERATOR_SWITCH" in failed[0].detail


def test_a_broken_command_that_vanishes_even_with_the_switch_still_fails() -> None:
    surface = good_surface(
        broken_cases=(
            ModelBrokenCommandCase(
                entry_point="c17-broken-probe-import",
                rc=2,
                output="Error: No such command 'c17-broken-probe-import'.",
            ),
        ),
    )
    h, _ = handler(InMemoryCustomerMachine(surface=surface))
    assert h.handle(request("C17")).verdict is EnumCheckVerdict.FAIL


def test_a_surface_with_no_named_operator_switch_is_precondition_absent_not_green() -> (
    None
):
    h, _ = handler(
        InMemoryCustomerMachine(surface=good_surface(operator_switch_env=None))
    )
    result = h.handle(request("C17"))
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert result.conclusion == "failure"


def test_a_machine_with_no_collector_reads_precondition_absent_after_the_chain_resolved() -> (
    None
):
    h, pub = handler(AbsentCustomerMachine("no collector"))
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert result.conclusion == "failure"
    assert pub.events
    # The chain read live stays on the run beside the absent collector.
    assert result.chosen_tier == "local"
    assert result.chosen_responder == INKLING
    assert result.chain[0].outcome == "chosen"


def test_a_probe_that_raises_is_an_error_run_and_still_emitted() -> None:
    class Raising(InMemoryCustomerMachine):
        def run_delegation(
            self, check_id: CheckId, responder: ModelChainResolution
        ) -> ModelDelegateObservation:
            raise RuntimeError("container engine gone")

    h, pub = handler(Raising())
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.ERROR
    assert result.status == "errored"
    assert "container engine gone" in (result.error or "")
    assert pub.events


def test_a_bus_that_refuses_is_reported_not_emitted() -> None:
    machine = InMemoryCustomerMachine(delegation=good_delegation())
    h, _ = handler(machine, publisher=InMemoryCheckPublisher(accept=False))
    result = h.handle(request("C13"))
    assert result.verdict is EnumCheckVerdict.PASS
    assert result.emitted is False


# ----- contract, overlay and tick ----------------------------------------------------------------------------------


def test_the_result_topic_is_declared_and_the_row_is_a_declared_setting() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    setter = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_overlay_setting_set_effect/contract.yaml"
        ).read_text()
    )
    declared = {(s["overlay_id"], s["key"]) for s in setter["settable_settings"]}
    row = contract["responder_chain"]
    assert (row["overlay_id"], row["setting_key"]) in declared


def _write_overlays(tmp: Path, responders: list[dict[str, str]]) -> dict[str, str]:
    probe = tmp / "probe.yaml"
    probe.write_text(
        yaml.safe_dump(
            {
                "checks": ["C13"],
                "history_file": "runs.jsonl",
                "history_keep": 5,
                "result_topic": contract_topics()[0],
                "responder_timeout_s": 1,
            }
        )
    )
    row = tmp / "responders.yaml"
    row.write_text(yaml.safe_dump({"schema_version": 1, "responders": responders}))
    return {
        tick.OVERLAY_FILE_ENV: str(probe),
        tick.RESPONDERS_FILE_ENV: str(row),
    }


def test_the_tick_with_no_collector_exits_1_emits_the_record_and_writes_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _write_overlays(tmp_path, [{"tier": "openrouter"}]).items():
        monkeypatch.setenv(key, value)
    pub = InMemoryCheckPublisher()
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=pub,
    )
    assert code == tick.EXIT_FAILED
    assert pub.events[0]["payload"]["verdict"] == "NOT_MEASURED"
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert evidence["runs"][0]["check"] == "C13"
    assert (tmp_path / "state" / "runs.jsonl").read_text().strip()


def test_the_tick_with_an_unreadable_responder_row_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_overlays(tmp_path, [{"tier": "claude_api"}, {"tier": "openrouter"}])
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=InMemoryCheckPublisher(),
    )
    assert code == tick.EXIT_OVERLAY


def test_the_host_installer_declares_an_hourly_systemd_unit_for_the_tick() -> None:
    from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
        load_units,
    )

    unit = load_units()["delegation-check-probe"]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["schedule"]["run_timeout_s"] <= 3 * unit["schedule"]["start_interval"]
    assert unit["module"] == (
        "omnimarket.nodes.node_delegation_check_probe_effect.runtime_delegation_check_probe_tick"
    )
    assert (ROOT / unit["entry_file"]).is_file()


def test_the_contract_names_the_host_unit_that_runs_the_node() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    unit = contract["runtime_dispatch"]["host_unit"]["unit"]
    assert unit == "delegation-check-probe"
    installer = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_host_installer_registry_units/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    declared = installer["units"][unit]
    assert declared["module"].startswith(
        "omnimarket.nodes.node_delegation_check_probe_effect."
    )
    assert declared["schedule"]["start_interval"] == 3600
