# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for node_ast_node_analyzer — pure deterministic AST analysis.

Fixtures are node source strings; each asserts the structural extraction and the
import/substring I/O-operation heuristic per case, plus a class-with-no-I/O case.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ast_node_analyzer.handlers.handler_ast_node_analyzer import (
    HandlerAstNodeAnalyzer,
    analyze_node_source,
)
from omnimarket.nodes.node_ast_node_analyzer.models.model_ast_node_analyzer_request import (
    ModelAstNodeAnalyzerRequest,
)
from omnimarket.nodes.node_ast_node_analyzer.models.model_node_analysis import (
    ModelNodeAnalysis,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_ast_node_analyzer"
    / "contract.yaml"
)

# --- fixture node sources ------------------------------------------------------

_HTTP_SOURCE = '''\
import httpx


class NodeHttpEffect(NodeEffect):
    """Calls an HTTP service."""

    async def run(self, url: str) -> str:
        client = httpx.AsyncClient()
        return await client.get(url)
'''

_DB_SOURCE = """\
import asyncpg


class NodeDbEffect(NodeEffect):
    async def run(self, pool) -> int:
        return await pool.fetchval("select 1")
"""

_MQ_SOURCE = """\
from aiokafka import AIOKafkaProducer


class NodeQueueEffect(NodeEffect):
    async def run(self, producer: AIOKafkaProducer) -> None:
        await producer.send("topic", b"payload")
"""

_FILE_IO_SOURCE = """\
from pathlib import Path


class NodeFileEffect(NodeEffect):
    def run(self, target: str) -> str:
        return Path(target).read_text()
"""

_PURE_COMPUTE_SOURCE = '''\
class NodePureCompute(NodeCompute):
    """A pure compute node with no external I/O."""

    def _helper(self, value: int) -> int:
        return value * 2

    def run(self, value: int) -> int:
        return self._helper(value)
'''

_COMBINED_SOURCE = """\
import httpx
import asyncpg
from pathlib import Path


class NodeComboEffect(NodeEffect):
    async def run(self, url: str) -> str:
        Path("cache").write_text(url)
        return url
"""

_MIXIN_SOURCE = """\
class NodeMixedEffect(NodeEffect, MixinHealthCheck, MixinMetrics):
    async def run(self) -> None:
        return None
"""

_NO_NODE_CLASS_SOURCE = """\
class JustAPlainClass:
    def method(self) -> int:
        return 1
"""

_SYNTAX_ERROR_SOURCE = "def broken(:\n    pass\n"


def _analyze(source: str) -> ModelNodeAnalysis:
    return HandlerAstNodeAnalyzer().handle(
        ModelAstNodeAnalyzerRequest(source_text=source)
    )


@pytest.mark.unit
class TestIoOperationHeuristic:
    def test_http_import_maps_to_http_request(self) -> None:
        result = _analyze(_HTTP_SOURCE)
        assert "http_request" in result.io_operations

    def test_database_import_maps_to_database_query(self) -> None:
        result = _analyze(_DB_SOURCE)
        assert "database_query" in result.io_operations

    def test_message_queue_import_maps_to_message_queue(self) -> None:
        result = _analyze(_MQ_SOURCE)
        assert "message_queue" in result.io_operations

    def test_path_substring_maps_to_file_io(self) -> None:
        result = _analyze(_FILE_IO_SOURCE)
        assert "file_io" in result.io_operations

    def test_no_io_defaults_to_computation(self) -> None:
        result = _analyze(_PURE_COMPUTE_SOURCE)
        assert result.io_operations == ("computation",)

    def test_combined_operations_in_declaration_order(self) -> None:
        result = _analyze(_COMBINED_SOURCE)
        # http (import) then database (import) then file_io (Path( substring);
        # message_queue absent, computation suppressed once any op is present.
        assert result.io_operations == ("http_request", "database_query", "file_io")


@pytest.mark.unit
class TestStructuralExtraction:
    def test_extracts_class_name_and_base_class(self) -> None:
        result = _analyze(_HTTP_SOURCE)
        assert result.class_name == "NodeHttpEffect"
        assert result.base_class == "NodeEffect"

    def test_extracts_methods(self) -> None:
        result = _analyze(_PURE_COMPUTE_SOURCE)
        assert result.methods == ("_helper", "run")

    def test_extracts_docstring(self) -> None:
        result = _analyze(_PURE_COMPUTE_SOURCE)
        assert result.docstring == "A pure compute node with no external I/O."

    def test_docstring_is_none_when_absent(self) -> None:
        result = _analyze(_DB_SOURCE)
        assert result.docstring is None

    def test_extracts_mixins_and_separates_base(self) -> None:
        result = _analyze(_MIXIN_SOURCE)
        assert result.base_class == "NodeEffect"
        assert result.mixins == ("MixinHealthCheck", "MixinMetrics")

    def test_class_with_no_io_has_empty_mixins(self) -> None:
        result = _analyze(_PURE_COMPUTE_SOURCE)
        assert result.mixins == ()


@pytest.mark.unit
class TestErrorHandling:
    def test_no_node_class_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="no ONEX node class"):
            _analyze(_NO_NODE_CLASS_SOURCE)

    def test_syntax_error_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="not valid Python"):
            _analyze(_SYNTAX_ERROR_SOURCE)


@pytest.mark.unit
class TestDeterminismAndPurity:
    def test_analysis_is_deterministic(self) -> None:
        first = _analyze(_COMBINED_SOURCE)
        second = _analyze(_COMBINED_SOURCE)
        assert first == second

    def test_pure_function_matches_handler(self) -> None:
        via_fn = analyze_node_source(_HTTP_SOURCE)
        via_handler = _analyze(_HTTP_SOURCE)
        assert via_fn == via_handler


@pytest.mark.unit
class TestContractStateCoverage:
    """Cover the contract-declared output states and published topic."""

    def test_contract_declares_output_keys(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        declared = set(contract["outputs"])
        assert declared == {
            "class_name",
            "base_class",
            "mixins",
            "methods",
            "docstring",
            "io_operations",
        }

    def test_contract_declares_bus_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.ast-node-analysis-completed.v1"
        )
        assert (
            "onex.evt.omnimarket.ast-node-analysis-completed.v1"
            in contract["event_bus"]["publish_topics"]
        )
