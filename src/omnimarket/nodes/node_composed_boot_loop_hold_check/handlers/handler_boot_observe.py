# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe one composed-runtime boot: probe /health from the container start, and read the sampler's hold records.

The probe transport is a seam: the workflow reaches the kernel through ``docker exec`` and the tests reach a local server
over HTTP. Time is a seam too (``clock`` and ``sleep``), so a boot can be replayed without waiting for it.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_observation import (
    ModelHealthSample,
    ModelLoopHold,
)


class ProbeTransport(Protocol):
    """One /health GET. Returns the response body, or None when nothing answered within ``timeout_seconds``."""

    def fetch(self, timeout_seconds: float) -> str | None: ...


class HttpProbe:
    """GET a URL; a 503 with a body is an answer (a degraded kernel answered)."""

    def __init__(self, url: str) -> None:
        self._url = url

    def fetch(self, timeout_seconds: float) -> str | None:
        try:
            with urllib.request.urlopen(self._url, timeout=timeout_seconds) as response:
                return str(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            return body or None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return None


class DockerExecProbe:
    """GET a URL from inside a container with curl (the image carries it, and the existing health steps use it)."""

    def __init__(self, container: str, url: str) -> None:
        self._container = container
        self._url = url

    def fetch(self, timeout_seconds: float) -> str | None:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                self._container,
                "curl",
                "-sS",
                "--max-time",
                f"{timeout_seconds:g}",
                self._url,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds + 30.0,
        )
        body = completed.stdout.strip()
        return body if completed.returncode == 0 and body else None


def _is_ready(body: str) -> bool:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False
    return bool(
        isinstance(parsed, dict)
        and parsed.get("status") == "healthy"
        and isinstance(parsed.get("details"), dict)
        and parsed["details"].get("is_running") is True
    )


def probe_until_ready(
    transport: ProbeTransport,
    *,
    window_seconds: float,
    probe_timeout_seconds: float,
    interval_seconds: float,
    elapsed_at_start: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ModelHealthSample, ...]:
    """Probe every ``interval_seconds`` until the kernel reports healthy and running, or the window ends."""
    samples: list[ModelHealthSample] = []
    began = clock()
    while True:
        started = clock()
        body = transport.fetch(probe_timeout_seconds)
        finished = clock()
        samples.append(
            ModelHealthSample(
                offset_seconds=elapsed_at_start + (started - began),
                answered=body is not None,
                latency_seconds=finished - started,
            )
        )
        if body is not None and _is_ready(body):
            break
        if finished - began >= window_seconds:
            break
        sleep(interval_seconds)
    return tuple(samples)


def parse_hold_records(text: str) -> tuple[ModelLoopHold, ...]:
    """Sampler JSONL to holds; one hold id appears once per report, and its longest report wins."""
    best: dict[str, ModelLoopHold] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            hold = ModelLoopHold(
                hold_id=str(record["hold_id"]),
                held_seconds=float(record["held_seconds"]),
                call=str(record["call"]),
                stack=tuple(str(frame) for frame in record.get("stack", [])),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"sampler record line {number} is unreadable: {error}"
            ) from error
        known = best.get(hold.hold_id)
        if known is None or hold.held_seconds >= known.held_seconds:
            best[hold.hold_id] = hold
    return tuple(best.values())
