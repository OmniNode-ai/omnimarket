# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_codebase_intelligence_bridge_effect.

All five operations are tested with a mock adapter so no subprocess or
network I/O occurs. Tests also cover timeout and error paths.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_codebase_intelligence_bridge_effect.adapters.adapter_repowise_cli import (
    AdapterRepoWiseCLI,
)
from omnimarket.nodes.node_codebase_intelligence_bridge_effect.adapters.protocol_codebase_intelligence import (
    ProtocolCodebaseIntelligence,
)
from omnimarket.nodes.node_codebase_intelligence_bridge_effect.handlers.handler_codebase_intelligence_bridge import (
    HandlerCodebaseIntelligenceBridge,
)
from omnimarket.nodes.node_codebase_intelligence_bridge_effect.models.model_codebase_intelligence_query_request import (
    ModelCodebaseIntelligenceQueryRequest,
)
from omnimarket.nodes.node_codebase_intelligence_bridge_effect.models.model_codebase_intelligence_query_response import (
    ModelCodebaseIntelligenceQueryResponse,
)


def _make_raw_response(
    *,
    confidence: str = "high",
    retrieval_quality: str = "good",
    stale_warning: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "_meta": {
            "confidence": confidence,
            "retrieval_quality": retrieval_quality,
        },
        "answer": "some answer",
    }
    if stale_warning is not None:
        result["_meta"]["stale_warning"] = stale_warning
    if extra:
        result.update(extra)
    return result


def _make_mock_adapter(raw: dict[str, Any]) -> MagicMock:
    adapter = MagicMock(spec=ProtocolCodebaseIntelligence)
    adapter.query = AsyncMock(return_value=raw)
    return adapter


def _make_handler(raw: dict[str, Any]) -> HandlerCodebaseIntelligenceBridge:
    return HandlerCodebaseIntelligenceBridge(adapter=_make_mock_adapter(raw))


@pytest.mark.unit
class TestCodebaseIntelligenceBridgeImports:
    def test_node_importable(self) -> None:
        import omnimarket.nodes.node_codebase_intelligence_bridge_effect as node

        assert node is not None

    def test_handler_importable(self) -> None:
        assert HandlerCodebaseIntelligenceBridge is not None

    def test_protocol_satisfied_by_adapter(self) -> None:
        adapter = AdapterRepoWiseCLI()
        assert isinstance(adapter, ProtocolCodebaseIntelligence)


@pytest.mark.unit
class TestGetAnswer:
    async def test_success_returns_result_and_meta(self) -> None:
        raw = _make_raw_response(confidence="high", retrieval_quality="good")
        handler = _make_handler(raw)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_answer",
            query="how does X work",
        )
        resp = await handler.handle(req)

        assert isinstance(resp, ModelCodebaseIntelligenceQueryResponse)
        assert resp.status == "success"
        assert resp.result == raw
        assert resp.confidence == "high"
        assert resp.retrieval_quality == "good"
        assert resp.stale_warning is None
        assert resp.error_message is None

    async def test_stale_warning_propagated(self) -> None:
        raw = _make_raw_response(stale_warning="index is 3 days old")
        handler = _make_handler(raw)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_answer",
            query="why is Y like this",
        )
        resp = await handler.handle(req)

        assert resp.status == "success"
        assert resp.stale_warning == "index is 3 days old"


@pytest.mark.unit
class TestGetContext:
    async def test_targets_and_include_forwarded_to_adapter(self) -> None:
        mock_adapter = _make_mock_adapter(_make_raw_response())
        handler = HandlerCodebaseIntelligenceBridge(adapter=mock_adapter)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_context",
            query="some/module.py",
            targets=("some/module.py",),
            include=("callers", "ownership"),
        )
        resp = await handler.handle(req)

        assert resp.status == "success"
        mock_adapter.query.assert_awaited_once_with(
            operation="get_context",
            query="some/module.py",
            targets=("some/module.py",),
            include=("callers", "ownership"),
        )


@pytest.mark.unit
class TestGetSymbol:
    async def test_success(self) -> None:
        raw = _make_raw_response(retrieval_quality="exact")
        handler = _make_handler(raw)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_symbol",
            query="path/to/file.py::MyClass",
            targets=("path/to/file.py::MyClass",),
        )
        resp = await handler.handle(req)

        assert resp.status == "success"
        assert resp.retrieval_quality == "exact"


@pytest.mark.unit
class TestSearchCodebase:
    async def test_empty_targets_allowed(self) -> None:
        raw = _make_raw_response()
        handler = _make_handler(raw)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="search_codebase",
            query="Kafka topic routing",
        )
        resp = await handler.handle(req)

        assert resp.status == "success"
        assert resp.operation == "search_codebase"


@pytest.mark.unit
class TestGetWhy:
    async def test_success(self) -> None:
        raw = _make_raw_response(confidence="medium")
        handler = _make_handler(raw)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_why",
            query="why is compat a separate repo",
            targets=("omnibase_compat/",),
        )
        resp = await handler.handle(req)

        assert resp.status == "success"
        assert resp.confidence == "medium"


@pytest.mark.unit
class TestErrorPaths:
    async def test_adapter_raises_returns_error_response(self) -> None:
        adapter = MagicMock(spec=ProtocolCodebaseIntelligence)
        adapter.query = AsyncMock(side_effect=RuntimeError("CLI crashed"))
        handler = HandlerCodebaseIntelligenceBridge(adapter=adapter)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_answer",
            query="any query",
        )
        resp = await handler.handle(req)

        assert resp.status == "error"
        assert "CLI crashed" in (resp.error_message or "")
        assert resp.result is None

    async def test_timeout_returns_timeout_response(self) -> None:
        async def _slow(*_: object, **__: object) -> dict[str, Any]:
            await asyncio.sleep(60)
            return {}

        adapter = MagicMock(spec=ProtocolCodebaseIntelligence)
        adapter.query = _slow
        handler = HandlerCodebaseIntelligenceBridge(
            adapter=adapter,
            timeout_seconds=0.01,
        )

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="search_codebase",
            query="slow query",
        )
        resp = await handler.handle(req)

        assert resp.status == "timeout"
        assert resp.error_message is not None
        assert "timed out" in resp.error_message.lower()

    async def test_missing_meta_fields_are_none(self) -> None:
        raw: dict[str, Any] = {"answer": "no meta here"}
        handler = _make_handler(raw)

        req = ModelCodebaseIntelligenceQueryRequest(
            operation="get_answer",
            query="no meta query",
        )
        resp = await handler.handle(req)

        assert resp.status == "success"
        assert resp.confidence is None
        assert resp.retrieval_quality is None
        assert resp.stale_warning is None


@pytest.mark.unit
class TestAdapterRepoWiseCLI:
    async def test_unknown_operation_raises_value_error(self) -> None:
        adapter = AdapterRepoWiseCLI()
        with pytest.raises(ValueError, match="Unknown operation"):
            await adapter.query(
                operation="nonexistent_op",
                query="q",
                targets=(),
                include=(),
            )

    async def test_cli_nonzero_exit_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_exec(*_: object, **__: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b"some error"))
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        adapter = AdapterRepoWiseCLI(cli_executable="repowise")
        with pytest.raises(RuntimeError, match="repowise CLI exited 1"):
            await adapter.query(
                operation="get_answer",
                query="q",
                targets=(),
                include=(),
            )

    async def test_cli_invalid_json_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_exec(*_: object, **__: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"not-json", b""))
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        adapter = AdapterRepoWiseCLI()
        with pytest.raises(RuntimeError, match="non-JSON"):
            await adapter.query(
                operation="get_answer",
                query="q",
                targets=(),
                include=(),
            )

    async def test_cli_success_returns_parsed_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        payload = {"answer": "42", "_meta": {"confidence": "high"}}

        async def _fake_exec(*_: object, **__: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(
                return_value=(json.dumps(payload).encode(), b"")
            )
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        adapter = AdapterRepoWiseCLI()
        result = await adapter.query(
            operation="get_answer",
            query="q",
            targets=(),
            include=(),
        )
        assert result == payload
