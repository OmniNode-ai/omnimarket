# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Index and search ledger prose with the dev-local embedding service."""

from __future__ import annotations

import heapq
import math
import os
import re
import sqlite3
import struct
import time
from collections.abc import Callable
from contextlib import closing
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    EnumWorkLedgerGraphQueryStatus,
    ModelWorkLedgerGraphQueryRequest,
    ModelWorkLedgerGraphQueryResult,
)
from omnimarket.models.work_ledger_graph.model_work_ledger_similarity import (
    EnumLedgerSimilarityOperation,
    ModelLedgerSimilarityHit,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_similarity_projection import (
    LedgerSimilarityPageReader,
    read_page,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_vector_index import (
    candidates,
    index_model,
    next_terminal,
    open_index,
    write_page,
)

_WORDS = re.compile(r"[\w-]+")
_EMBEDDING_BATCH_SIZE = 32


class _EmbeddingModel(BaseModel):
    id: str = Field(min_length=1)


class _EmbeddingModelList(BaseModel):
    data: list[_EmbeddingModel] = Field(min_length=1, max_length=1)


class _EmbeddingVector(BaseModel):
    index: int
    embedding: tuple[float, ...] = Field(min_length=1)


class _EmbeddingUsage(BaseModel):
    prompt_tokens: int = Field(ge=0)


class _EmbeddingResponse(BaseModel):
    model: str
    data: list[_EmbeddingVector]
    usage: _EmbeddingUsage


def _local_base_url() -> str:
    raw_url = os.environ.get("LLM_EMBEDDING_URL", "")  # contract-config-ok: config
    try:
        url = urlsplit(raw_url)
        address = ip_address(url.hostname or "")
        if (
            url.scheme not in {"http", "https"}
            or not (address.is_private or address.is_loopback)
            or url.username is not None
            or url.query
            or url.fragment
            or url.path.rstrip("/") not in {"", "/v1"}
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "embedding endpoint must be a configured local IP URL"
        ) from None
    return f"{url.scheme}://{url.netloc}/v1"


def _index_path() -> Path:
    configured = os.environ.get(
        "WORK_LEDGER_VECTOR_INDEX_PATH"
    )  # contract-config-ok: config
    if configured:
        return Path(configured)
    state_dir = os.environ.get("ONEX_STATE_DIR")  # contract-config-ok: config
    if not state_dir:
        raise ValueError("WORK_LEDGER_VECTOR_INDEX_PATH or ONEX_STATE_DIR must be set")
    return Path(state_dir) / "work-ledger-vectors.sqlite"


def _normalized(vector: tuple[float, ...]) -> tuple[float, ...]:
    magnitude = math.hypot(*vector)
    if not math.isfinite(magnitude) or not magnitude:
        raise ValueError("embedding endpoint returned a zero or non-finite vector")
    return tuple(value / magnitude for value in vector)


def _served_model(client: httpx.Client) -> str:
    response = client.get("models")
    response.raise_for_status()
    return _EmbeddingModelList.model_validate_json(response.content).data[0].id


def _embed(
    client: httpx.Client, model: str, texts: list[str]
) -> tuple[tuple[tuple[float, ...], ...], int]:
    response = client.post("embeddings", json={"model": model, "input": texts})
    response.raise_for_status()
    payload = _EmbeddingResponse.model_validate_json(response.content)
    vectors = sorted(payload.data, key=lambda item: item.index)
    if payload.model != model or [item.index for item in vectors] != list(
        range(len(texts))
    ):
        raise ValueError("embedding endpoint returned a different model or input count")
    return (
        tuple(_normalized(item.embedding) for item in vectors),
        payload.usage.prompt_tokens,
    )


def _cosine_score(vector: tuple[float, ...]) -> Callable[[str, bytes], float]:
    def score(_text: str, stored: bytes) -> float:
        values = struct.unpack(f"<{len(vector)}f", stored)
        similarity = math.fsum(a * b for a, b in zip(vector, values, strict=True))
        return max(-1.0, min(1.0, similarity))

    return score


def _word_score(query_text: str) -> Callable[[str, bytes], float]:
    terms = set(_WORDS.findall(query_text.casefold()))

    def score(text: str, _stored: bytes) -> float:
        return float(len(terms.intersection(_WORDS.findall(text.casefold()))))

    return score


class WorkLedgerSimilarity:
    def __init__(
        self,
        http_transport: httpx.BaseTransport | None,
        page_reader: LedgerSimilarityPageReader | None,
    ) -> None:
        self._http_transport = http_transport
        self._read_page = page_reader or read_page

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=_local_base_url(),
            transport=self._http_transport,
            trust_env=False,
            follow_redirects=False,
            timeout=30.0,
        )

    def handle(
        self, request: ModelWorkLedgerGraphQueryRequest
    ) -> ModelWorkLedgerGraphQueryResult:
        started = time.perf_counter()
        try:
            path = _index_path()
            if request.query is EnumLedgerSimilarityOperation.INDEX_LEDGER_ROWS:
                result = self._index(request, path)
            else:
                result = self._search(request, path)
        except httpx.HTTPError:
            result = self._error(request, "local embedding endpoint request failed")
        except (sqlite3.Error, struct.error, OSError):
            result = self._error(
                request, "work ledger vector index read or write failed"
            )
        except ValueError as exc:
            # ValidationError may include the input text, DSN or returned payload.
            message = (
                str(exc)
                if type(exc) is ValueError
                else "invalid ledger or embedding data"
            )
            result = self._error(request, message)
        return result.model_copy(
            update={"elapsed_ms": (time.perf_counter() - started) * 1000.0}
        )

    @staticmethod
    def _error(
        request: ModelWorkLedgerGraphQueryRequest, message: str
    ) -> ModelWorkLedgerGraphQueryResult:
        return ModelWorkLedgerGraphQueryResult(
            query=request.query,
            status=EnumWorkLedgerGraphQueryStatus.ERROR,
            error=message,
        )

    def _index(
        self, request: ModelWorkLedgerGraphQueryRequest, path: Path
    ) -> ModelWorkLedgerGraphQueryResult:
        if request.index is None:
            raise ValueError("request names no index input")
        rows, cursor = self._read_page(request.index)
        inputs = [
            (row.row_id, field, value)
            for row in sorted(rows, key=lambda item: item.row_id)
            for field, value in (("text", row.text), ("question", row.question))
            if value and value.strip()
        ]
        model = None
        dimension = None
        tokens = 0
        by_source: dict[tuple[str, str], tuple[float, ...]] = {}
        if inputs:
            with self._client() as client:
                model = _served_model(client)
                for offset in range(0, len(inputs), _EMBEDDING_BATCH_SIZE):
                    page = inputs[offset : offset + _EMBEDDING_BATCH_SIZE]
                    vectors, used = _embed(client, model, [text for _, _, text in page])
                    tokens += used
                    dimension = dimension or len(vectors[0])
                    if any(len(vector) != dimension for vector in vectors):
                        raise ValueError(
                            "embedding endpoint returned inconsistent dimensions"
                        )
                    by_source.update(
                        ((row_id, field), vector)
                        for (row_id, field, _), vector in zip(
                            page, vectors, strict=True
                        )
                    )
        if rows:
            write_page(path, request.index.ledger_id, rows, by_source, model, dimension)
        return ModelWorkLedgerGraphQueryResult(
            query=request.query,
            status=EnumWorkLedgerGraphQueryStatus.OK,
            indexed_rows=len({row_id for row_id, _ in by_source}),
            rows_without_text=sum(not row.text.strip() for row in rows),
            next_cursor=cursor,
            embedding_model=model,
            embedding_dimension=dimension,
            model_tokens=tokens,
        )

    def _search(
        self, request: ModelWorkLedgerGraphQueryRequest, path: Path
    ) -> ModelWorkLedgerGraphQueryResult:
        search = request.similarity
        if search is None:
            raise ValueError("request names no similarity input")
        ruling = request.query is EnumLedgerSimilarityOperation.SIMILAR_RULINGS
        tokens = 0
        model = None
        dimension = None
        with closing(open_index(path)) as conn:
            if search.method == "embedding":
                model, dimension = index_model(conn)
                with self._client() as client:
                    (vector,), tokens = _embed(client, model, [search.text])
                if len(vector) != dimension:
                    raise ValueError(
                        "query embedding dimension differs from vector index"
                    )
                score = _cosine_score(vector)
            else:
                score = _word_score(search.text)
            conn.execute("BEGIN")
            scored = (
                (
                    max(
                        score(text, blob)
                        for text, blob in (
                            (row.text, vector_blob),
                            (row.question or "", question_blob),
                        )
                        if blob is not None
                    ),
                    row,
                )
                for row, vector_blob, question_blob in candidates(
                    conn, search, ruling=ruling
                )
            )
            ranked = heapq.nlargest(
                search.limit,
                (
                    pair
                    for pair in scored
                    if search.method == "embedding" or pair[0] > 0
                ),
                key=lambda pair: (pair[0], pair[1].row_ts, pair[1].row_id),
            )
            hits = tuple(
                ModelLedgerSimilarityHit(
                    row=row,
                    score=value,
                    next_terminal=None if ruling else next_terminal(conn, search, row),
                )
                for value, row in ranked
            )
        return ModelWorkLedgerGraphQueryResult(
            query=request.query,
            status=EnumWorkLedgerGraphQueryStatus.OK,
            model_tokens=tokens,
            embedding_model=model,
            embedding_dimension=dimension,
            similar_rows=hits,
        )
