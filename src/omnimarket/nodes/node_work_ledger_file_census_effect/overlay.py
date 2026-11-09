# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_work_ledger_file_census_effect: every deployment fact, none in code.

Found at ``<root>/node_work_ledger_file_census_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_LEDGER_CENSUS_OVERLAY`` names. The package ships
``overlay.example.yaml`` (the repository's own row only); the full registry list is deployment data
and stays outside this repository. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
)

NODE_NAME = "node_work_ledger_file_census_effect"
OVERLAY_FILE_ENV = "ONEX_LEDGER_CENSUS_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelCensusOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelCensusOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
