# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Floor read routes: the floor's JSON over HTTP, and an in-memory floor."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from omnimarket.nodes.node_ask_done_check_effect.protocols import ModelFloorRead


def floor_from_json(document: object) -> ModelFloorRead:
    if not isinstance(document, dict) or not isinstance(document.get("pieces"), list):
        return ModelFloorRead(error="floor-json-without-pieces")
    pieces: dict[int, str] = {}
    for item in document["pieces"]:
        if isinstance(item, dict) and isinstance(item.get("piece"), int):
            pieces[item["piece"]] = str(item.get("status", ""))
    generated = document.get("generated_at")
    return ModelFloorRead(
        pieces=pieces, generated_at=generated if isinstance(generated, str) else None
    )


class HttpFloorRead:
    def __init__(self, url: str, timeout_seconds: int) -> None:
        self._url = url
        self._timeout = timeout_seconds

    def read(self) -> ModelFloorRead:
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout) as response:
                return floor_from_json(json.loads(response.read().decode("utf-8")))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return ModelFloorRead(error=f"floor-unreadable:{type(exc).__name__}")


class InMemoryFloorRead:
    def __init__(self, floor: ModelFloorRead) -> None:
        self._floor = floor

    def read(self) -> ModelFloorRead:
        return self._floor


__all__ = ["HttpFloorRead", "InMemoryFloorRead", "floor_from_json"]
