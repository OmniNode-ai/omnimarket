# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the scan overlay: ``transcript_roots``, ``manifest_hash_algorithm``, ``read_max_bytes``."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptScanOverlay,
)

OVERLAY_ID = "node_transcript_session_scan_effect"


def load_scan_overlay(
    environ: Mapping[str, str] | None = None,
    *,
    explicit: object | None = None,
) -> ModelTranscriptScanOverlay:
    """Load and validate the overlay; a bucket root without a credential reference refuses at load."""
    return load_structured_overlay(
        OVERLAY_ID, ModelTranscriptScanOverlay, environ, explicit=explicit
    )
