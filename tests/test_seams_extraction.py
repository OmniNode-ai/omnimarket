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
