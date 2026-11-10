# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend profile: the effect node's file sources, projected and written where the floor and the fill read."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime

from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import run


class FileSourcesWorkStateProjector:
    """Runs node_work_state_projection_effect's read, project and atomic write once per call."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    def project(self, now: datetime) -> ModelWorkStateResult:
        return run(os.environ if self._environ is None else self._environ, now)
