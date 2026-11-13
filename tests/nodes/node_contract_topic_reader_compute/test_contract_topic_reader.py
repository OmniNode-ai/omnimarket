# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads every topic a node contract declares, in every shape (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.contract_documents_loader import (
    load_contract_documents,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.model_contract_topics import (
    ModelContractDocument,
    ModelContractTopicReport,
    ModelContractTopicRequest,
    ModelContractTopicRule,
)
from omnimarket.nodes.node_contract_topic_reader_compute import (
    NodeContractTopicReaderCompute,
)
from omnimarket.nodes.node_contract_topic_reader_compute.handlers.handler_contract_topic_reader import (
    HandlerContractTopicReader,
)
from omnimarket.nodes.node_contract_topic_reader_compute.overlay import (
    NODE_NAME,
    load_contract_topic_rule,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

RULE = ModelContractTopicRule(
    topic_pattern=r"^onex\.(evt|cmd|intent|dlq|snapshot)\.[a-z0-9._-]+\.v\d+$"
)

CONTRACT = {
    "event_bus": {
        "subscribe_topics": ["onex.evt.orders.created.v1"],
        "publish_topics": ["onex.evt.orders.updated.v1"],
        "dlq_topics": ["onex.dlq.orders.failed.v1"],
    },
    "consumed_events": [{"topic": "onex.evt.orders.created.v1"}],
    "externally_produced_topics": [{"topic": "onex.evt.orders.created.v1"}],
    "externally_consumed_topics": [{"topic": "onex.evt.orders.updated.v1"}],
    "published_events": ["onex.evt.orders.updated.v1"],
    "projection_api": {
        "expose": True,
        "exposures": [
            {"topic": "onex.snapshot.orders.v1", "bus_backed": True},
            {"topic": "onex.snapshot.orders-hidden.v1", "bus_backed": False},
        ],
    },
    "runtime_dispatch": {
        "command_topic": "onex.cmd.orders.create.v1",
        "terminal_events": {
            "success": "onex.evt.orders.completed.v1",
            "failure": "onex.evt.orders.failed.v1",
        },
    },
    "terminal_event": "onex.evt.orders.completed.v1",
    "handler_routing": {
        "handlers": [
            {"topic": "onex.evt.orders.created.v1"},
            {"topic": "onex.evt.orders.updated.v1"},
        ]
    },
    "topics": {
        "orders": "onex.evt.orders.created.v1",
        "orders_updated": "onex.evt.orders.updated.v1",
    },
}

EXPECTED_TOPICS_BY_SHAPE = {
    "event_bus_subscribe": ("onex.evt.orders.created.v1",),
    "event_bus_publish": ("onex.evt.orders.updated.v1",),
    "event_bus_dlq": ("onex.dlq.orders.failed.v1",),
    "projection_api": ("onex.snapshot.orders.v1",),
    "consumed_events": ("onex.evt.orders.created.v1",),
    "externally_produced": ("onex.evt.orders.created.v1",),
    "externally_consumed": ("onex.evt.orders.updated.v1",),
    "command": ("onex.cmd.orders.create.v1",),
    "terminal": ("onex.evt.orders.completed.v1", "onex.evt.orders.failed.v1"),
    "handler_routing": ("onex.evt.orders.created.v1", "onex.evt.orders.updated.v1"),
    "topics_map": ("onex.evt.orders.created.v1", "onex.evt.orders.updated.v1"),
    "published_events": ("onex.evt.orders.updated.v1",),
}

EXPECTED_ALL_TOPICS = tuple(
    sorted(
        {
            "onex.evt.orders.created.v1",
            "onex.evt.orders.updated.v1",
            "onex.dlq.orders.failed.v1",
            "onex.snapshot.orders.v1",
            "onex.cmd.orders.create.v1",
            "onex.evt.orders.completed.v1",
            "onex.evt.orders.failed.v1",
        }
    )
)


def _make_doc(
    name: str, document: dict[str, object] | None = None, read_error: str | None = None
) -> ModelContractDocument:
    return ModelContractDocument(
        name=name,
        path=f"/contracts/{name}.yaml",
        document=document,
        read_error=read_error,
    )


def _run_handler(
    documents: tuple[ModelContractDocument, ...], rule: ModelContractTopicRule = RULE
) -> ModelContractTopicReport:
    return HandlerContractTopicReader().handle(
        ModelContractTopicRequest(rule=rule, documents=documents)
    )


def test_contract_topic_reader_full_contract() -> None:
    doc = _make_doc("full-contract", CONTRACT)
    report = _run_handler((doc,))

    assert report.topics_by_shape == EXPECTED_TOPICS_BY_SHAPE
    assert report.all_topics == EXPECTED_ALL_TOPICS


def test_contract_topic_reader_projection_only() -> None:
    doc = _make_doc(
        "projection-only",
        {
            "projection_api": {
                "expose": True,
                "exposures": [
                    {"topic": "onex.snapshot.orders.v1", "bus_backed": True},
                ],
            }
        },
    )
    report = _run_handler((doc,))

    assert "onex.snapshot.orders.v1" in report.all_topics
    assert report.topics_by_shape.get("projection_api") == ("onex.snapshot.orders.v1",)


def test_contract_topic_reader_projection_expose_false_yields_nothing() -> None:
    doc = _make_doc(
        "projection-expose-false",
        {
            "projection_api": {
                "expose": False,
                "exposures": [
                    {"topic": "onex.snapshot.orders.v1", "bus_backed": True},
                ],
            }
        },
    )
    report = _run_handler((doc,))

    assert report.all_topics == ()
    assert report.topics_by_shape == {}


def test_contract_topic_reader_not_bus_backed_yields_nothing() -> None:
    doc = _make_doc(
        "projection-not-bus-backed",
        {
            "projection_api": {
                "expose": True,
                "exposures": [
                    {"topic": "onex.snapshot.orders.v1", "bus_backed": False},
                ],
            }
        },
    )
    report = _run_handler((doc,))

    assert report.all_topics == ()
    assert report.topics_by_shape == {}


def test_contract_topic_reader_error_chain() -> None:
    docs = (
        _make_doc("non-mapping", None),
        _make_doc("read-error", None, read_error="yaml parse error"),
        _make_doc("rejected", {"event_bus": {"subscribe_topics": ["invalid-topic"]}}),
    )
    report = _run_handler(docs)

    assert len(report.unreadable) == 2
    assert {u.contract for u in report.unreadable} == {"non-mapping", "read-error"}
    assert len(report.rejected) == 1
    assert report.rejected[0].contract == "rejected"
    assert report.rejected[0].literal == "invalid-topic"
    assert report.all_topics == ()


def test_contract_topic_reader_invalid_pattern_raises_value_error() -> None:
    docs = (_make_doc("any", {}),)
    bad_rule = ModelContractTopicRule(topic_pattern="^onex\\.(evt|cmd")
    with pytest.raises(ValueError, match="is not a valid regex"):
        _run_handler(docs, rule=bad_rule)


def test_contract_topic_reader_overlay_install_and_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"topic_pattern": r"^onex\.(evt|cmd|intent|dlq|snapshot)\.[a-z0-9._-]+\.v\d+$"},
    )
    rule = load_contract_topic_rule()
    assert (
        rule.topic_pattern
        == r"^onex\.(evt|cmd|intent|dlq|snapshot)\.[a-z0-9._-]+\.v\d+$"
    )


def test_contract_topic_reader_missing_overlay_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "some-other-node", {"topic_pattern": ".*"}
    )
    with pytest.raises(NodeOverlayNotFoundError):
        load_contract_topic_rule()


def test_contract_topic_reader_adapter_load_documents(tmp_path: Path) -> None:
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "contract.yaml").write_text(
        yaml.safe_dump({"name": "good", **CONTRACT}), encoding="utf-8"
    )
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "contract.yaml").write_text(
        "not: valid: yaml: [", encoding="utf-8"
    )

    docs = load_contract_documents(tmp_path)
    assert len(docs) == 2

    good_doc = next(d for d in docs if d.name == "good")
    bad_doc = next(d for d in docs if d.name == "bad")

    assert good_doc.document is not None
    assert good_doc.read_error is None
    assert bad_doc.document is None
    assert bad_doc.read_error is not None


def test_contract_topic_reader_determinism() -> None:
    doc_a = _make_doc("a", CONTRACT)
    doc_b = _make_doc("b", CONTRACT)

    report1 = _run_handler((doc_a, doc_b))
    report2 = _run_handler((doc_b, doc_a))

    assert report1 == report2


def test_contract_topic_reader_the_contract_names_exactly_the_two_topics() -> None:
    contract_path = NodeContractTopicReaderCompute.contract_path
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.contract-topic-read-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.contract-topic-read-completed.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.contract-topic-read-completed.v1"
    )
    assert contract["lifecycle"] == "experimental"


def test_contract_topic_reader_the_node_entry_point_equals_the_handler() -> None:
    request = ModelContractTopicRequest(
        rule=RULE,
        documents=(_make_doc("a", CONTRACT),),
    )

    assert NodeContractTopicReaderCompute().handle(
        request
    ) == HandlerContractTopicReader().handle(request)
