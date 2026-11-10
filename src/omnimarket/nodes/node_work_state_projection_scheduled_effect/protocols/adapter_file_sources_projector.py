# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend profile: the effect node's file sources, projected, written, and published on the bus (OMN-23427)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    publish_projection,
    run,
)


def _publish_off_loop(
    result: ModelWorkStateResult, env: Mapping[str, str], now: datetime
) -> str:
    """Publish the projection; inside a running event loop the publication runs on a thread of its own.

    publish_projection starts its own loop, which a running loop refuses: the runtime calls a handler from one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return publish_projection(result, env, now)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(publish_projection, result, env, now).result()


class FileSourcesWorkStateProjector:
    """Runs node_work_state_projection_effect's read, project and atomic write once per call, then publishes the projection.

    A bus that refuses is a reading, never a failed projection: the file is written, the answer is in ``publication``.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ
        self.publication = ""

    def project(self, now: datetime) -> ModelWorkStateResult:
        env = os.environ if self._environ is None else self._environ
        result = run(env, now)
        try:
            self.publication = _publish_off_loop(result, env, now)
        except Exception as exc:
            self.publication = f"not published ({type(exc).__name__}: {exc})"
        return result
