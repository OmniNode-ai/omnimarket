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
from omnimarket.seams.models.model_seam_graph import (
    EnumSeamGraphObservationKind,
    ModelSeamGraphV1,
)


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

    def test_bus_subscribe_is_observed(self, tmp_path: Path) -> None:
        # <bus-like>.subscribe(...) -- OMN-15779 corpus finding: the
        # dominant real-corpus subscribe receiver name is a bus reference
        # ("event_bus", "self._bus", "bus", "typed_bus" --
        # omnibase_infra/src/omnibase_infra/event_bus/event_bus_kafka.py:168
        # et al.), never a "consumer"-suffixed name. The pre-existing
        # classifier only recognized a consumer-suffixed receiver for
        # ``.subscribe()``, so this idiom never matched at all (0 real
        # consumer_subscribe observations on the corpus even before symbol
        # resolution was in play).
        _write(
            tmp_path,
            "svc/consumer.py",
            "class Service:\n"
            "    async def run(self):\n"
            '        await self._event_bus.subscribe("bus-subscribe-topic", identity, handler)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert "bus-subscribe-topic" in values

    def test_bare_bus_subscribe_is_observed(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/consumer.py",
            "async def run(bus):\n"
            '    await bus.subscribe("bare-bus-topic", identity, handler)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert "bare-bus-topic" in values


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
class TestEventBusDeclaredTopicsAreExtracted:
    """OMN-15763 AC1 fix-forward: the shipped extractor's contract-declared
    leg only ever read a hand-authored ``seams:`` block that no real
    contract carries (0/0/0 finding — beta-triage revert comment
    ``2ef90b79``, 2026-08-10). Real contracts declare topics under
    ``event_bus.publish_topics`` / ``event_bus.subscribe_topics`` (435+
    contracts across omnibase_infra/omnimarket/omnibase_core, e.g.
    ``omnibase_infra/src/omnibase_infra/verification/contract.yaml``) — this
    is the actual, non-fabricated source-of-truth schema per this repo's own
    CLAUDE.md ("Keep event topics declared in contract.yaml"). These tests
    reproduce that real schema shape directly (no ``seams:`` block present)
    and assert it now yields edges."""

    def test_publish_topics_yield_producer_edges(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: verification_event_emitter\n"
            "node_type: COMPUTE_GENERIC\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            "  publish_topics:\n"
            '    - "onex.evt.platform.contract-verification-result.v1"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(
            e
            for e in graph.edges
            if e.topic == "onex.evt.platform.contract-verification-result.v1"
        )
        assert edge.role == "producer"
        assert edge.source_contract_path == "svc/contracts/contract.yaml"
        assert edge.producer_contract_path == "svc/contracts/contract.yaml"
        assert edge.consumer_contract_path is None
        # ONEX topic names are versioned by convention
        # (``onex.<kind>.<domain>.<name>.vN``) — the trailing ``.v1`` is read
        # straight off the topic string itself, not fabricated (OMN-15843).
        # No ``published_events:`` block present anywhere in this fixture,
        # so envelope_model stays honestly None (no producer-declared wire
        # type to correlate against).
        assert edge.envelope_model is None
        assert edge.envelope_version == "v1"

    def test_subscribe_topics_yield_consumer_edges(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: node_polish_task_classifier\n"
            "node_type: compute\n"
            "event_bus:\n"
            "  subscribe_topics:\n"
            "    - onex.cmd.omnimarket.polish-task-classifier.v1\n"
            "  publish_topics: []\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(
            e
            for e in graph.edges
            if e.topic == "onex.cmd.omnimarket.polish-task-classifier.v1"
        )
        assert edge.role == "consumer"
        assert edge.consumer_contract_path == "svc/contracts/contract.yaml"
        assert edge.producer_contract_path is None

    def test_producer_and_consumer_contracts_correlate_via_shared_topic(
        self, tmp_path: Path
    ) -> None:
        shared_topic = "onex.evt.omnimarket.polish-task-classifier-completed.v1"
        _write(
            tmp_path,
            "svc_producer/contracts/contract.yaml",
            "name: svc_producer\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            f"  publish_topics:\n    - {shared_topic}\n",
        )
        _write(
            tmp_path,
            "svc_consumer/contracts/contract.yaml",
            "name: svc_consumer\n"
            "event_bus:\n"
            f"  subscribe_topics:\n    - {shared_topic}\n"
            "  publish_topics: []\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc_producer", "svc_consumer"))
        edges = {e.role: e for e in graph.edges if e.topic == shared_topic}
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

    def test_missing_event_bus_block_yields_no_event_bus_edges(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "svc/contracts/contract.yaml", "name: no_event_bus\n")
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        assert graph.edges == ()

    def test_empty_topic_lists_yield_no_edges(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: empty_lists\nevent_bus:\n  subscribe_topics: []\n  publish_topics: []\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        assert graph.edges == ()

    def test_non_string_topic_entries_are_skipped_not_fabricated(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: malformed\nevent_bus:\n  publish_topics: [1, null, '']\n  subscribe_topics: []\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        assert graph.edges == ()

    def test_full_three_repo_corpus_shape_yields_nonzero_declared_edges(
        self, tmp_path: Path
    ) -> None:
        """Reproduces the exact live corpus finding cited in the revert: a
        realistic scan over infra-style + market-style contract.yaml files
        (event_bus topics, no seams: block anywhere) previously yielded
        zero declared edges. It must now yield a nonzero, deterministic
        count."""
        _write(
            tmp_path,
            "omnibase_infra/verification/contract.yaml",
            "name: verification_event_emitter\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            '  publish_topics:\n    - "onex.evt.platform.contract-verification-result.v1"\n',
        )
        _write(
            tmp_path,
            "omnimarket/node_polish_task_classifier/contract.yaml",
            "name: node_polish_task_classifier\n"
            "event_bus:\n"
            "  subscribe_topics:\n    - onex.cmd.omnimarket.polish-task-classifier.v1\n"
            "  publish_topics:\n"
            "    - onex.evt.omnimarket.polish-task-classifier-completed.v1\n"
            "    - onex.evt.omnimarket.polish-task-classifier-failed.v1\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("omnibase_infra", "omnimarket"))
        assert len(graph.edges) == 4
        first = extract_seam_graph(str(tmp_path), ("omnibase_infra", "omnimarket"))
        second = extract_seam_graph(str(tmp_path), ("omnibase_infra", "omnimarket"))
        assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.unit
class TestEventBusEnvelopeFieldsResolved:
    """OMN-15843 (OMN-15763 F7 residual): the event_bus-schema leg emitted
    ``envelope_model``/``envelope_version=None`` for all 1460 corpus edges.
    Two real, non-fabricated sources close most of the gap:

    1. ``envelope_version`` — ONEX topic names are versioned by convention
       (``onex.<kind>.<domain>.<name>.vN``); the trailing ``.vN`` is read
       straight off the topic string already on the edge. Applies uniformly
       to every conforming topic, both roles.
    2. ``envelope_model`` — a contract's own ``published_events:`` block
       (``ModelPublishedEventEntry``: ``{topic, event_type}``) is a real,
       already-declared per-topic wire-type name. It is producer-authored,
       but pub/sub means one topic has exactly one wire envelope, so a
       consumer-side edge on the same topic inherits the same
       ``event_type`` via cross-contract correlation — not a guess, the
       same wire type the producer already declared.

    A topic with no ``.vN`` suffix, or no matching ``published_events:``
    entry anywhere in the scan, stays honestly ``None`` — never a
    fabricated placeholder.
    """

    def test_envelope_version_derived_from_topic_version_suffix(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            '  publish_topics:\n    - "onex.evt.svc.thing-happened.v3"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(
            e for e in graph.edges if e.topic == "onex.evt.svc.thing-happened.v3"
        )
        assert edge.envelope_version == "v3"

    def test_topic_without_version_suffix_leaves_envelope_version_none(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            '  publish_topics:\n    - "legacy-unversioned-topic"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(e for e in graph.edges if e.topic == "legacy-unversioned-topic")
        assert edge.envelope_version is None

    def test_envelope_model_resolved_from_published_events_block(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            '  publish_topics:\n    - "onex.evt.svc.thing-happened.v1"\n'
            "published_events:\n"
            '  - topic: "onex.evt.svc.thing-happened.v1"\n'
            '    event_type: "ModelThingHappened"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(
            e for e in graph.edges if e.topic == "onex.evt.svc.thing-happened.v1"
        )
        assert edge.envelope_model == "ModelThingHappened"

    def test_envelope_model_none_when_no_published_events_entry(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            '  publish_topics:\n    - "onex.evt.svc.unmapped.v1"\n'
            "published_events:\n"
            '  - topic: "onex.evt.svc.some-other-topic.v1"\n'
            '    event_type: "ModelSomeOtherEvent"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(e for e in graph.edges if e.topic == "onex.evt.svc.unmapped.v1")
        assert edge.envelope_model is None

    def test_consumer_edge_inherits_envelope_model_from_producer_published_events(
        self, tmp_path: Path
    ) -> None:
        shared_topic = "onex.evt.svc.cross-contract-shared.v1"
        _write(
            tmp_path,
            "svc_producer/contracts/contract.yaml",
            "name: svc_producer\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            f"  publish_topics:\n    - {shared_topic}\n"
            "published_events:\n"
            f"  - topic: {shared_topic}\n"
            '    event_type: "ModelSharedThing"\n',
        )
        _write(
            tmp_path,
            "svc_consumer/contracts/contract.yaml",
            "name: svc_consumer\n"
            "event_bus:\n"
            f"  subscribe_topics:\n    - {shared_topic}\n"
            "  publish_topics: []\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc_producer", "svc_consumer"))
        consumer_edge = next(
            e for e in graph.edges if e.topic == shared_topic and e.role == "consumer"
        )
        # The consumer's own contract never declared published_events —
        # this can only be populated by cross-contract correlation against
        # the producer's declaration, same as producer_contract_path.
        assert consumer_edge.envelope_model == "ModelSharedThing"

    def test_malformed_published_events_entries_skipped_not_fabricated(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            '  publish_topics:\n    - "onex.evt.svc.malformed-source.v1"\n'
            "published_events:\n"
            "  - topic: null\n"
            '    event_type: "ModelShouldNotAttach"\n'
            "  - event_type: missing_topic_key\n"
            '  - topic: "onex.evt.svc.malformed-source.v1"\n'
            "    event_type: 123\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(
            e for e in graph.edges if e.topic == "onex.evt.svc.malformed-source.v1"
        )
        assert edge.envelope_model is None

    def test_seams_block_declared_envelope_fields_are_never_overwritten(
        self, tmp_path: Path
    ) -> None:
        """The hand-authored ``seams:`` schema already carries real
        envelope_model/envelope_version — the new correlation pass must not
        clobber a genuinely-declared value even if a same-topic
        published_events entry exists elsewhere in the scan."""
        _write(
            tmp_path,
            "svc/contracts/contract.yaml",
            "name: svc\n"
            "seams:\n"
            "  - id: S1\n"
            "    seam: declared edge\n"
            "    role: producer\n"
            "    topic: onex.evt.svc.declared.v1\n"
            "    envelope_model: pkg.models.ModelDeclaredExplicit\n"
            "    envelope_version: '9.9.9'\n"
            "published_events:\n"
            "  - topic: onex.evt.svc.declared.v1\n"
            '    event_type: "ModelShouldNotWin"\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        edge = next(e for e in graph.edges if e.edge_id == "S1")
        assert edge.envelope_model == "pkg.models.ModelDeclaredExplicit"
        assert edge.envelope_version == "9.9.9"

    def test_two_runs_with_envelope_correlation_are_byte_identical(
        self, tmp_path: Path
    ) -> None:
        shared_topic = "onex.evt.svc.determinism-check.v1"
        _write(
            tmp_path,
            "svc_producer/contracts/contract.yaml",
            "name: svc_producer\n"
            "event_bus:\n"
            "  subscribe_topics: []\n"
            f"  publish_topics:\n    - {shared_topic}\n"
            "published_events:\n"
            f"  - topic: {shared_topic}\n"
            '    event_type: "ModelDeterminismCheck"\n',
        )
        _write(
            tmp_path,
            "svc_consumer/contracts/contract.yaml",
            "name: svc_consumer\n"
            "event_bus:\n"
            f"  subscribe_topics:\n    - {shared_topic}\n"
            "  publish_topics: []\n",
        )
        first = extract_seam_graph(str(tmp_path), ("svc_producer", "svc_consumer"))
        second = extract_seam_graph(str(tmp_path), ("svc_producer", "svc_consumer"))
        assert first.model_dump_json() == second.model_dump_json()
        assert any(e.envelope_model == "ModelDeterminismCheck" for e in first.edges)
        assert any(e.envelope_version == "v1" for e in first.edges)


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
class TestSymbolResolutionModuleConstant:
    """OMN-15779 AC1 residual: a ``Name`` argument to ``.send()``/
    ``.subscribe()`` resolves through a module-level constant, not just a
    literal."""

    def test_module_level_constant_resolves(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            'TOPIC = "onex.evt.svc.module-const.v1"\n'
            "\n"
            "def emit():\n"
            "    producer.send(TOPIC, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.module-const.v1" in values

    def test_module_level_constant_via_local_alias_resolves(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            'TOPIC = "onex.evt.svc.alias-const.v1"\n'
            "\n"
            "def emit():\n"
            "    topic = TOPIC\n"
            "    producer.send(topic, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.alias-const.v1" in values

    def test_list_wrapped_module_constant_resolves_for_subscribe(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/consumer.py",
            'TOPIC = "onex.cmd.svc.list-const.v1"\n'
            "\n"
            "def run():\n"
            "    consumer.subscribe([TOPIC])\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert "onex.cmd.svc.list-const.v1" in values


@pytest.mark.unit
class TestSymbolResolutionClassAttribute:
    def test_class_body_constant_resolves(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            "class TopicBase:\n"
            '    FOO = "onex.evt.svc.class-attr.v1"\n'
            "\n"
            "def emit():\n"
            "    producer.send(TopicBase.FOO, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.class-attr.v1" in values


@pytest.mark.unit
class TestSymbolResolutionSelfAttribute:
    """Regression coverage for the ticket's own named example --
    ``consumer_health_emitter.py:150``'s ``self._producer.send(self._topic, ...)``
    where ``self._topic`` is assigned in ``__init__`` from a local variable
    chain, not a direct literal."""

    def test_self_attr_assigned_directly_to_literal_resolves(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/emitter.py",
            "class Emitter:\n"
            "    def __init__(self):\n"
            '        self._topic = "onex.evt.svc.self-attr-literal.v1"\n'
            "\n"
            "    def emit(self):\n"
            "        self._producer.send(self._topic, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.self-attr-literal.v1" in values

    def test_self_attr_assigned_via_local_var_chain_resolves(
        self, tmp_path: Path
    ) -> None:
        # Mirrors omnibase_infra's consumer_health_emitter.py: the ctor
        # takes an optional ``topic`` param, resolves it via a local
        # variable when absent, then stores it on ``self``.
        _write(
            tmp_path,
            "svc/emitter.py",
            'DEFAULT_TOPIC = "onex.evt.svc.self-attr-chain.v1"\n'
            "\n"
            "class Emitter:\n"
            "    def __init__(self, topic=None):\n"
            "        if topic is None:\n"
            "            topic = DEFAULT_TOPIC\n"
            "        self._topic = topic\n"
            "\n"
            "    def emit(self):\n"
            "        self._producer.send(self._topic, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.self-attr-chain.v1" in values


@pytest.mark.unit
class TestSymbolResolutionRegistryDictLookup:
    """Mirrors the dominant real-corpus idiom: a factory function builds a
    dict literal mapping key constants to value constants, and a
    ``.resolve(KEY)``/``.get(KEY)`` call looks the topic up by key --
    ``ServiceTopicRegistry.from_defaults().resolve(topic_keys.X)`` in
    omnibase_infra."""

    def test_registry_resolve_call_resolves_through_dict_literal(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/topic_keys.py",
            'CONSUMER_HEALTH = "CONSUMER_HEALTH"\n',
        )
        _write(
            tmp_path,
            "svc/topic_suffixes.py",
            'SUFFIX_CONSUMER_HEALTH = "onex.evt.svc.registry-lookup.v1"\n',
        )
        _write(
            tmp_path,
            "svc/registry.py",
            "from svc import topic_keys\n"
            "from svc import topic_suffixes\n"
            "\n"
            "class ServiceTopicRegistry:\n"
            "    def __init__(self, topics):\n"
            "        self._topics = dict(topics)\n"
            "\n"
            "    @classmethod\n"
            "    def from_defaults(cls):\n"
            "        topics = {\n"
            "            topic_keys.CONSUMER_HEALTH: topic_suffixes.SUFFIX_CONSUMER_HEALTH,\n"
            "        }\n"
            "        return cls(topics)\n"
            "\n"
            "    def resolve(self, key):\n"
            "        return self._topics[key]\n",
        )
        _write(
            tmp_path,
            "svc/emitter.py",
            "from svc import topic_keys\n"
            "from svc.registry import ServiceTopicRegistry\n"
            "\n"
            "class Emitter:\n"
            "    def __init__(self, producer, topic=None):\n"
            "        if topic is None:\n"
            "            topic = ServiceTopicRegistry.from_defaults().resolve(\n"
            "                topic_keys.CONSUMER_HEALTH\n"
            "            )\n"
            "        self._producer = producer\n"
            "        self._topic = topic\n"
            "\n"
            "    def emit(self):\n"
            "        self._producer.send(self._topic, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.registry-lookup.v1" in values

    def test_ambiguous_key_across_two_dicts_stays_unresolved(
        self, tmp_path: Path
    ) -> None:
        # Two distinct dict literals map the same resolved key to two
        # DIFFERENT values -- must not fabricate a pick.
        _write(
            tmp_path,
            "svc/registries.py",
            'KEY = "SHARED_KEY"\n'
            'MAP_A = {KEY: "onex.evt.svc.ambiguous-a.v1"}\n'
            'MAP_B = {KEY: "onex.evt.svc.ambiguous-b.v1"}\n',
        )
        _write(
            tmp_path,
            "svc/emitter.py",
            "from svc.registries import KEY, MAP_A\n"
            "\n"
            "def emit():\n"
            "    producer.send(MAP_A.get(KEY), payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.ambiguous-a.v1" not in values
        assert "onex.evt.svc.ambiguous-b.v1" not in values
        unresolved = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND_UNRESOLVED
        }
        assert unresolved  # emitted as an honest unresolved observation


@pytest.mark.unit
class TestSymbolResolutionCrossFileImport:
    def test_module_constant_resolves_across_files_via_from_import(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/constants.py",
            'TOPIC = "onex.evt.svc.cross-file-const.v1"\n',
        )
        _write(
            tmp_path,
            "svc/publisher.py",
            "from svc.constants import TOPIC\n"
            "\n"
            "def emit():\n"
            "    producer.send(TOPIC, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.cross-file-const.v1" in values

    def test_class_attribute_resolves_across_files_via_from_import(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/constants.py",
            'class TopicBase:\n    FOO = "onex.evt.svc.cross-file-class-attr.v1"\n',
        )
        _write(
            tmp_path,
            "svc/publisher.py",
            "from svc.constants import TopicBase\n"
            "\n"
            "def emit():\n"
            "    producer.send(TopicBase.FOO, payload)\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        values = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.cross-file-class-attr.v1" in values


@pytest.mark.unit
class TestSymbolResolutionUnresolvedIsExplicit:
    """The ticket's own carve-out: a dynamic/f-string topic (or any other
    unmodeled dynamic expression) must surface as an explicit
    ``*_UNRESOLVED`` observation, not a silent miss and not a fabricated
    value."""

    def test_fstring_topic_is_unresolved_not_silently_dropped(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            "def emit(slug):\n"
            '    producer.send(f"tenant-{slug}.onex.evt.svc.dynamic.v1", payload)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        resolved_kinds = {
            o.kind
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert not resolved_kinds
        unresolved = [
            o
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND_UNRESOLVED
        ]
        assert len(unresolved) == 1
        assert "slug" in unresolved[0].value
        assert unresolved[0].line_number == 2

    def test_unmodeled_function_call_result_is_unresolved(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "svc/consumer.py",
            "def run():\n    consumer.subscribe([compute_dynamic_topic()])\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        resolved = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert not resolved
        unresolved = [
            o
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE_UNRESOLVED
        ]
        assert len(unresolved) == 1


@pytest.mark.unit
class TestRefPinIndirectionResolution:
    """OMN-15779 scope item 3: a ``@ref:`` pin pointing at a YAML file+key is
    additionally resolved to the underlying literal value it names, without
    changing the existing raw ``REF_PIN`` observation."""

    def test_ref_pin_target_resolves_to_underlying_value(self, tmp_path: Path) -> None:
        # @ref: paths resolve confined against repo_base (the whole pinned
        # tree), not against the pinning file's own discovery root -- same
        # convention as a discovery root itself.
        _write(
            tmp_path,
            "configs/service_endpoints.yaml",
            "backends:\n  cloud-x: onex.evt.svc.ref-resolved.v1\n",
        )
        _write(
            tmp_path,
            "svc/publisher.py",
            "# @ref: configs/service_endpoints.yaml#backends.cloud-x\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        raw_pins = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/service_endpoints.yaml#backends.cloud-x" in raw_pins
        resolved_pins = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN_RESOLVED
        }
        assert "onex.evt.svc.ref-resolved.v1" in resolved_pins

    def test_ref_pin_with_missing_target_stays_unresolved_only(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/publisher.py",
            "# @ref: configs/does-not-exist.yaml#backends.x\n",
        )
        graph = extract_seam_graph(str(tmp_path), ("svc",))
        raw_pins = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/does-not-exist.yaml#backends.x" in raw_pins
        resolved_pins = {
            o.value
            for o in graph.code_observations
            if o.kind == EnumSeamGraphObservationKind.REF_PIN_RESOLVED
        }
        assert not resolved_pins


@pytest.mark.unit
class TestSymbolResolutionDeterminism:
    def test_two_runs_over_symbol_resolved_tree_are_byte_identical(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "svc/topic_keys.py",
            'CONSUMER_HEALTH = "CONSUMER_HEALTH"\n',
        )
        _write(
            tmp_path,
            "svc/topic_suffixes.py",
            'SUFFIX_CONSUMER_HEALTH = "onex.evt.svc.determinism.v1"\n',
        )
        _write(
            tmp_path,
            "svc/registry.py",
            "from svc import topic_keys, topic_suffixes\n"
            "\n"
            "class Registry:\n"
            "    def __init__(self, topics):\n"
            "        self._topics = dict(topics)\n"
            "\n"
            "    @classmethod\n"
            "    def from_defaults(cls):\n"
            "        return cls(\n"
            "            {topic_keys.CONSUMER_HEALTH: topic_suffixes.SUFFIX_CONSUMER_HEALTH}\n"
            "        )\n"
            "\n"
            "    def resolve(self, key):\n"
            "        return self._topics[key]\n",
        )
        _write(
            tmp_path,
            "svc/emitter.py",
            "from svc import topic_keys\n"
            "from svc.registry import Registry\n"
            "\n"
            "class Emitter:\n"
            "    def __init__(self):\n"
            "        self._topic = Registry.from_defaults().resolve(\n"
            "            topic_keys.CONSUMER_HEALTH\n"
            "        )\n"
            "\n"
            "    def emit(self):\n"
            "        self._producer.send(self._topic, payload)\n",
        )
        first = extract_seam_graph(str(tmp_path), ("svc",))
        second = extract_seam_graph(str(tmp_path), ("svc",))
        assert first.model_dump_json() == second.model_dump_json()
        values = {
            o.value
            for o in first.code_observations
            if o.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "onex.evt.svc.determinism.v1" in values


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


@pytest.mark.unit
class TestTestHarnessSourceClassification:
    """OMN-15779 R1 audit (RSD lane-2 comment 4acd1d93, 2026-08-10): the
    corpus-wide producer_send/consumer_subscribe count mixes real
    inter-service application seams with test-substrate code that happens
    to be shipped inside a scanned ``src/`` tree rather than under
    ``tests/`` (e.g. ``omnibase_core``'s ``event_bus/testing/
    contract_event_bus_substrate.py``, ``runtime/transport/
    runtime_transport_conformance.py``). A harness observation is real
    code, correctly extracted — it is excluded from the
    production-application count, never dropped from the graph itself."""

    def test_event_bus_testing_path_is_classified_as_harness(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/omnibase_core/event_bus/testing/contract_event_bus_substrate.py",
            'producer.send("harness-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = next(
            o for o in graph.code_observations if o.value == "harness-topic"
        )
        assert observation.is_test_harness is True

    def test_runtime_transport_conformance_path_is_classified_as_harness(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/omnibase_infra/runtime/transport/runtime_transport_conformance.py",
            'producer.send("conformance-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = next(
            o for o in graph.code_observations if o.value == "conformance-topic"
        )
        assert observation.is_test_harness is True

    def test_runtime_transport_non_conformance_file_is_production(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/omnibase_infra/runtime/transport/kafka_transport.py",
            'producer.send("transport-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = next(
            o for o in graph.code_observations if o.value == "transport-topic"
        )
        assert observation.is_test_harness is False

    def test_ordinary_production_path_is_not_harness(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "src/omnibase_infra/runtime/consumer_health_emitter.py",
            'producer.send("prod-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = next(
            o for o in graph.code_observations if o.value == "prod-topic"
        )
        assert observation.is_test_harness is False

    def test_tests_directory_within_scanned_tree_is_classified_as_harness(
        self, tmp_path: Path
    ) -> None:
        # Defensive: tests/** is already excluded upstream (no discovery
        # root passed by any real caller today includes a tests/ dir) —
        # if one ever did, the classification must still hold rather than
        # silently mis-counting harness code as production.
        _write(
            tmp_path,
            "src/pkg/tests/test_something.py",
            'producer.send("test-file-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = next(
            o for o in graph.code_observations if o.value == "test-file-topic"
        )
        assert observation.is_test_harness is True

    def test_harness_classification_does_not_depend_on_kind(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/pkg/event_bus/testing/contract_event_bus_substrate.py",
            'consumer.subscribe(["harness-sub"])\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = next(
            o for o in graph.code_observations if o.value == "harness-sub"
        )
        assert observation.is_test_harness is True


@pytest.mark.unit
class TestObservationSummarySplitsHarnessFromProduction:
    """Summary counts (OMN-15779 R1 audit) surface the
    production-application vs test-harness-in-src split per observation
    kind as a durable, code-computed value — replacing the ad hoc manual
    grep/count a prior session produced by hand."""

    def test_summary_splits_producer_send_by_harness_vs_production(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/pkg/event_bus/testing/contract_event_bus_substrate.py",
            'producer.send("harness-topic", p)\n',
        )
        _write(
            tmp_path,
            "src/pkg/service/publisher.py",
            'producer.send("prod-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        by_kind = {s.kind: s for s in graph.observation_summary}
        producer_summary = by_kind[EnumSeamGraphObservationKind.PRODUCER_SEND]
        assert producer_summary.total == 2
        assert producer_summary.test_harness == 1
        assert producer_summary.production_application == 1

    def test_summary_counts_are_internally_consistent_per_kind(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/pkg/runtime/transport/runtime_transport_conformance.py",
            'consumer.subscribe(["harness-sub"])\n',
        )
        _write(
            tmp_path,
            "src/pkg/service/consumer.py",
            'consumer.subscribe(["prod-sub"])\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        assert len(graph.observation_summary) > 0
        for summary in graph.observation_summary:
            assert (
                summary.total == summary.test_harness + summary.production_application
            )

    def test_summary_omits_kinds_with_zero_observations(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/pkg/nothing.py", "x = 1\n")
        graph = extract_seam_graph(str(tmp_path), ("src",))
        assert graph.observation_summary == ()


# OMN-15854: measured basis (this session's own run, reproduced live against
# omnibase_core/omnibase_infra/omnimarket at their current dev tips) -- 8
# validator-runtime-tooling files, each contributing 2 producer_send + 2
# consumer_subscribe production-application hits (16 of 22 per kind), and
# the 6+6 genuine inter-service sites making up the remaining 6 of 22 per
# kind. Paths below are the exact repo-relative source_path strings the
# extractor produces when scanning with discovery_roots =
# ("omnibase_core/src", "omnibase_infra/src", "omnimarket/src") against a
# repo_base containing all three repos side by side (the real production
# scan shape) -- not the bare "src/..." shorthand most other tests in this
# file use, because pinning the LITERAL source_path is the point here.
_TOOLING_FILES: tuple[str, ...] = (
    "omnibase_core/src/omnibase_core/validation/doc_content_scan/runtime_doc_content_scan.py",
    "omnibase_core/src/omnibase_core/validation/hardcoded_topic/runtime_hardcoded_topic.py",
    "omnibase_core/src/omnibase_core/validation/local_paths/runtime_local_paths.py",
    "omnibase_core/src/omnibase_core/validation/localhost_url/runtime_localhost_url.py",
    "omnibase_core/src/omnibase_core/validation/no_faked_boundary/runtime_no_faked_boundary.py",
    "omnibase_core/src/omnibase_core/validation/pin_hygiene/runtime_pin_hygiene.py",
    "omnibase_core/src/omnibase_core/validation/private_ip/runtime_private_ip.py",
    "omnibase_core/src/omnibase_core/validation/todo_marker/runtime_todo_marker.py",
)

# The 6 genuine inter-service producer_send sites (OMN-15854 ticket list).
_INTER_SERVICE_PRODUCER_FILES: tuple[str, ...] = (
    "omnibase_infra/src/omnibase_infra/event_bus/consumer_health_emitter.py",
    "omnibase_infra/src/omnibase_infra/nodes/node_consumer_health_triage_effect/handlers/handler_consumer_health_triage.py",
    "omnibase_infra/src/omnibase_infra/observability/runtime_log_event_bridge.py",
    "omnibase_infra/src/omnibase_infra/services/registry_api/registry_discovery.py",
    "omnimarket/src/omnimarket/logging/structured_logger.py",
    "omnimarket/src/omnimarket/nodes/node_review_thread_reconciler/handlers/handler_review_thread_reconciler.py",
)

# The 6 genuine inter-service consumer_subscribe sites (2 files, 5+1 sites
# -- OMN-15854 ticket list).
_INTER_SERVICE_CONSUMER_FILES: tuple[str, ...] = (
    "omnibase_core/src/omnibase_core/mixins/mixin_service_registry.py",
    "omnibase_core/src/omnibase_core/runtime/runtime_local.py",
)


@pytest.mark.unit
class TestValidatorRuntimeToolingThirdSplit:
    """OMN-15854: production-application -> {validator-runtime-tooling,
    inter-service-application}. Pins the 8 tooling files' classification
    and asserts the split is non-weakening against the 12 named genuine
    inter-service sites (6 producer + 6 consumer)."""

    @pytest.mark.parametrize("tooling_path", _TOOLING_FILES)
    def test_each_of_the_8_tooling_files_classifies_as_validator_runtime_tooling(
        self, tmp_path: Path, tooling_path: str
    ) -> None:
        _write(
            tmp_path,
            tooling_path,
            'producer.send("tool-topic", p)\nconsumer.subscribe(["tool-sub"])\n',
        )
        graph = extract_seam_graph(
            str(tmp_path), ("omnibase_core/src", "omnibase_infra/src", "omnimarket/src")
        )
        producer_obs = next(
            o for o in graph.code_observations if o.value == "tool-topic"
        )
        consumer_obs = next(o for o in graph.code_observations if o.value == "tool-sub")
        assert producer_obs.is_test_harness is False
        assert producer_obs.is_validator_runtime_tooling is True
        assert consumer_obs.is_test_harness is False
        assert consumer_obs.is_validator_runtime_tooling is True

    @pytest.mark.parametrize("inter_service_path", _INTER_SERVICE_PRODUCER_FILES)
    def test_each_of_the_6_genuine_producer_sites_is_not_reclassified_as_tooling(
        self, tmp_path: Path, inter_service_path: str
    ) -> None:
        _write(tmp_path, inter_service_path, 'producer.send("real-topic", p)\n')
        graph = extract_seam_graph(
            str(tmp_path), ("omnibase_core/src", "omnibase_infra/src", "omnimarket/src")
        )
        observation = next(
            o for o in graph.code_observations if o.value == "real-topic"
        )
        assert observation.is_test_harness is False
        assert observation.is_validator_runtime_tooling is False

    @pytest.mark.parametrize("inter_service_path", _INTER_SERVICE_CONSUMER_FILES)
    def test_each_of_the_2_genuine_consumer_files_is_not_reclassified_as_tooling(
        self, tmp_path: Path, inter_service_path: str
    ) -> None:
        _write(tmp_path, inter_service_path, 'consumer.subscribe(["real-sub"])\n')
        graph = extract_seam_graph(
            str(tmp_path), ("omnibase_core/src", "omnibase_infra/src", "omnimarket/src")
        )
        observation = next(o for o in graph.code_observations if o.value == "real-sub")
        assert observation.is_test_harness is False
        assert observation.is_validator_runtime_tooling is False

    def test_named_inter_service_site_consumer_health_emitter_stays_inter_service(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "omnibase_infra/src/omnibase_infra/event_bus/consumer_health_emitter.py",
            'producer.send("health-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("omnibase_infra/src",))
        observation = next(
            o for o in graph.code_observations if o.value == "health-topic"
        )
        assert observation.is_validator_runtime_tooling is False

    def test_named_inter_service_site_mixin_service_registry_stays_inter_service(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "omnibase_core/src/omnibase_core/mixins/mixin_service_registry.py",
            'consumer.subscribe(["registry-sub"])\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("omnibase_core/src",))
        observation = next(
            o for o in graph.code_observations if o.value == "registry-sub"
        )
        assert observation.is_validator_runtime_tooling is False

    def test_validation_dir_without_runtime_prefix_filename_is_not_tooling(
        self, tmp_path: Path
    ) -> None:
        """Both conditions are required -- a validation/ path whose
        filename does NOT start with runtime_ must not match."""
        _write(
            tmp_path,
            "omnibase_core/src/omnibase_core/validation/doc_content_scan/scanner.py",
            'producer.send("scanner-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("omnibase_core/src",))
        observation = next(
            o for o in graph.code_observations if o.value == "scanner-topic"
        )
        assert observation.is_validator_runtime_tooling is False

    def test_runtime_prefix_filename_without_validation_dir_is_not_tooling(
        self, tmp_path: Path
    ) -> None:
        """Both conditions are required -- a runtime_*.py filename outside
        validation/ (e.g. runtime_local.py, runtime_log_event_bridge.py)
        must not match, matching the two named counter-examples in the
        OMN-15854 ticket."""
        _write(
            tmp_path,
            "omnibase_core/src/omnibase_core/runtime/runtime_local.py",
            'consumer.subscribe(["local-sub"])\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("omnibase_core/src",))
        observation = next(o for o in graph.code_observations if o.value == "local-sub")
        assert observation.is_validator_runtime_tooling is False

    def test_unrecognized_path_defaults_fail_closed_to_inter_service_application(
        self, tmp_path: Path
    ) -> None:
        """A file matching neither the harness glob nor the tooling glob
        defaults to inter_service_application (un-suppressed) -- proving
        tooling-bucket membership requires an explicit, positive glob
        match; absence of proof never silently hides an observation from
        the trusted-signal count."""
        _write(
            tmp_path,
            "omnibase_infra/src/omnibase_infra/some_new_module/emitter.py",
            'producer.send("unrecognized-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("omnibase_infra/src",))
        observation = next(
            o for o in graph.code_observations if o.value == "unrecognized-topic"
        )
        assert observation.is_test_harness is False
        assert observation.is_validator_runtime_tooling is False

    def test_full_corpus_shape_reproduces_22_16_6_split_per_kind(
        self, tmp_path: Path
    ) -> None:
        """End-to-end reproduction of the OMN-15854 measured basis: 22
        production-application hits per kind, 16 of which are tooling (the
        8 files x 2 hits), 6 of which are genuine inter-service sites."""
        for tooling_path in _TOOLING_FILES:
            _write(
                tmp_path,
                tooling_path,
                'producer.send("t", p)\nproducer.send("t2", p)\n'
                'consumer.subscribe(["c"])\nconsumer.subscribe(["c2"])\n',
            )
        for producer_path in _INTER_SERVICE_PRODUCER_FILES:
            _write(tmp_path, producer_path, 'producer.send("real", p)\n')
        _write(
            tmp_path,
            "omnibase_core/src/omnibase_core/mixins/mixin_service_registry.py",
            "\n".join(f'consumer.subscribe(["real{i}"])' for i in range(5)) + "\n",
        )
        _write(
            tmp_path,
            "omnibase_core/src/omnibase_core/runtime/runtime_local.py",
            'consumer.subscribe(["real-local"])\n',
        )
        graph = extract_seam_graph(
            str(tmp_path), ("omnibase_core/src", "omnibase_infra/src", "omnimarket/src")
        )
        by_kind = {s.kind: s for s in graph.observation_summary}
        producer_summary = by_kind[EnumSeamGraphObservationKind.PRODUCER_SEND]
        consumer_summary = by_kind[EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE]
        assert producer_summary.production_application == 22
        assert producer_summary.validator_runtime_tooling == 16
        assert producer_summary.inter_service_application == 6
        assert consumer_summary.production_application == 22
        assert consumer_summary.validator_runtime_tooling == 16
        assert consumer_summary.inter_service_application == 6


@pytest.mark.unit
class TestObservationSummaryThirdSplitInvariants:
    """OMN-15854: production_application == validator_runtime_tooling +
    inter_service_application, and total == test_harness +
    validator_runtime_tooling + inter_service_application, for every
    summary row -- not merely for the two hand-picked cases above."""

    def test_summary_rows_satisfy_the_three_way_split_invariant(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "src/pkg/event_bus/testing/contract_event_bus_substrate.py",
            'producer.send("harness-topic", p)\n',
        )
        _write(
            tmp_path,
            "src/omnibase_core/validation/doc_content_scan/runtime_doc_content_scan.py",
            'producer.send("tool-topic", p)\n',
        )
        _write(
            tmp_path,
            "src/pkg/service/publisher.py",
            'producer.send("prod-topic", p)\n',
        )
        graph = extract_seam_graph(str(tmp_path), ("src",))
        assert len(graph.observation_summary) > 0
        for summary in graph.observation_summary:
            assert (
                summary.total
                == summary.test_harness
                + summary.validator_runtime_tooling
                + summary.inter_service_application
            )
            assert (
                summary.production_application
                == summary.validator_runtime_tooling + summary.inter_service_application
            )
        producer_summary = next(
            s
            for s in graph.observation_summary
            if s.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        )
        assert producer_summary.total == 3
        assert producer_summary.test_harness == 1
        assert producer_summary.validator_runtime_tooling == 1
        assert producer_summary.inter_service_application == 1


@pytest.mark.unit
class TestSchemaVersionV2Bump:
    """OMN-15854 R2 fix: schema_version bumped from seam-graph/v1 to
    seam-graph/v2 in the same PR that adds the third-split computed
    fields, and the full serialized field set is pinned so a future
    computed-field addition fails loudly rather than silently drifting the
    wire shape again."""

    def test_schema_version_literal_is_v2(self, tmp_path: Path) -> None:
        graph = extract_seam_graph(str(tmp_path), ("src",))
        assert graph.schema_version == "seam-graph/v2"

    def test_default_constructed_schema_version_is_v2(self) -> None:
        assert ModelSeamGraphV1().schema_version == "seam-graph/v2"

    def test_top_level_serialized_field_set_is_pinned(self, tmp_path: Path) -> None:
        graph = extract_seam_graph(str(tmp_path), ("src",))
        assert set(graph.model_dump().keys()) == {
            "schema_version",
            "discovery_roots",
            "edges",
            "code_observations",
            "source_manifest",
            "observation_summary",
        }

    def test_code_observation_serialized_field_set_is_pinned(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "src/pkg/publisher.py", 'producer.send("t", p)\n')
        graph = extract_seam_graph(str(tmp_path), ("src",))
        observation = graph.code_observations[0]
        assert set(observation.model_dump().keys()) == {
            "source_path",
            "kind",
            "value",
            "line_number",
            "is_test_harness",
            "is_validator_runtime_tooling",
        }

    def test_observation_kind_summary_serialized_field_set_is_pinned(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "src/pkg/publisher.py", 'producer.send("t", p)\n')
        graph = extract_seam_graph(str(tmp_path), ("src",))
        summary = graph.observation_summary[0]
        assert set(summary.model_dump().keys()) == {
            "kind",
            "total",
            "test_harness",
            "production_application",
            "validator_runtime_tooling",
            "inter_service_application",
        }
