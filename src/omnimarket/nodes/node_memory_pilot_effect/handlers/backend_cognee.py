# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cognee behind the backend protocol: self-hosted, in-process stores, lab models only.

The package is imported lazily, after the environment is set from the overlay,
because Cognee reads its settings when it is first imported. It is not a
dependency of omnimarket; only a host that installed it can use this backend.
"""

from __future__ import annotations

import os
from typing import Any

from omnimarket.nodes.node_memory_pilot_effect.handlers.chat_answerer import (
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
)

_CONNECTION_MARKERS = (
    "Connection",
    "Timeout",
    "Unavailable",
    "ConnectError",
    "RemoteProtocol",
)


def cognee_environment(overlay: ModelMemoryPilotOverlay) -> dict[str, str]:
    """The environment Cognee reads, built only from the overlay. Telemetry is off."""
    if (
        overlay.chat is None
        or overlay.embeddings is None
        or overlay.storage_root is None
    ):
        raise ValueError("cognee overlay incomplete")
    root = overlay.storage_root
    return {
        "TELEMETRY_DISABLED": "1",
        "DO_NOT_TRACK": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "HF_HUB_OFFLINE": "1",
        "TIKTOKEN_CACHE_DIR": str(root / "tiktoken"),
        "LLM_PROVIDER": "custom",
        "LLM_MODEL": f"openai/{overlay.chat.model}",
        "LLM_ENDPOINT": overlay.chat.base_url,
        "LLM_API_KEY": overlay.api_key_placeholder,
        "LLM_TEMPERATURE": str(overlay.temperature),
        "LLM_MAX_COMPLETION_TOKENS": str(overlay.max_completion_tokens),
        "EMBEDDING_PROVIDER": "openai_compatible",
        "EMBEDDING_MODEL": overlay.embeddings.model,
        "EMBEDDING_ENDPOINT": overlay.embeddings.base_url,
        "EMBEDDING_API_KEY": overlay.api_key_placeholder,
        "EMBEDDING_DIMENSIONS": str(overlay.embedding_dimensions),
        "DATA_ROOT_DIRECTORY": str(root / "data"),
        "SYSTEM_ROOT_DIRECTORY": str(root / "system"),
        "CACHE_ROOT_DIRECTORY": str(root / "cache"),
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


class BackendCognee:
    """Add, cognify and GRAPH_COMPLETION search through the installed Cognee package."""

    name = "cognee"

    def __init__(self, overlay: ModelMemoryPilotOverlay, *, system_prompt: str) -> None:
        self._overlay = overlay
        self._system_prompt = system_prompt
        self._cognee: Any = None

    def _load(self) -> Any:
        if self._cognee is None:
            os.environ.update(cognee_environment(self._overlay))
            import cognee  # type: ignore[import-not-found,unused-ignore]

            self._cognee = cognee
        return self._cognee

    def _probe_models(self) -> None:
        for endpoint in self._overlay.endpoints():
            probe_endpoint(endpoint)

    async def ingest(
        self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
    ) -> int:
        self._probe_models()
        cognee = self._load()
        try:
            await cognee.add(
                [doc.text for doc in documents],
                dataset_name=dataset,
                node_set=None,
            )
            runs = await cognee.cognify(datasets=[dataset])
        except Exception as exc:
            if _is_connection_failure(exc):
                raise LabModelUnavailableError(type(exc).__name__) from None
            raise
        incomplete = sorted(
            type(run).__name__
            for run in (runs or {}).values()
            if type(run).__name__ != "PipelineRunCompleted"
        )
        if not runs or incomplete:
            raise RuntimeError(
                f"cognify did not complete: {incomplete or 'no run returned'}"
            )
        return len(documents)

    async def query(self, dataset: str, question: str) -> BackendAnswer:
        self._probe_models()
        cognee = self._load()
        try:
            rows = await cognee.search(
                query_text=question,
                query_type=cognee.SearchType.GRAPH_COMPLETION,
                datasets=[dataset],
                system_prompt=self._system_prompt,
            )
        except Exception as exc:
            if _is_connection_failure(exc):
                raise LabModelUnavailableError(type(exc).__name__) from None
            raise
        texts: list[str] = []
        for row in rows or []:
            result = row.get("search_result") if isinstance(row, dict) else row
            if isinstance(result, list):
                texts.extend(str(item) for item in result)
            elif result is not None:
                texts.append(str(result))
        answer = "\n".join(t.strip() for t in texts if t.strip())
        return BackendAnswer(
            answer,
            (ModelMemoryPilotContextRef(source="cognee-graph", excerpt=answer[:500]),),
        )
