# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graphiti behind the backend protocol: self-hosted, embedded Kuzu graph, lab models only.

Graphiti retrieves facts; the injected lab answerer writes the answer from them with the
frozen prompt, the same form as the stage-one baseline. The package is imported lazily,
after the environment is set from the overlay, because Graphiti reads settings when it
is first imported. It is not a dependency of omnimarket; only a host that installed it
can use this backend.
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
        self._graphiti: Any = graphiti
        self._episode_source: Any = "text"

    def _probe_models(self) -> None:
        for endpoint in self._overlay.endpoints():
            probe_endpoint(endpoint)

    async def _open(self) -> Any:
        if self._graphiti is not None:
            return self._graphiti
        overlay = self._overlay
        assert overlay.storage_root is not None
        assert overlay.embeddings is not None
        assert overlay.chat is not None
        os.environ.update(graphiti_environment(overlay))
        overlay.storage_root.mkdir(parents=True, exist_ok=True)
        graphiti_class = _library("graphiti_core").Graphiti
        kuzu_class = _library("graphiti_core.driver.kuzu_driver").KuzuDriver
        embedder_module = _library("graphiti_core.embedder.openai")
        llm_config_class = _library("graphiti_core.llm_client.config").LLMConfig
        llm_client_class = _library(
            "graphiti_core.llm_client.openai_generic_client"
        ).OpenAIGenericClient
        self._episode_source = _library("graphiti_core.nodes").EpisodeType.text

        # graphiti-core 0.31.2: add_episode reads driver._database, which KuzuDriver never
        # sets. An empty name makes every group_id take the clone path, and the base clone
        # returns this same driver, so a one-database embedded store behaves as intended.
        driver_class = type("PilotKuzuDriver", (kuzu_class,), {"_database": ""})
        cross_encoder_class = type(
            "NoCrossEncoder",
            (_library("graphiti_core.cross_encoder.client").CrossEncoderClient,),
            {"rank": _no_rank},
        )

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
        # Kuzu allows one write transaction at a time, so the connection keeps its
        # default of one concurrent query; the model-call cap is max_coroutines below.
        driver = driver_class(db=str(overlay.storage_root / "kuzu.db"))
        self._graphiti = graphiti_class(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=cross_encoder_class(),
            max_coroutines=overlay.lab_max_in_flight,
        )
        return self._graphiti

    async def _episode_count(self, graphiti: Any, dataset: str) -> int:
        rows, _, _ = await graphiti.driver.execute_query(
            "MATCH (e:Episodic {group_id: $dataset_group}) RETURN count(e) AS n",
            dataset_group=dataset,
        )
        return int(rows[0]["n"]) if rows else 0

    async def ingest(
        self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
    ) -> int:
        self._probe_models()
        graphiti = await self._open()
        chars = self._overlay.graphiti_chunk_chars
        assert chars is not None
        reference_time = datetime.now(UTC)
        try:
            if await self._episode_count(graphiti, dataset) == 0:
                # KuzuDriver.build_indices_and_constraints is a no-op in 0.31.2 and its
                # schema holds no full-text index; the maintenance operation creates them.
                await graphiti.driver.graph_ops.build_indices_and_constraints(
                    graphiti.driver
                )
            for document in documents:
                for number, chunk in enumerate(chunk_text(document.text, chars), 1):
                    began = time.monotonic()
                    await graphiti.add_episode(
                        name=f"{document.doc_id}#{number}",
                        episode_body=chunk,
                        source=self._episode_source,
                        source_description=document.doc_id,
                        reference_time=reference_time,
                        group_id=dataset,
                    )
                    _log.info(
                        "graphiti episode %s#%d added in %.1f s",
                        document.doc_id,
                        number,
                        time.monotonic() - began,
                    )
        except Exception as exc:
            if _is_connection_failure(exc):
                raise LabModelUnavailableError(type(exc).__name__) from None
            raise
        return len(documents)

    async def query(self, dataset: str, question: str) -> BackendAnswer:
        self._probe_models()
        graphiti = await self._open()
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
