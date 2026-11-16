# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The never-produced listing: evidence in, names out, nothing deleted (OMN-23614)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.model_topic_never_produced import (
    ModelNeverProducedReport,
    ModelNeverProducedRequest,
    ModelNeverProducedRule,
    ModelTopicEvidence,
)
from omnimarket.nodes.node_topic_never_produced_compute import (
    NodeTopicNeverProducedCompute,
)
from omnimarket.nodes.node_topic_never_produced_compute.handlers.handler_topic_never_produced import (
    HandlerTopicNeverProduced,
)
from omnimarket.nodes.node_topic_never_produced_compute.overlay import (
    NODE_NAME,
    load_never_produced_rule,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

RULE = ModelNeverProducedRule(
    protected_prefixes=("__", "_schemas"),
    list_declared_producers=False,
)

NEVER_PRODUCED_EVIDENCE = (
    "no record was ever produced (high watermark total 0)",
    "no data inside retention",
    "no consumer group reads it",
    "no contract declares a producer",
)


def _topic(name: str, **update: object) -> ModelTopicEvidence:
    base: dict[str, object] = {
        "name": name,
        "partitions": 3,
        "high_watermark_total": 0,
        "records_in_retention": 0,
        "consumer_groups": (),
        "declared_producers": (),
    }
    base.update(update)
    return ModelTopicEvidence.model_validate(base)


def _golden_topics() -> tuple[ModelTopicEvidence, ...]:
    # One topic of each outcome, deliberately not in name order.
    return (
        _topic("omnimarket.orders-capture-events", high_watermark_total=41),
        _topic("_schemas", partitions=1),
        _topic("omnimarket.abandoned-provision-topic"),
        _topic("omnimarket.retention-holds-topic", records_in_retention=7),
        _topic(
            "omnimarket.group-read-topic", consumer_groups=("omnimarket-orders-lane",)
        ),
        _topic("__consumer_offsets", partitions=50),
        _topic(
            "omnimarket.contract-declared-topic",
            declared_producers=("b-contract", "a-contract"),
        ),
        _topic("omnimarket.offsets-unread-topic", consumer_groups=None),
    )


def _report(**rule_over: object) -> ModelNeverProducedReport:
    rule = RULE.model_copy(update=rule_over)
    return HandlerTopicNeverProduced().handle(
        ModelNeverProducedRequest(rule=rule, topics=_golden_topics())
    )


def test_never_produced_golden_listing_names_every_outcome() -> None:
    report = _report()

    assert [entry.name for entry in report.listed] == [
        "omnimarket.abandoned-provision-topic"
    ]
    assert report.listed[0].evidence == NEVER_PRODUCED_EVIDENCE
    assert report.listed[0].partitions == 3
    assert [(entry.name, entry.reason) for entry in report.not_listed] == [
        ("__consumer_offsets", "protected"),
        ("_schemas", "protected"),
        ("omnimarket.contract-declared-topic", "declared_producer"),
        ("omnimarket.group-read-topic", "has_consumer_group"),
        ("omnimarket.offsets-unread-topic", "evidence_unread"),
        ("omnimarket.orders-capture-events", "produced"),
        ("omnimarket.retention-holds-topic", "data_in_retention"),
    ]
    assert report.topics_read == 8


def test_never_produced_a_declared_producer_is_listed_when_the_rule_allows_it() -> None:
    report = _report(list_declared_producers=True)

    listed = {entry.name: entry for entry in report.listed}
    assert "omnimarket.contract-declared-topic" in listed
    assert listed["omnimarket.contract-declared-topic"].evidence == (
        "no record was ever produced (high watermark total 0)",
        "no data inside retention",
        "no consumer group reads it",
        "contract(s) declaring a producer: a-contract, b-contract",
    )
    assert all(entry.reason != "declared_producer" for entry in report.not_listed)


def test_never_produced_a_listed_topic_stops_being_listed_when_one_group_reads_it() -> (
    None
):
    request = ModelNeverProducedRequest(
        rule=RULE,
        topics=(
            _topic("omnimarket.abandoned-provision-topic"),
            _topic("omnimarket.read-provision-topic", consumer_groups=("one-lane",)),
        ),
    )

    report = HandlerTopicNeverProduced().handle(request)

    assert [entry.name for entry in report.listed] == [
        "omnimarket.abandoned-provision-topic"
    ]
    assert [(entry.name, entry.reason) for entry in report.not_listed] == [
        ("omnimarket.read-provision-topic", "has_consumer_group")
    ]
    assert report.topics_read == 2


def test_never_produced_unread_evidence_never_lists_even_all_zero_elsewhere() -> None:
    # Everything else reads as never produced; the unread groups alone keep it off the listing.
    request = ModelNeverProducedRequest(
        rule=RULE,
        topics=(_topic("omnimarket.groups-unread-topic", consumer_groups=None),),
    )

    report = HandlerTopicNeverProduced().handle(request)

    assert report.listed == ()
    assert [(entry.name, entry.reason) for entry in report.not_listed] == [
        ("omnimarket.groups-unread-topic", "evidence_unread")
    ]


def test_never_produced_the_report_carries_no_delete_but_the_literal_zero() -> None:
    for report in (_report(), _report(list_declared_producers=True)):
        assert report.deletes_issued == 0

    delete_fields = sorted(
        name for name in ModelNeverProducedReport.model_fields if "delete" in name
    )
    assert delete_fields == ["deletes_issued"]
    assert (
        ModelNeverProducedReport.model_fields["deletes_issued"].annotation == Literal[0]
    )


def test_never_produced_rule_comes_only_from_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayNotFoundError):
        load_never_produced_rule()

    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"protected_prefixes": ["__", "_schemas"], "list_declared_producers": False},
    )

    rule = load_never_produced_rule()
    assert rule == ModelNeverProducedRule(
        protected_prefixes=("__", "_schemas"), list_declared_producers=False
    )


def _contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_topic_never_produced_compute/contract.yaml"
    )


def test_never_produced_the_contract_names_exactly_the_two_topics() -> None:
    contract = yaml.safe_load(_contract_path().read_text())

    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.topic-never-produced-list-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.topic-never-produced-listed.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.topic-never-produced-listed.v1"
    )
    assert contract["lifecycle"] == "experimental"


def test_never_produced_the_node_entry_point_equals_the_handler() -> None:
    request = ModelNeverProducedRequest(rule=RULE, topics=_golden_topics())

    assert NodeTopicNeverProducedCompute().handle(request) == (
        HandlerTopicNeverProduced().handle(request)
    )
