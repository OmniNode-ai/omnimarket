# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the three facts through the Temporal CLI: cluster health, and the pollers of the worker's task queue.

A CLI that is missing, times out or exits non-zero reads as the server not answering; a task queue with no listed
poller reads as the worker not polling. The newest poll is the latest ``lastAccessTime`` among the listed pollers.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from omnimarket.nodes.node_temporal_health_alarm_effect.models.model_temporal_health_alarm import (
    ModelTemporalHealthFacts,
    ModelTemporalHealthSettings,
)

# A runner takes an argv and returns (exit code, stdout, stderr).
Runner = Callable[[list[str], float], tuple[int, str, str]]


def run_cli(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as missing:
        return 127, "", f"not found: {missing.filename}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout_s:g}s"
    return done.returncode, done.stdout, done.stderr


def _access_times(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("lastAccessTime", "last_access_time") and isinstance(value, str):
                yield value
            else:
                yield from _access_times(value)
    elif isinstance(node, list):
        for item in node:
            yield from _access_times(item)


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _tail(text: str) -> str:
    return " ".join(text.split())[-160:]


def read_facts(
    settings: ModelTemporalHealthSettings,
    *,
    now: datetime | None = None,
    runner: Runner = run_cli,
) -> ModelTemporalHealthFacts:
    read_at = now or datetime.now(UTC)
    common = ["--address", settings.address, "--namespace", settings.namespace]
    code, out, err = runner(
        [
            settings.temporal_cli,
            "operator",
            "cluster",
            "health",
            "--address",
            settings.address,
        ],
        settings.cli_timeout_s,
    )
    if code != 0 or "SERVING" not in out.upper():
        return ModelTemporalHealthFacts(
            read_at=read_at,
            server_answers=False,
            worker_polling=False,
            detail=f"cluster health exit={code} {_tail(err or out)}".strip(),
        )
    code, out, err = runner(
        [
            settings.temporal_cli,
            "task-queue",
            "describe",
            "--task-queue",
            settings.task_queue,
            *common,
            "--output",
            "json",
        ],
        settings.cli_timeout_s,
    )
    if code != 0:
        return ModelTemporalHealthFacts(
            read_at=read_at,
            server_answers=True,
            worker_polling=False,
            detail=f"task-queue describe exit={code} {_tail(err or out)}".strip(),
        )
    try:
        document: object = json.loads(out or "null")
    except ValueError:
        document = None
    times = [t for t in (_parse_time(s) for s in _access_times(document)) if t]
    newest = max(times) if times else None
    return ModelTemporalHealthFacts(
        read_at=read_at,
        server_answers=True,
        worker_polling=bool(times),
        newest_poll_at=newest,
        detail=f"pollers={len(times)} task_queue={settings.task_queue}",
    )
