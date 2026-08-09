# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for omnimarket.seams.extraction (OMN-15763).

Covers the CodeRabbit-flagged correctness gaps on the code-level extractor:
comments/docstrings never produce a false observation, multiline calls
still resolve, an attribute-chain call site (``self.producer.send(...)``)
is still detected, and a discovery root cannot escape the pinned tree via
an absolute path, ``..`` traversal, or symlink.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.seams.extraction import _resolve_confined_root, extract_seam_graph
from omnimarket.seams.models.model_seam_graph import EnumSeamGraphObservationKind


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.unit
class TestCommentsAndDocstringsNeverMatch:
    def test_producer_send_in_comment_is_not_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            '# producer.send("commented-topic")\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {o.value for o in graph.code_observations}
        assert "commented-topic" not in values

    def test_producer_send_in_docstring_is_not_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            '"""producer.send("docstring-topic")"""\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {o.value for o in graph.code_observations}
        assert "docstring-topic" not in values

    def test_consumer_subscribe_in_docstring_is_not_observed(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/consumer.py",
            "class Example:\n"
            '    """consumer.subscribe(["docstring-subscription"])"""\n'
            "    pass\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {o.value for o in graph.code_observations}
        assert "docstring-subscription" not in values


@pytest.mark.unit
class TestMultilineAndAttributeChainCalls:
    def test_multiline_producer_send_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            'producer.send(\n    "multiline-topic",\n    payload,\n)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "multiline-topic" in values

    def test_multiline_consumer_subscribe_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/consumer.py",
            "def run():\n"
            "    consumer.subscribe(\n"
            '        ["multiline-subscription"],\n'
            "    )\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert "multiline-subscription" in values

    def test_attribute_chain_producer_send_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            "class Service:\n"
            "    def emit(self):\n"
            '        self.producer.send("chained-topic", payload)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "chained-topic" in values


@pytest.mark.unit
class TestRefPinIsCommentScoped:
    def test_ref_pin_in_string_literal_is_not_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            'NOT_A_REF = "@ref: configs/should-not-match.yaml#x"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/should-not-match.yaml#x" not in values

    def test_ref_pin_in_real_comment_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            "# @ref: configs/real.yaml#backends.x\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/real.yaml#backends.x" in values


@pytest.mark.unit
class TestRealCorpusIdiomsNoLongerMissed:
    """Regression coverage for the adversarial-verify finding: the shipped
    extractor required the receiver's trailing identifier to be exactly
    ``producer``/``consumer``, missing the live idioms found in a real
    corpus scan (omnibase_infra's ``consumer_health_emitter.py`` etc).
    These reproduce the exact cited false negatives."""

    def test_underscore_prefixed_producer_attribute_is_observed(
        self, tmp_path: Path
    ) -> None:
        # self._producer.send(...) — the real omnibase_infra idiom
        # (consumer_health_emitter.py), base name "_producer" != "producer".
        _write(
            tmp_path,
            "svc/emitter.py",
            "class Emitter:\n"
            "    def emit(self):\n"
            '        self._producer.send("underscore-producer-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "underscore-producer-topic" in values

    def test_kafka_producer_attribute_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/emitter.py",
            "class Emitter:\n"
            "    def emit(self):\n"
            '        self.kafka_producer.send("kafka-producer-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "kafka-producer-topic" in values

    def test_underscore_prefixed_consumer_attribute_is_observed(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/consumer.py",
            "class Service:\n"
            "    def run(self):\n"
            '        self._consumer.subscribe(["underscore-consumer-topic"])\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert "underscore-consumer-topic" in values

    def test_event_bus_publish_is_observed(self, tmp_path: Path) -> None:
        # event_bus.publish(...) — the canonical thin-publisher path
        # (feedback_bus_is_the_transport) — the shipped extractor only
        # recognized ``.send``/``.subscribe``, never ``.publish``.
        _write(
            tmp_path,
            "svc/publisher.py",
            "class Service:\n"
            "    def emit(self):\n"
            '        self.event_bus.publish("event-bus-topic", payload)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "event-bus-topic" in values


@pytest.mark.unit
class TestYamlRefPinsAreObserved:
    """Regression coverage: real @ref pins live in YAML string values, not
    Python comments — the shipped extractor's tokenize-COMMENT restriction
    (correct for .py files) never scanned .yaml/.yml files at all."""

    def test_ref_pin_in_yaml_string_value_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/configs/routing.yaml",
            'endpoint_ref: "@ref:configs/service_endpoints.yaml#backends.cloud-x"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/service_endpoints.yaml#backends.cloud-x" in values

    def test_ref_pin_in_yaml_comment_is_also_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/configs/routing.yaml",
            "# @ref: configs/other.yaml#backends.y\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/other.yaml#backends.y" in values


@pytest.mark.unit
class TestContractPathCorrelation:
    """A producer-side declaration only knows its own contract path at
    extraction time; the consumer-side counterpart path is filled by
    post-extraction correlation once both sides of the same edge_id are
    discovered in the same scan."""

    def test_producer_and_consumer_paths_correlate_across_two_contracts(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc_producer/contracts/contract.yaml",
            "name: svc_producer\n"
            "seams:\n"
            "  - id: SX\n"
            "    seam: cross-service edge\n"
            "    role: producer\n"
            "    topic: t\n"
            "    envelope_model: m\n"
            "    envelope_version: '1.0.0'\n",
        )
        _write(
            tmp_path,
            "svc_consumer/contracts/contract.yaml",
            "name: svc_consumer\n"
            "seams:\n"
            "  - id: SX\n"
            "    seam: cross-service edge\n"
            "    role: consumer\n"
            "    topic: t\n"
            "    envelope_model: m\n"
            "    envelope_version: '1.0.0'\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc_producer", "svc_consumer"))
        edges = {e.role: e for e in graph.edges if e.edge_id == "SX"}
        assert edges["producer"].producer_contract_path == (
            "svc_producer/contracts/contract.yaml"
        )
        assert edges["producer"].consumer_contract_path == (
            "svc_consumer/contracts/contract.yaml"
        )
        assert edges["consumer"].producer_contract_path == (
            "svc_producer/contracts/contract.yaml"
        )
        assert edges["consumer"].consumer_contract_path == (
            "svc_consumer/contracts/contract.yaml"
        )

    def test_uncorrelated_edge_leaves_counterpart_path_none(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc_producer/contracts/contract.yaml",
            "name: svc_producer\n"
            "seams:\n"
            "  - id: SY\n"
            "    seam: no consumer declared yet\n"
            "    role: producer\n"
            "    topic: t\n"
            "    envelope_model: m\n"
            "    envelope_version: '1.0.0'\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc_producer",))
        edge = next(e for e in graph.edges if e.edge_id == "SY")
        assert edge.producer_contract_path == "svc_producer/contracts/contract.yaml"
        assert edge.consumer_contract_path is None


@pytest.mark.unit
class TestExpandedSeamsBlockFields:
    """key_fields / delivery_semantics / fsm_state_transitions are optional
    seams: entry fields (2026-08-08 addendum reconciliation)."""

    def test_key_fields_and_delivery_semantics_and_fsm_states_are_parsed(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "seams:\n"
            "  - id: SZ\n"
            "    seam: full-schema edge\n"
            "    role: producer\n"
            "    topic: t\n"
            "    envelope_model: m\n"
            "    envelope_version: '1.0.0'\n"
            "    key_fields:\n"
            "      - name: tenant_id\n"
            "        field_type: str\n"
            "      - name: request_id\n"
            "        field_type: uuid.UUID\n"
            "    delivery_semantics: at_least_once\n"
            "    fsm_state_transitions:\n"
            "      - PENDING\n"
            "      - ROUTED\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(e for e in graph.edges if e.edge_id == "SZ")
        assert [f.name for f in edge.key_fields] == ["tenant_id", "request_id"]
        assert [f.field_type for f in edge.key_fields] == ["str", "uuid.UUID"]
        assert edge.delivery_semantics.value == "at_least_once"
        assert edge.fsm_state_transitions == ("PENDING", "ROUTED")

    def test_absent_optional_fields_default_honestly(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "seams:\n"
            "  - id: SW\n"
            "    seam: minimal edge\n"
            "    role: producer\n"
            "    topic: t\n"
            "    envelope_model: m\n"
            "    envelope_version: '1.0.0'\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(e for e in graph.edges if e.edge_id == "SW")
        assert edge.key_fields == ()
        assert edge.delivery_semantics.value == "unknown"
        assert edge.fsm_state_transitions == ()


@pytest.mark.unit
class TestMalformedSeamDeclarationsSkippedNotFatal:
    def test_wrong_typed_field_is_skipped_valid_edges_still_extracted(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "seams:\n"
            "  - id: BAD\n"
            "    seam: null\n"  # wrong type: seam must be a string
            "    role: producer\n"
            "    topic: t\n"
            "    envelope_model: m\n"
            "    envelope_version: '1.0.0'\n"
            "  - id: GOOD\n"
            "    seam: a real seam\n"
            "    role: producer\n"
            "    topic: t2\n"
            "    envelope_model: m2\n"
            "    envelope_version: '1.0.0'\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge_ids = {e.edge_id for e in graph.edges}
        assert "GOOD" in edge_ids
        assert "BAD" not in edge_ids

    def test_missing_required_key_is_skipped(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\nseams:\n  - id: INCOMPLETE\n    seam: x\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        assert graph.edges == ()


@pytest.mark.unit
class TestDiscoveryRootConfinement:
    def test_absolute_root_is_rejected(self, tmp_path: Path) -> None:
        assert _resolve_confined_root(tmp_path, "/etc") is None

    def test_parent_traversal_root_is_rejected(self, tmp_path: Path) -> None:
        assert _resolve_confined_root(tmp_path, "../outside") is None

    def test_nested_parent_traversal_root_is_rejected(self, tmp_path: Path) -> None:
        assert _resolve_confined_root(tmp_path, "src/../../outside") is None

    def test_normal_child_root_is_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        result = _resolve_confined_root(tmp_path, "src")
        assert result is not None
        assert result == (tmp_path / "src").resolve()

    def test_extract_seam_graph_ignores_traversal_root_without_crashing(
        self, tmp_path: Path
    ) -> None:
        # A malicious/mistaken absolute root must not crash extraction —
        # it is simply excluded, other valid roots still scan.
        _write(tmp_path, "svc/publisher.py", 'producer.send("in-tree-topic", p)\n')
        graph = extract_seam_graph(str(tmp_path), ("/etc", "svc"))
        values = {o.value for o in graph.code_observations}
        assert "in-tree-topic" in values
