# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the real frozen stage-one directory, a bus that records, a failure recorder."""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelLabEndpoint,
    ModelMemoryPilotDocument,
    ModelMemoryPilotOverlay,
)

PILOT_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_memory_pilot_effect/pilot/stage1"
)
CORR = UUID("00000000-0000-4000-a000-0000000000b1")


class RecordingBus:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[str, bytes | None, bytes]] = []
        self._fail = fail

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        if self._fail:
            raise ConnectionError("bus down")
        self.events.append((topic, key, value))

    @property
    def topics(self) -> list[str]:
        return [t.rsplit("-", 1)[-1].removesuffix(".v1") for t, _k, _v in self.events]


class RecordingRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    async def record_failure(
        self, correlation_id: str, error_code: str, detail: str
    ) -> None:
        self.rows.append((correlation_id, error_code, detail))


def frozen_hash(pilot_dir: Path = PILOT_DIR) -> str:
    return hashlib.sha256((pilot_dir / "FROZEN.sha256").read_bytes()).hexdigest()


def make_overlay(
    *,
    backend: str = "in_memory",
    pilot_dir: Path = PILOT_DIR,
    chat_url: str = "http://127.0.0.1:9/v1",
    allowed: tuple[str, ...] = ("127.0.0.1",),
    frozen: str | None = None,
) -> ModelMemoryPilotOverlay:
    chat = ModelLabEndpoint(base_url=chat_url, model="lab-chat", timeout_seconds=2)
    return ModelMemoryPilotOverlay.model_validate(
        {
            "backend": backend,
            "frozen_hash": frozen or frozen_hash(pilot_dir),
            "pilot_dir": pilot_dir,
            "telemetry_disabled": True,
            "allowed_hosts": allowed,
            "chat": chat if backend != "in_memory" else None,
        }
    )


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def recorder() -> RecordingRecorder:
    return RecordingRecorder()


@pytest.fixture
def planted_documents() -> tuple[ModelMemoryPilotDocument, ...]:
    return tuple(
        ModelMemoryPilotDocument(
            doc_id=f"planted_notes/{p.name}", text=p.read_text(encoding="utf-8")
        )
        for p in sorted((PILOT_DIR / "planted_notes").glob("PN-*.txt"))
    )


def make_graphiti_overlay(
    storage_root: Path,
    *,
    chat_url: str = "http://127.0.0.1:9/v1",
    cap: int = 4,
) -> ModelMemoryPilotOverlay:
    """A graphiti overlay whose three endpoints are all on the loopback allow list."""
    return ModelMemoryPilotOverlay.model_validate(
        {
            "backend": "graphiti",
            "frozen_hash": frozen_hash(),
            "pilot_dir": PILOT_DIR,
            "telemetry_disabled": True,
            "allowed_hosts": ("127.0.0.1",),
            "chat": {"base_url": chat_url, "model": "chat-m", "timeout_seconds": 2},
            "chat_extraction": {
                "base_url": chat_url,
                "model": "chat-m",
                "timeout_seconds": 2,
            },
            "embeddings": {
                "base_url": chat_url,
                "model": "embed-m",
                "timeout_seconds": 2,
            },
            "embedding_dimensions": 8,
            "storage_root": storage_root,
            "lab_max_in_flight": cap,
            "graphiti_chunk_chars": 60,
            "graphiti_num_results": 3,
        }
    )


class FakeGraphiti:
    """Stands in for the Graphiti object: holds episodes in a list and returns canned facts."""

    def __init__(
        self, facts: tuple[str, ...] = (), *, add_error: Exception | None = None
    ) -> None:
        self.facts = facts
        self.add_error = add_error
        self.episodes: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []
        self.index_builds = 0
        outer = self

        class _Ops:
            async def build_indices_and_constraints(self, driver: object) -> None:
                outer.index_builds += 1

        class _Driver:
            graph_ops = _Ops()

            async def execute_query(
                self, query: str, **params: Any
            ) -> tuple[list[dict[str, int]], None, None]:
                group = params["dataset_group"]
                held = sum(1 for e in outer.episodes if e["group_id"] == group)
                return [{"n": held}], None, None

        self.driver = _Driver()

    async def add_episode(self, **kwargs: Any) -> None:
        if self.add_error is not None:
            raise self.add_error
        self.episodes.append(kwargs)

    async def search(
        self, query: str, group_ids: list[str], num_results: int
    ) -> list[Any]:
        self.searches.append(
            {"query": query, "group_ids": group_ids, "num_results": num_results}
        )
        return [SimpleNamespace(fact=f) for f in self.facts][:num_results]


@pytest.fixture
def lab_url() -> Iterator[str]:
    """A loopback port that accepts connections, so the reachability probe of a lab host passes."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        yield f"http://127.0.0.1:{server.getsockname()[1]}/v1"
