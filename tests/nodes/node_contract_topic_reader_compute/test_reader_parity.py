# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every reader of a contract's declared topics is held to the one reader (OMN-19998, broker plan M1).

Five readers each knew a different subset of what a contract declares: boot provisioning
(`_contract_provision_topics`), the removal node's declared-topic scan, the topic graph
validator, the ACL derive (omnimarket branch omn-19998-l1-acl-derive, lane l1-acl-6100; not on dev, so not
imported here) and the census in `test_rerun_request_topics_provisioned.py` (omnimarket branch
omn-23991-serve-command-topics, lane cmd-topics-6240). The topics of a contract in each shape below are
written out by hand; the one reader, then each other reader, must equal the hand-written set it is
documented to read. A reader that starts reading another shape, or stops reading one, fails here.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics

from omnimarket.handlers.contract_documents_loader import (
    load_contract_documents,
)
from omnimarket.models.model_contract_topics import (
    EnumContractTopicShape,
    ModelContractTopicReport,
    ModelContractTopicRequest,
    ModelContractTopicRule,
)
from omnimarket.nodes.node_contract_topic_reader_compute.handlers.handler_contract_topic_reader import (
    HandlerContractTopicReader,
)
from omnimarket.validators.contract_topic_graph import parse_contract

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
RULE = ModelContractTopicRule(
    topic_pattern=r"^onex\.(evt|cmd|intent|dlq|snapshot)\.[a-z0-9._-]+\.v\d+$"
)

# One topic per shape, written by hand. No reader is asked what a shape holds.
SUBSCRIBE = "onex.cmd.parity.fixture-subscribe.v1"
PUBLISH = "onex.evt.parity.fixture-publish.v1"
DLQ = "onex.dlq.parity.fixture-dlq.v1"
PROJECTION = "onex.snapshot.parity.fixture-projection.v1"
PROJECTION_NOT_BUS_BACKED = "onex.snapshot.parity.fixture-not-bus-backed.v1"
CONSUMED = "onex.cmd.parity.fixture-consumed.v1"
EXTERNALLY_PRODUCED = "onex.evt.parity.fixture-externally-produced.v1"
EXTERNALLY_CONSUMED = "onex.evt.parity.fixture-externally-consumed.v1"
COMMAND = "onex.cmd.parity.fixture-command.v1"
TERMINAL_SUCCESS = "onex.evt.parity.fixture-terminal-ok.v1"
TERMINAL_FAILURE = "onex.evt.parity.fixture-terminal-failed.v1"
HANDLER = "onex.cmd.parity.fixture-handler.v1"
TOPICS_MAP = "onex.evt.parity.fixture-topics-map.v1"
PUBLISHED = "onex.evt.parity.fixture-published.v1"

EXPECTED_BY_SHAPE: dict[EnumContractTopicShape, set[str]] = {
    EnumContractTopicShape.EVENT_BUS_SUBSCRIBE: {SUBSCRIBE},
    EnumContractTopicShape.EVENT_BUS_PUBLISH: {PUBLISH},
    EnumContractTopicShape.EVENT_BUS_DLQ: {DLQ},
    EnumContractTopicShape.PROJECTION_API: {PROJECTION},
    EnumContractTopicShape.CONSUMED_EVENTS: {CONSUMED},
    EnumContractTopicShape.EXTERNALLY_PRODUCED: {EXTERNALLY_PRODUCED},
    EnumContractTopicShape.EXTERNALLY_CONSUMED: {EXTERNALLY_CONSUMED},
    EnumContractTopicShape.COMMAND: {COMMAND},
    EnumContractTopicShape.TERMINAL: {TERMINAL_SUCCESS, TERMINAL_FAILURE},
    EnumContractTopicShape.HANDLER_ROUTING: {HANDLER},
    EnumContractTopicShape.TOPICS_MAP: {TOPICS_MAP},
    EnumContractTopicShape.PUBLISHED_EVENTS: {PUBLISHED},
}

# The one topic literal on dev that is not a versioned topic name (measured 2026-11-12, 1,139 contracts).
KNOWN_REJECTED_LITERALS: set[tuple[str, str]] = {
    ("projection_tenant_registry", "onex.tenant.events"),
}

# What each reader is documented to read, as the shapes whose hand-written topics it must return.
PROVISIONING_SHAPES = (
    EnumContractTopicShape.EVENT_BUS_SUBSCRIBE,
    EnumContractTopicShape.EVENT_BUS_PUBLISH,
    EnumContractTopicShape.EVENT_BUS_DLQ,
    EnumContractTopicShape.PROJECTION_API,
)


def _expected(shapes: tuple[EnumContractTopicShape, ...]) -> set[str]:
    return set().union(*(EXPECTED_BY_SHAPE[shape] for shape in shapes))


def _every_shape_contract() -> dict[str, Any]:
    """A contract with one topic in each of the twelve shapes, on the real contract shape."""
    return {
        "name": "node_parity_fixture_effect",
        "contract_name": "node_parity_fixture_effect",
        "node_name": "node_parity_fixture_effect",
        "node_type": "EFFECT_GENERIC",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "description": "parity fixture",
        "input_model": {"name": "ModelIn", "module": "fixture.models"},
        "output_model": {"name": "ModelOut", "module": "fixture.models"},
        "handler": {
            "module": "fixture.handlers",
            "class": "HandlerFixture",
            "input_model": "fixture.models.ModelIn",
        },
        "handler_routing": {
            "routing_strategy": "operation_match",
            "handlers": [
                {
                    "operation": "handle",
                    "handler": {"name": "HandlerFixture", "module": "fixture.handlers"},
                    "topic": HANDLER,
                }
            ],
        },
        "event_bus": {
            "subscribe_topics": [SUBSCRIBE],
            "publish_topics": [PUBLISH],
            "dlq_topics": [DLQ],
        },
        "projection_api": {
            "expose": True,
            "exposures": [
                {"topic": PROJECTION, "bus_backed": True},
                {"topic": PROJECTION_NOT_BUS_BACKED, "bus_backed": False},
            ],
        },
        "consumed_events": [{"topic": CONSUMED}],
        "externally_produced_topics": [
            {"topic": EXTERNALLY_PRODUCED, "producer": "fixture producer"}
        ],
        "externally_consumed_topics": [EXTERNALLY_CONSUMED],
        "runtime_dispatch": {
            "command_topic": COMMAND,
            "terminal_events": {
                "success": TERMINAL_SUCCESS,
                "failure": TERMINAL_FAILURE,
            },
        },
        "topics": {"fixture_topic": TOPICS_MAP},
        "published_events": [{"topic": PUBLISHED}],
    }


def _write(directory: Path, document: dict[str, Any]) -> Path:
    directory.mkdir(parents=True)
    path = directory / "contract.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _read(root: Path) -> ModelContractTopicReport:
    return HandlerContractTopicReader().handle(
        ModelContractTopicRequest(rule=RULE, documents=load_contract_documents(root))
    )


def test_the_one_reader_returns_the_hand_written_topic_of_every_shape(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "node_parity_fixture_effect", _every_shape_contract())

    report = _read(tmp_path)

    assert {
        EnumContractTopicShape(shape): set(topics)
        for shape, topics in report.topics_by_shape.items()
    } == EXPECTED_BY_SHAPE
    assert set(report.all_topics) == _expected(tuple(EXPECTED_BY_SHAPE))
    assert PROJECTION_NOT_BUS_BACKED not in report.all_topics


def test_boot_provisioning_returns_exactly_the_event_bus_dlq_and_projection_shapes(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / "node_parity_fixture_effect", _every_shape_contract())

    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors, manifest.errors
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))

    assert provisioned == _expected(PROVISIONING_SHAPES)
    # The topics provisioning does not read are the ones the one reader reads in other shapes: the gap
    # this seam keeps visible. A provisioning change that closes it makes this fail, and the expected
    # shapes above are then widened by hand with the change.
    unread = set(_read(tmp_path).all_topics) - provisioned
    assert unread == _expected(
        tuple(s for s in EXPECTED_BY_SHAPE if s not in PROVISIONING_SHAPES)
    )


def test_boot_provisioning_returns_nothing_for_a_projection_only_contract(
    tmp_path: Path,
) -> None:
    document = _every_shape_contract()
    for key in ("event_bus", "consumed_events", "published_events"):
        del document[key]
    path = _write(tmp_path / "node_parity_fixture_effect", document)

    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors, manifest.errors
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))
    reader = _read(tmp_path)

    assert provisioned == set()
    assert PROJECTION in reader.all_topics


def test_the_topic_graph_validator_reads_the_hand_written_union_of_its_shapes(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / "node_parity_fixture_effect", _every_shape_contract())

    node = parse_contract(path, "omnimarket")

    assert node is not None
    seen = {
        *node.publish_topics,
        *node.subscribe_topics,
        *node.externally_consumed,
        *(topic for topic, _ in node.externally_produced),
    }
    # It reads the event_bus lists, the consumed, published, terminal, command, externally declared and
    # `topics:` map shapes; not the DLQ list, the projection exposures or the handler routing topics.
    assert seen == _expected(
        (
            EnumContractTopicShape.EVENT_BUS_SUBSCRIBE,
            EnumContractTopicShape.EVENT_BUS_PUBLISH,
            EnumContractTopicShape.CONSUMED_EVENTS,
            EnumContractTopicShape.EXTERNALLY_PRODUCED,
            EnumContractTopicShape.EXTERNALLY_CONSUMED,
            EnumContractTopicShape.COMMAND,
            EnumContractTopicShape.TERMINAL,
            EnumContractTopicShape.PUBLISHED_EVENTS,
        )
    )


def test_a_topic_in_a_key_no_reader_knows_is_named_by_the_removal_scan_and_not_the_one_reader(
    tmp_path: Path,
) -> None:
    # The removal node's declared-topic scan takes every matching string anywhere in a contract; the one
    # reader takes the shapes it names. A topic under an unnamed key is the difference, and the test
    # that holds the two together reports it instead of letting a topic be deleted that nothing reads.
    from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
        ContractFileDeclaredTopics,
    )
    from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
        ModelContractSource,
    )

    unnamed = "onex.evt.parity.fixture-unnamed-key.v1"
    document = _every_shape_contract()
    document["some_future_section"] = {"nested": [unnamed]}
    _write(tmp_path / "repo" / "nodes" / "node_parity_fixture_effect", document)
    source = ModelContractSource(
        registry_root_env="PARITY_ROOT",
        repositories=("repo",),
        contract_glob="nodes/*/contract.yaml",
        excluded_parts=(),
        topic_pattern=RULE.topic_pattern,
        publish_key_fragments=("publish",),
        reference_globs=("no-such-directory/*.nothing",),
    )

    declared = set(
        ContractFileDeclaredTopics(source, {"PARITY_ROOT": str(tmp_path)})
        .declared()
        .keys()
    )
    reader = set(_read(tmp_path / "repo" / "nodes").all_topics)

    # The scan also names the exposure that is not bus backed: it does not apply the served rule, so it
    # keeps a topic the one reader (and provisioning) leave out. That direction never deletes a live topic.
    assert declared - reader == {unnamed, PROJECTION_NOT_BUS_BACKED}
    assert reader - declared == set()


def test_the_one_reader_changes_when_a_shape_is_dropped(tmp_path: Path) -> None:
    # Failing control: remove the consumed_events shape from the fixture and the hand-written set no
    # longer holds; the equality in the first test is what names it.
    document = copy.deepcopy(_every_shape_contract())
    del document["consumed_events"]
    _write(tmp_path / "node_parity_fixture_effect", document)

    report = _read(tmp_path)

    assert CONSUMED not in report.all_topics
    assert set(report.all_topics) != _expected(tuple(EXPECTED_BY_SHAPE))


def test_every_real_contract_is_read_and_provisioning_matches_its_shapes() -> None:
    # The real tree: no contract may be unreadable, and for every contract that provisioning reads, its
    # output must equal the one reader's event_bus, dlq and projection shapes. Fails when either side
    # changes which of these a real contract yields.
    report = HandlerContractTopicReader().handle(
        ModelContractTopicRequest(rule=RULE, documents=load_contract_documents(NODES))
    )
    assert report.unreadable == ()
    # A string in a topic position that is not a versioned topic name is rejected by the one reader and
    # still provisioned by boot provisioning (which reads the literal as written). Each is named here with
    # its contract: a new one is a decision to add, not a silent difference.
    assert {(r.contract, r.literal) for r in report.rejected} == KNOWN_REJECTED_LITERALS
    by_contract: dict[str, set[str]] = {}
    for use in report.uses:
        if use.shape in PROVISIONING_SHAPES:
            by_contract.setdefault(use.contract, set()).add(use.topic)
    for entry in report.rejected:
        if entry.shape in PROVISIONING_SHAPES:
            by_contract.setdefault(entry.contract, set()).add(entry.literal)

    paths = sorted(p for p in NODES.glob("*/contract.yaml"))
    manifest = discover_contracts_from_paths(paths)
    names = {
        document.name: document.path for document in load_contract_documents(NODES)
    }
    checked = 0
    for contract in manifest.contracts:
        provisioned = set(_contract_provision_topics(contract))
        read = by_contract.get(contract.name, set())
        if contract.event_bus is None:
            assert provisioned == set(), contract.name
            continue
        assert provisioned == read, (
            f"{contract.name} ({names.get(contract.name)}): provisioning minus reader "
            f"{sorted(provisioned - read)}, reader minus provisioning {sorted(read - provisioned)}"
        )
        checked += 1
    assert checked > 0
