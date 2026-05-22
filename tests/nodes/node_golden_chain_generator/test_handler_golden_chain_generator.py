# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for node_golden_chain_generator."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_generator.handlers.handler_golden_chain_generator import (
    HandlerGoldenChainGenerator,
)
from omnimarket.nodes.node_golden_chain_generator.models.model_generation_request import (
    ModelGoldenChainGenerationRequest,
)
from omnimarket.nodes.node_golden_chain_generator.models.model_generation_result import (
    EnumGenerationStatus,
)

_CONTRACT_YAML = """\
name: node_chain_diff
node_type: compute
event_bus:
  subscribe_topics:
    - onex.cmd.omnimarket.chain-diff-requested.v1
  publish_topics:
    - onex.evt.omnimarket.chain-diff-completed.v1
"""

_CONTRACT_HASH = "abc123"
_GENERATOR_VERSION = "1.0.0"


def _make_request(**kwargs: object) -> ModelGoldenChainGenerationRequest:
    defaults: dict[str, object] = {
        "contract_yaml": _CONTRACT_YAML,
        "contract_hash": _CONTRACT_HASH,
        "generator_version": _GENERATOR_VERSION,
    }
    defaults.update(kwargs)
    return ModelGoldenChainGenerationRequest(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestDeterminism:
    def test_same_input_produces_same_chain_hash(self) -> None:
        handler = HandlerGoldenChainGenerator()
        req = _make_request()
        r1 = handler.handle(req)
        r2 = handler.handle(req)
        assert r1.chain_hash == r2.chain_hash

    def test_different_contract_produces_different_hash(self) -> None:
        handler = HandlerGoldenChainGenerator()
        r1 = handler.handle(_make_request(contract_yaml=_CONTRACT_YAML))
        r2 = handler.handle(
            _make_request(
                contract_yaml=_CONTRACT_YAML.replace(
                    "chain-diff-requested", "other-event-requested"
                )
            )
        )
        assert r1.chain_hash != r2.chain_hash


@pytest.mark.unit
class TestContractTopologyDrivesChain:
    def test_subscribe_topic_appears_as_command_entry(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        topics = [e.topic for e in result.expected_chain]
        assert "onex.cmd.omnimarket.chain-diff-requested.v1" in topics

    def test_publish_topic_appears_as_event_entry(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        topics = [e.topic for e in result.expected_chain]
        assert "onex.evt.omnimarket.chain-diff-completed.v1" in topics

    def test_publish_entry_source_node_is_contract_name(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        publish_entries = [
            e for e in result.expected_chain if e.topic.startswith("onex.evt.")
        ]
        assert any(e.source_node == "node_chain_diff" for e in publish_entries)

    def test_status_is_ok(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        assert result.status == EnumGenerationStatus.OK

    def test_contract_hash_propagated(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        assert result.contract_hash == _CONTRACT_HASH

    def test_generator_version_propagated(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        assert result.generator_version == _GENERATOR_VERSION

    def test_known_contract_produces_two_entries(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        assert len(result.expected_chain) == 2

    def test_sequence_is_ordered(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        seqs = [e.sequence for e in result.expected_chain]
        assert seqs == sorted(seqs)


@pytest.mark.unit
class TestASTSupplement:
    def test_ast_topic_in_tests_added_as_unknown(self) -> None:
        test_source = """\
def test_thing():
    topic = "onex.evt.omnimarket.some-extra-event.v1"
    assert topic
"""
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(test_source=test_source))
        topics = [e.topic for e in result.expected_chain]
        assert "onex.evt.omnimarket.some-extra-event.v1" in topics

    def test_ast_topic_already_in_contract_not_duplicated(self) -> None:
        test_source = """\
def test_thing():
    topic = "onex.cmd.omnimarket.chain-diff-requested.v1"
    assert topic
"""
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(test_source=test_source))
        topic_count = sum(
            1
            for e in result.expected_chain
            if e.topic == "onex.cmd.omnimarket.chain-diff-requested.v1"
        )
        assert topic_count == 1

    def test_dynamic_fstring_not_extracted(self) -> None:
        test_source = """\
def test_thing():
    suffix = "chain-diff-requested"
    topic = f"onex.cmd.omnimarket.{suffix}.v1"
    assert topic
"""
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(test_source=test_source))
        observed = {e.topic for e in result.expected_chain}
        expected = {
            "onex.cmd.omnimarket.chain-diff-requested.v1",
            "onex.evt.omnimarket.chain-diff-completed.v1",
        }
        # f-string fragments are not valid topic literals
        assert observed == expected

    def test_invalid_test_source_syntax_does_not_crash(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(test_source="def broken(:"))
        assert result.status == EnumGenerationStatus.OK

    def test_empty_test_source_uses_contract_only(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(test_source=""))
        assert len(result.expected_chain) == 2


@pytest.mark.unit
class TestEmptyOrMissingTopics:
    def test_no_event_bus_produces_empty_chain(self) -> None:
        contract = "name: node_bare\nnode_type: compute\n"
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(contract_yaml=contract, test_source=""))
        assert result.expected_chain == ()
        assert result.status == EnumGenerationStatus.OK

    def test_invalid_yaml_produces_empty_chain_not_crash(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request(contract_yaml=": invalid: {{yaml"))
        assert result.status == EnumGenerationStatus.OK

    def test_chain_hash_is_non_empty(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        assert len(result.chain_hash) == 64


@pytest.mark.unit
class TestOutputSerializable:
    def test_result_serializes_to_json(self) -> None:
        import json

        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        dumped = result.model_dump(mode="json")
        # Ensure the expected_chain is serializable as a list of dicts
        json_str = json.dumps(dumped)
        assert '"topic"' in json_str

    def test_expected_chain_entries_have_required_fields(self) -> None:
        handler = HandlerGoldenChainGenerator()
        result = handler.handle(_make_request())
        for entry in result.expected_chain:
            assert isinstance(entry.sequence, int)
            assert isinstance(entry.event_type, str)
            assert isinstance(entry.topic, str)
            assert isinstance(entry.source_node, str)
