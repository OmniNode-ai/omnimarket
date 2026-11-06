# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the real frozen stage-one directory, a bus that records, a failure recorder."""

from __future__ import annotations

import hashlib
from pathlib import Path
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
