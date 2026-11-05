# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graphiti behind the backend protocol: self-hosted, embedded FalkorDB graph, lab models only.

Graphiti retrieves facts; the injected lab answerer writes the answer from them with the
frozen prompt, the same form as the stage-one baseline. The package is imported lazily,
after the environment is set from the overlay, because Graphiti reads settings when it
is first imported. It is not a dependency of omnimarket; only a host that installed it
can use this backend. The embedded store is FalkorDBLite: a private server process on a
Unix socket, no TCP port, saved to the overlay's storage root on request.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_memory_pilot_effect.handlers.graphiti_chunking import (
    chunk_text,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.lab_reachability import (
    probe_endpoint,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotContextRef,
    ModelMemoryPilotDocument,
    ModelMemoryPilotOverlay,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    BackendAnswer,
    LabModelUnavailableError,
    ProtocolChatAnswerer,
)

_log = logging.getLogger(__name__)

_CONNECTION_MARKERS = (
    "Connection",
    "Timeout",
    "Unavailable",
    "ConnectError",
    "RemoteProtocol",
)


def graphiti_environment(overlay: ModelMemoryPilotOverlay) -> dict[str, str]:
    """The environment Graphiti reads, built only from the overlay. Telemetry is off."""
    if (
        overlay.chat is None
        or overlay.embeddings is None
        or overlay.storage_root is None
        or overlay.lab_max_in_flight is None
    ):
        raise ValueError("graphiti overlay incomplete")
    return {
        "GRAPHITI_TELEMETRY_ENABLED": "false",
        "DO_NOT_TRACK": "1",
        "HF_HUB_OFFLINE": "1",
        "SEMAPHORE_LIMIT": str(overlay.lab_max_in_flight),
    }


class IngestIncompleteError(Exception):
    """Some chunks failed in the extraction model; every other chunk is stored and saved."""


def _is_connection_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, (ConnectionError, TimeoutError)) or any(
            marker in type(cursor).__name__ for marker in _CONNECTION_MARKERS
        ):
            return True
        cursor = cursor.__cause__ or cursor.__context__
    return False


def _library(name: str) -> Any:
    """Import the optional package lazily, after the environment is set."""
    return importlib.import_module(name)


async def _no_rank(
    self: object, query: str, passages: list[str]
) -> list[tuple[str, float]]:
    """The pilot's search recipe never reranks with a model; a call would be a bug."""
    raise RuntimeError("the pilot's search recipe has no cross encoder")


def render_facts(facts: list[str]) -> str:
    """The context handed to the answerer: one retrieved fact per line."""
    return "\n".join(f"- {fact}" for fact in facts if fact.strip())


class BackendGraphiti:
    """Chunk and add episodes, then hybrid-search facts and let the injected answerer reply."""

    name = "graphiti"

    def __init__(
        self,
        overlay: ModelMemoryPilotOverlay,
        *,
        answerer: ProtocolChatAnswerer,
        system_prompt: str,
        graphiti: Any | None = None,
    ) -> None:
        self._overlay = overlay
        self._answerer = answerer
        self._system_prompt = system_prompt
        self._injected: Any = graphiti
        self._graphs: dict[str, Any] = {}
        self._store: Any = None
        self._clients: tuple[Any, Any] | None = None
        self._episode_source: Any = "text"

    def _probe_models(self) -> None:
        for endpoint in self._overlay.endpoints():
            probe_endpoint(endpoint)

    def _build_clients(self) -> tuple[Any, Any]:
        overlay = self._overlay
        assert overlay.embeddings is not None
        assert overlay.chat is not None
        llm_config_class = _library("graphiti_core.llm_client.config").LLMConfig
        llm_client_class = _library(
            "graphiti_core.llm_client.openai_generic_client"
        ).OpenAIGenericClient
        embedder_module = _library("graphiti_core.embedder.openai")
        extraction = overlay.chat_extraction or overlay.chat
        llm = llm_client_class(
            config=llm_config_class(
                api_key=overlay.api_key_placeholder,
                model=extraction.model,
                small_model=extraction.model,
                base_url=extraction.base_url,
                temperature=overlay.temperature,
                max_tokens=overlay.max_completion_tokens,
            ),
            max_tokens=overlay.max_completion_tokens,
        )
        embedder = embedder_module.OpenAIEmbedder(
            config=embedder_module.OpenAIEmbedderConfig(
                api_key=overlay.api_key_placeholder,
                base_url=overlay.embeddings.base_url,
                embedding_model=overlay.embeddings.model,
                embedding_dim=overlay.embedding_dimensions,
            )
        )
        return llm, embedder

    async def _open(self, dataset: str) -> Any:
        """The Graphiti object of one dataset, which is one graph of the embedded store."""
        if self._injected is not None:
            return self._injected
        if dataset in self._graphs:
            return self._graphs[dataset]
        overlay = self._overlay
        assert overlay.storage_root is not None
        os.environ.update(graphiti_environment(overlay))
        overlay.storage_root.mkdir(parents=True, exist_ok=True)
        if self._store is None:
            self._store = _library("redislite.async_falkordb_client").AsyncFalkorDB(
                str(overlay.storage_root / "falkordb.rdb")
            )
            self._clients = self._build_clients()
            self._episode_source = _library("graphiti_core.nodes").EpisodeType.text
        assert self._clients is not None
        llm, embedder = self._clients
        cross_encoder_class = type(
            "NoCrossEncoder",
            (_library("graphiti_core.cross_encoder.client").CrossEncoderClient,),
            {"rank": _no_rank},
        )
        driver = _library("graphiti_core.driver.falkordb_driver").FalkorDriver(
            falkor_db=self._store, database=dataset
        )
        graphiti = _library("graphiti_core").Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=cross_encoder_class(),
            max_coroutines=overlay.lab_max_in_flight,
        )
        self._graphs[dataset] = graphiti
        return graphiti

    async def save(self) -> None:
        """Write the embedded store to disk; closing it does not (redislite's async close skips the save)."""
        if self._store is not None:
            await self._store.client.execute_command("SAVE")

    async def close(self) -> None:
        """Stop the embedded server process."""
        if self._store is not None:
            await self._store.close()
            self._store = None
            self._graphs.clear()

    async def _episode_count(self, graphiti: Any, dataset: str) -> int:
        try:
            rows, _, _ = await graphiti.driver.execute_query(
                "MATCH (e:Episodic {group_id: $dataset_group}) RETURN count(e) AS n",
                dataset_group=dataset,
            )
        except Exception as exc:
            if "empty key" in str(exc):
                return 0
            raise
        return int(rows[0]["n"]) if rows else 0

    async def _episode_exists(self, graphiti: Any, dataset: str, name: str) -> bool:
        rows, _, _ = await graphiti.driver.execute_query(
            "MATCH (e:Episodic {group_id: $dataset_group, name: $episode_name}) "
            "RETURN count(e) AS n",
            dataset_group=dataset,
            episode_name=name,
        )
        return bool(rows) and int(rows[0]["n"]) > 0

    async def ingest(
        self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
    ) -> int:
        """Add every chunk not already stored; a chunk the extraction model cannot do is recorded.

        Re-running resumes: a stored chunk is skipped. Chunks that fail after the library's own
        retries do not stop the others, and the call then raises IngestIncompleteError naming them.
        """
        self._probe_models()
        graphiti = await self._open(dataset)
        chars = self._overlay.graphiti_chunk_chars
        assert chars is not None
        reference_time = datetime.now(UTC)
        failed: list[str] = []
        try:
            if await self._episode_count(graphiti, dataset) == 0:
                await graphiti.build_indices_and_constraints()
            for document in documents:
                for number, chunk in enumerate(chunk_text(document.text, chars), 1):
                    name = f"{document.doc_id}#{number}"
                    if await self._episode_exists(graphiti, dataset, name):
                        _log.info("graphiti episode %s already stored", name)
                        continue
                    began = time.monotonic()
                    try:
                        await graphiti.add_episode(
                            name=name,
                            episode_body=chunk,
                            source=self._episode_source,
                            source_description=document.doc_id,
                            reference_time=reference_time,
                            group_id=dataset,
                        )
                    except Exception as exc:
                        if _is_connection_failure(exc):
                            raise
                        failed.append(name)
                        _log.error(
                            "graphiti episode %s failed after %.1f s: %s",
                            name,
                            time.monotonic() - began,
                            type(exc).__name__,
                        )
                        continue
                    _log.info(
                        "graphiti episode %s added in %.1f s",
                        name,
                        time.monotonic() - began,
                    )
                await self.save()
        except Exception as exc:
            if _is_connection_failure(exc):
                raise LabModelUnavailableError(type(exc).__name__) from None
            raise
        if failed:
            raise IngestIncompleteError(f"{len(failed)} chunks failed: {failed}")
        return len(documents)

    async def query(self, dataset: str, question: str) -> BackendAnswer:
        self._probe_models()
        graphiti = await self._open(dataset)
        limit = self._overlay.graphiti_num_results
        assert limit is not None
        try:
            if await self._episode_count(graphiti, dataset) == 0:
                raise KeyError(dataset)
            edges = await graphiti.search(
                question, group_ids=[dataset], num_results=limit
            )
        except KeyError:
            raise
        except Exception as exc:
            if _is_connection_failure(exc):
                raise LabModelUnavailableError(type(exc).__name__) from None
            raise
        facts = [str(edge.fact) for edge in edges]
        answer = await self._answerer.answer(
            self._system_prompt, question, render_facts(facts)
        )
        refs = tuple(
            ModelMemoryPilotContextRef(source="graphiti-fact", excerpt=fact[:500])
            for fact in facts
        )
        return BackendAnswer(answer, refs)
