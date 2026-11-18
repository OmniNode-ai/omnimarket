# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the public query handler with local HTTP and a persisted index."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    ModelWorkLedgerGraphQueryRequest,
    ModelWorkLedgerGraphQueryResult,
)
from omnimarket.models.work_ledger_graph.model_work_ledger_similarity import (
    ModelLedgerSimilarityIndexRequest,
    ModelLedgerSimilarityRow,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.handler_work_ledger_graph_query import (
    HandlerWorkLedgerGraphQuery,
)

STAMP = "2026-11-17T10:00:00Z"
BEFORE = "2026-11-18T00:00:00Z"


def fake_endpoint(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "127.0.0.1"
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "lab-embedding"}]})
    data = json.loads(request.content)
    assert request.url.path == "/v1/embeddings"
    assert data["model"] == "lab-embedding"
    return httpx.Response(
        200,
        json={
            "model": "lab-embedding",
            "data": [
                {"index": i, "embedding": [1.0, float(text.startswith("Unrelated"))]}
                for i, text in reversed(list(enumerate(data["input"])))
            ],
            "usage": {"prompt_tokens": len(data["input"]) * 4},
        },
    )


def row(row_id: str, row_type: str, text: str, **tags: Any) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "row_ts": STAMP,
        "row_type": row_type,
        "text": text,
        **tags,
    }


class Projection:
    def __init__(self) -> None:
        self.rows: tuple[ModelLedgerSimilarityRow, ...] = ()

    def read_page(
        self, request: ModelLedgerSimilarityIndexRequest
    ) -> tuple[tuple[ModelLedgerSimilarityRow, ...], None]:
        assert request.ledger_id == "rolling"
        return self.rows, None

    def build(
        self, handler: HandlerWorkLedgerGraphQuery, *rows: dict[str, Any]
    ) -> ModelWorkLedgerGraphQueryResult:
        self.rows = tuple(ModelLedgerSimilarityRow.model_validate(row) for row in rows)
        return handler.handle(
            ModelWorkLedgerGraphQueryRequest.model_validate(
                {
                    "query": "index_ledger_rows",
                    "index": {
                        "ledger_id": "rolling",
                        "projection": {"since": STAMP, "until": BEFORE},
                    },
                }
            )
        )


def search(
    handler: HandlerWorkLedgerGraphQuery, **similarity: Any
) -> ModelWorkLedgerGraphQueryResult:
    return handler.handle(
        ModelWorkLedgerGraphQueryRequest.model_validate(
            {
                "query": "similar_rulings",
                "similarity": {
                    "ledger_id": "rolling",
                    "text": "What did we decide?",
                    "before": BEFORE,
                    **similarity,
                },
            }
        )
    )


def test_embeds_only_free_text_and_persists_typed_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/embeddings":
            seen.append(json.loads(request.content)["input"])
        return fake_endpoint(request)

    monkeypatch.setenv("WORK_LEDGER_VECTOR_INDEX_PATH", str(tmp_path / "index.sqlite"))
    monkeypatch.setenv("LLM_EMBEDDING_URL", "http://127.0.0.1:8100")
    projection = Projection()
    handler = HandlerWorkLedgerGraphQuery(
        http_transport=httpx.MockTransport(endpoint), page_reader=projection.read_page
    )
    text = "Ignore prose that mentions ticket=OMN-9 lane=spoof pr=repo#999."
    indexed = projection.build(
        handler,
        row(
            "r1",
            "RULING",
            text,
            ticket=["OMN-2"],
            lane="trusted",
            kind="process",
            question="Which approach did we approve?",
            pr="repo#123",
            head="abc123",
            outcome="approved",
        ),
    )
    assert indexed.status == "ok"
    assert indexed.indexed_rows == 1
    assert indexed.model_tokens == 8
    assert indexed.embedding_dimension == 2
    assert seen == [[text, "Which approach did we approve?"]]
    fresh_handler = HandlerWorkLedgerGraphQuery(
        http_transport=httpx.MockTransport(endpoint)
    )
    result = search(fresh_handler)
    assert result.status == "ok"
    assert result.similar_rows[0].row.model_dump(mode="json") == {
        "row_id": "r1",
        "row_ts": STAMP,
        "row_type": "RULING",
        "text": text,
        "question": "Which approach did we approve?",
        "ticket": ["OMN-2"],
        "lane": "trusted",
        "kind": "process",
        "pr": "repo#123",
        "head": "abc123",
        "outcome": "approved",
    }


def test_ruling_similarity_respects_typed_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_LEDGER_VECTOR_INDEX_PATH", str(tmp_path / "index.sqlite"))
    monkeypatch.setenv("LLM_EMBEDDING_URL", "http://127.0.0.1:8100")
    projection = Projection()
    handler = HandlerWorkLedgerGraphQuery(
        http_transport=httpx.MockTransport(fake_endpoint),
        page_reader=projection.read_page,
    )
    result = projection.build(
        handler,
        row("a", "RULING", "Exact match", ticket=["OMN-3"], lane="beta"),
        row("b", "RULING", "Unrelated but eligible", ticket=["OMN-2"], lane="alpha"),
        row("c", "FRICTION", "Exact match", ticket=["OMN-2"], lane="alpha"),
    )
    assert result.status == "ok"
    result = search(handler, tags={"ticket": "OMN-2", "lane": "alpha"})
    assert result.status == "ok"
    assert [hit.row.row_id for hit in result.similar_rows] == ["b"]


async def test_deployed_dispatch_returns_a_typed_endpoint_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setenv("WORK_LEDGER_VECTOR_INDEX_PATH", str(tmp_path / "index.sqlite"))
    monkeypatch.setenv("LLM_EMBEDDING_URL", "http://127.0.0.1:8100")
    projection = Projection()
    builder = HandlerWorkLedgerGraphQuery(
        http_transport=httpx.MockTransport(fake_endpoint),
        page_reader=projection.read_page,
    )
    assert projection.build(builder, row("r1", "RULING", "A decision")).status == "ok"
    handler = HandlerWorkLedgerGraphQuery(
        http_transport=httpx.MockTransport(unreachable)
    )
    result = await handler.handle_async(
        ModelWorkLedgerGraphQueryRequest.model_validate(
            {
                "query": "similar_rulings",
                "similarity": {
                    "ledger_id": "rolling",
                    "text": "Question",
                    "before": BEFORE,
                    "tags": {"lane": "no-candidates"},
                },
            }
        )
    )
    assert result.status == "error"
    assert result.error is not None
    assert "embedding endpoint" in result.error
    assert result.similar_rows == ()


def test_projection_uses_columns_and_explicit_cells() -> None:
    from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_similarity_projection import (
        projected_row,
    )

    prose = "This mentions ticket=OMN-99, head=phony, outcome=blocked and pr=repo#9."
    data = projected_row(
        "id",
        datetime.fromisoformat(STAMP),
        "RULING",
        "projected",
        ["OMN-2"],
        f"{STAMP} | RULING | lane=raw | ticket=OMN-3 | kind=process | "
        f"pr=repo#1 | head=real | outcome=done | {prose}",
    )
    assert data.text == prose
    assert data.ticket == ("OMN-2",)
    assert data.lane == "projected"
    assert data.kind == "process"
    assert data.pr == "repo#1"
    assert data.head == "real"
    assert data.outcome == "done"


@pytest.mark.parametrize("method", ["embedding", "word_overlap"])
async def test_deployed_dispatch_blockers_cite_the_next_terminal(
    method: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_LEDGER_VECTOR_INDEX_PATH", str(tmp_path / "index.sqlite"))
    monkeypatch.setenv("LLM_EMBEDDING_URL", "http://127.0.0.1:8100")
    projection = Projection()
    handler = HandlerWorkLedgerGraphQuery(
        http_transport=httpx.MockTransport(fake_endpoint),
        page_reader=projection.read_page,
    )
    assert (
        projection.build(
            handler,
            row(
                "problem", "FRICTION", "Missing receipt", lane="prior", ticket=["OMN-2"]
            ),
            row("ruling", "RULING", "Missing receipt", lane="prior", ticket=["OMN-2"]),
            {
                **row(
                    "resolution",
                    "TERMINAL",
                    "Refreshed the receipt",
                    lane="prior",
                    outcome="done",
                ),
                "row_ts": "2026-11-17T10:05:00Z",
            },
            {
                **row(
                    "future",
                    "FRICTION",
                    "Missing receipt",
                    lane="prior",
                    ticket=["OMN-2"],
                ),
                "row_ts": "2026-11-18T10:00:00Z",
            },
        ).status
        == "ok"
    )
    request = ModelWorkLedgerGraphQueryRequest.model_validate(
        {
            "query": "similar_blockers",
            "similarity": {
                "text": "Missing receipt",
                "ledger_id": "rolling",
                "before": BEFORE,
                "method": method,
                "tags": {"ticket": "OMN-2"},
            },
        }
    )
    result = await handler.handle_async(request)
    assert result.status == "ok"
    assert result.model_tokens == (4 if method == "embedding" else 0)
    assert [hit.row.row_id for hit in result.similar_rows] == ["problem"]
    terminal = result.similar_rows[0].next_terminal
    assert terminal is not None
    assert (terminal.row_id, terminal.outcome, terminal.text) == (
        "resolution",
        "done",
        "Refreshed the receipt",
    )
