# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public, declarative entry for the pure transcript manifest operation."""

from pathlib import Path

from .handlers.handler_transcript_manifest import HandlerTranscriptManifest


class NodeContextCompactionManifestCompute(HandlerTranscriptManifest):
    contract_path = Path(__file__).with_name("contract.yaml")
