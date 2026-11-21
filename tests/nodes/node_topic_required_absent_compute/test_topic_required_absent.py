# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the node_topic_required_absent_compute node and its required-and-absent reading (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.model_contract_topics import (
    ModelContractDocument,
    ModelContractTopicRequest,
    ModelContractTopicRule,
)
from omnimarket.models.model_topic_required_absent import (
    EnumRequiredAbsentVerdict,
    ModelRequiredAbsentRequest,
    ModelRequiredAbsentRule,
    ModelRequiredTopic,
)
from omnimarket.nodes.node_contract_topic_reader_compute.handlers.handler_contract_topic_reader import (
    HandlerContractTopicReader,
)
from omnimarket.nodes.node_topic_required_absent_compute import (
    NodeTopicRequiredAbsentCompute,
)
from omnimarket.nodes.node_topic_required_absent_compute.handlers.handler_topic_required_absent import (
    HandlerTopicRequiredAbsent,
)
from omnimarket.nodes.node_topic_required_absent_compute.overlay import (
    NODE_NAME,
    load_required_absent_rule,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit


def test_golden_all_required_topics_live() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
        ModelRequiredTopic(
            topic="onex.cmd.orders.cancel.v1", required_by="orders-service"
        ),
    )
    live = (
        "onex.evt.orders.created.v1",
        "onex.cmd.orders.cancel.v1",
        "onex.evt.other.v1",
    )
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=live)
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.WORKING
    assert report.reason == "every required topic exists on the broker"
    assert report.required_count == 2
    assert report.present_count == 2
    assert report.absent == ()


def test_required_topic_deleted_from_live_listing() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="audit-service"
        ),
        ModelRequiredTopic(
            topic="onex.cmd.orders.cancel.v1", required_by="orders-service"
        ),
    )
    live = ("onex.cmd.orders.cancel.v1",)
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=live)
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.NOT_WORKING
    assert report.reason == "1 required topics are absent from the broker"
    assert report.required_count == 2
    assert report.present_count == 1
    assert len(report.absent) == 1
    assert report.absent[0].topic == "onex.evt.orders.created.v1"
    assert report.absent[0].required_by == ("audit-service", "orders-service")


def test_restore_topic_returns_working() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
        ModelRequiredTopic(
            topic="onex.cmd.orders.cancel.v1", required_by="orders-service"
        ),
    )
    live = ("onex.evt.orders.created.v1", "onex.cmd.orders.cancel.v1")
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=live)
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.WORKING


def test_live_topics_none_is_unknown() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
    )
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=None)
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.UNKNOWN
    assert report.reason == "the broker topic list was not read"
    assert report.absent == ()
    assert report.present_count == 0
    assert report.required_count == 1


def test_live_listing_below_min_live_topics_is_unknown() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=5)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
    )
    live = ("onex.evt.orders.created.v1",)
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=live)
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.UNKNOWN
    assert (
        report.reason
        == "the broker topic list holds 1 topics, fewer than the minimum 5: a failed read, not an empty broker"
    )
    assert report.absent == ()


def test_two_contracts_requiring_one_absent_topic() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="audit-service"
        ),
    )
    live = ("onex.evt.orders.shipped.v1",)
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=live)
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.NOT_WORKING
    assert len(report.absent) == 1
    assert report.absent[0].topic == "onex.evt.orders.created.v1"
    assert report.absent[0].required_by == ("audit-service", "orders-service")


def test_determinism_reversed_input_equal_report() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required_a = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
        ModelRequiredTopic(
            topic="onex.cmd.orders.cancel.v1", required_by="audit-service"
        ),
    )
    required_b = (
        ModelRequiredTopic(
            topic="onex.cmd.orders.cancel.v1", required_by="audit-service"
        ),
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
    )
    live = ("onex.evt.orders.created.v1",)
    request_a = ModelRequiredAbsentRequest(
        rule=rule, required=required_a, live_topics=live
    )
    request_b = ModelRequiredAbsentRequest(
        rule=rule, required=required_b, live_topics=live
    )
    report_a = HandlerTopicRequiredAbsent().handle(request_a)
    report_b = HandlerTopicRequiredAbsent().handle(request_b)
    assert report_a == report_b


def test_overlay_install_and_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"min_live_topics": 5},
    )
    rule = load_required_absent_rule()
    assert rule.min_live_topics == 5


def test_overlay_missing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "some-other-node", {"min_live_topics": 5}
    )
    with pytest.raises(NodeOverlayNotFoundError):
        load_required_absent_rule()


def test_node_entry_point_equals_handler() -> None:
    rule = ModelRequiredAbsentRule(min_live_topics=1)
    required = (
        ModelRequiredTopic(
            topic="onex.evt.orders.created.v1", required_by="orders-service"
        ),
    )
    live = ("onex.evt.orders.created.v1",)
    request = ModelRequiredAbsentRequest(rule=rule, required=required, live_topics=live)
    node_report = NodeTopicRequiredAbsentCompute().handle(request)
    handler_report = HandlerTopicRequiredAbsent().handle(request)
    assert node_report == handler_report


def test_contract_topics_and_lifecycle() -> None:
    contract_text = NodeTopicRequiredAbsentCompute.contract_path.read_text()
    contract = yaml.safe_load(contract_text)
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.topic-required-absent-read-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.topic-required-absent-read-completed.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.topic-required-absent-read-completed.v1"
    )
    assert contract["lifecycle"] == "experimental"


def test_seam_with_reader() -> None:
    rule = ModelContractTopicRule(
        topic_pattern=r"^onex\.(evt|cmd)\.[a-z0-9._-]+\.v\d+$"
    )
    contract_doc = ModelContractDocument(
        name="fixture",
        path="fixture/contract.yaml",
        document={
            "event_bus": {
                "subscribe_topics": [],
                "publish_topics": ["onex.evt.orders.created.v1"],
                "dlq_topics": [],
            },
            "runtime_dispatch": {"command_topic": "onex.cmd.orders.cancel.v1"},
        },
    )
    reader_request = ModelContractTopicRequest(
        rule=rule,
        documents=(contract_doc,),
    )
    reader_report = HandlerContractTopicReader().handle(reader_request)

    required_topics = []
    for use in reader_report.uses:
        required_topics.append(
            ModelRequiredTopic(
                topic=use.topic,
                required_by=use.contract,
            )
        )

    rule_required_absent = ModelRequiredAbsentRule(min_live_topics=1)
    live = ("onex.evt.orders.created.v1",)
    request = ModelRequiredAbsentRequest(
        rule=rule_required_absent,
        required=tuple(required_topics),
        live_topics=live,
    )
    report = HandlerTopicRequiredAbsent().handle(request)
    assert report.verdict == EnumRequiredAbsentVerdict.NOT_WORKING
    assert len(report.absent) == 1
    assert report.absent[0].topic == "onex.cmd.orders.cancel.v1"
    assert report.absent[0].required_by == ("fixture",)
