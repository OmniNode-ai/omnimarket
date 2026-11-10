# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live fact source of the floor fill: the floor record by URL, a plan text by file, and the brake and stop reads.

``fetch_url_text`` and ``read_text_file`` are the two real reads (a network or file URL, a local file); every other
behaviour takes them by constructor, so the golden tests run on scripted ones. The brake and the stop signal have no
live source yet: they are injected callables, and an absent one raises ``unbound:<source>`` so the pass fails closed
and names what is unbound.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    LINE_ID_PLACEHOLDER,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
)

_ALLOWED_SCHEMES = frozenset({"http", "https", "file"})


def fetch_url_text(url: str, timeout_seconds: float) -> str:
    """Read ``url`` (http, https or file) as utf-8 text; any other scheme is refused."""
    if urllib.parse.urlsplit(url).scheme not in _ALLOWED_SCHEMES:
        raise ValueError("scheme")
    with urllib.request.build_opener().open(url, timeout=timeout_seconds) as response:
        return str(response.read().decode("utf-8"))


def read_text_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class LiveFloorFacts:
    """``ProtocolFactSource`` over the floor URL, the plans root and the injected brake and stop reads."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        fetch_text: Callable[[str, float], str],
        read_file: Callable[[str], str],
        timeout_seconds: float,
        plan_file_pattern: str,
        brake: Callable[[], bool] | None,
        stop: Callable[[str], str | None] | None,
    ) -> None:
        self._fetch_text = fetch_text
        self._read_file = read_file
        self._timeout_seconds = timeout_seconds
        self._plan_file_pattern = plan_file_pattern
        self._brake = brake
        self._stop = stop

    def read_floor(self, url: str) -> str:
        try:
            return self._fetch_text(url, self._timeout_seconds)
        except Exception as error:
            raise FloorFillUnreadableError(type(error).__name__) from error

    def read_plan(self, plans_root: str, line_id: str) -> str:
        if (
            not line_id
            or "/" in line_id
            or "\\" in line_id
            or ".." in line_id
            or LINE_ID_PLACEHOLDER in line_id
        ):
            raise FloorFillUnreadableError("plan-path")
        path = os.path.join(plans_root, self._plan_file_pattern.format(line_id=line_id))
        try:
            return self._read_file(path)
        except Exception as error:
            raise FloorFillUnreadableError("plan-absent") from error

    def landing_only(self) -> bool:
        if self._brake is None:
            raise FloorFillUnreadableError("unbound:brake")
        return self._brake()

    def stop_refuses(self, task_class: str) -> str | None:
        if self._stop is None:
            raise FloorFillUnreadableError("unbound:stop-signal")
        return self._stop(task_class)


__all__: list[str] = ["LiveFloorFacts", "fetch_url_text", "read_text_file"]
