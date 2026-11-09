# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A boot against its deployment's declared boot set (one-runtime plan, T28, OMN-23119).

The control replays today's dev start (638 contracts attached and 439 subscriptions with no declared set) and must read
FAIL; the same start against a declared set reads FAIL with the excess named; a boot that stays inside the declared set
reads PASS. Nothing here depends on a count written in the handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.models.runtime.model_runtime_node_selection import (
    ENV_RUNTIME_NODE_SELECTION,
    ModelRuntimeNodeSelection,
)

from omnimarket.models.model_runtime_boot_set import ModelBootObservation
from omnimarket.nodes.node_runtime_boot_set_compute.handlers.handler_runtime_boot_set import (
    HandlerRuntimeBootSet,
)
from tests.nodes.node_runtime_boot_set_compute.boot_set_support import (
    ATTACHED_TODAY,
    DECLARED,
    SUBSCRIBED_TODAY,
    declaration,
    declared_boot,
    request,
    todays_boot,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_runtime_boot_set_compute/contract.yaml"
)
HANDLER = HandlerRuntimeBootSet()


def test_control_todays_start_with_no_declared_set_reads_fail() -> None:
    verdict = HANDLER.handle(request(todays_boot(), None))
    assert verdict.status == "FAIL"
    assert verdict.rules == ("NO_DECLARED_SET",)
    assert verdict.loaded_contract_count == ATTACHED_TODAY
    assert verdict.subscribed_topic_count == SUBSCRIBED_TODAY
    assert verdict.contracts_over_declared == ATTACHED_TODAY
    assert verdict.selection_env == ""


def test_todays_start_against_a_declared_set_names_every_contract_and_topic_outside_it() -> (
    None
):
    verdict = HANDLER.handle(request(todays_boot(), declaration()))
    assert verdict.status == "FAIL"
    assert verdict.rules == (
        "LOADED_OUTSIDE_DECLARED_SET",
        "SUBSCRIBED_OUTSIDE_DECLARED_SET",
    )
    assert verdict.contracts_over_declared == ATTACHED_TODAY - len(DECLARED)
    assert len(verdict.excess_contracts) == ATTACHED_TODAY - len(DECLARED)
    assert set(DECLARED).isdisjoint(verdict.excess_contracts)
    assert verdict.topics_over_declared == len(verdict.excess_topics) > 0


def test_a_boot_inside_the_declared_set_reads_pass_and_carries_the_measured_start() -> (
    None
):
    verdict = HANDLER.handle(request(declared_boot(), declaration()))
    assert verdict.status == "PASS"
    assert verdict.rules == ()
    assert (
        verdict.loaded_contract_count
        == verdict.declared_contract_count
        == len(DECLARED)
    )
    assert verdict.contracts_over_declared == verdict.topics_over_declared == 0
    assert verdict.time_to_healthy_seconds == 12.0
    assert verdict.memory_mib == 310.0


def test_a_declared_name_no_contract_announces_is_refused() -> None:
    bad = declaration().model_copy(
        update={"contracts": (*DECLARED, "node_not_announced")}
    )
    verdict = HANDLER.handle(request(declared_boot(), bad))
    assert verdict.status == "FAIL"
    assert "CONTRACT_NOT_ANNOUNCED" in verdict.rules
    assert verdict.unannounced_contracts == ("node_not_announced",)


def test_a_declared_contract_the_boot_dropped_is_named() -> None:
    boot = ModelBootObservation(
        attached_contracts=DECLARED[:-1],
        subscribed_topics=declared_boot().subscribed_topics,
    )
    verdict = HANDLER.handle(request(boot, declaration()))
    assert verdict.status == "FAIL"
    assert verdict.rules == ("DECLARED_NOT_LOADED",)
    assert verdict.missing_contracts == (DECLARED[-1],)


def test_a_declaration_for_another_deployment_fails() -> None:
    verdict = HANDLER.handle(request(declared_boot(), declaration("stability"), "dev"))
    assert verdict.status == "FAIL"
    assert "DECLARATION_FOR_OTHER_DEPLOYMENT" in verdict.rules


@pytest.mark.parametrize(
    "boot",
    [
        ModelBootObservation(read_error="log unreadable: timeout"),
        ModelBootObservation(),
    ],
)
def test_a_boot_that_was_not_read_is_unknown_and_never_a_pass(
    boot: ModelBootObservation,
) -> None:
    verdict = HANDLER.handle(request(boot, declaration()))
    assert verdict.status == "UNKNOWN"
    assert verdict.rules == ("BOOT_NOT_READ",)


def test_the_selection_value_is_the_one_the_runtime_parses_back_to_the_declared_set() -> (
    None
):
    verdict = HANDLER.handle(request(declared_boot(), declaration()))
    selection = ModelRuntimeNodeSelection.from_env(
        {ENV_RUNTIME_NODE_SELECTION: verdict.selection_env}
    )
    assert selection.contracts == DECLARED
    assert selection.is_active
    assert not selection.admits("node_fixture_0001")


def test_the_contract_declares_its_topics_and_wires_none_at_boot() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    topics = contract["topics"]
    assert len(set(topics.values())) == len(topics)
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
