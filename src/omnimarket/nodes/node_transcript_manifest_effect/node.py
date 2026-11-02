# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry for the filesystem effect surrounding manifest computation."""

from pathlib import Path

from .handlers.handler_transcript_manifest_source import HandlerTranscriptManifestSource


class NodeTranscriptManifestEffect(HandlerTranscriptManifestSource):
    contract_path = Path(__file__).with_name("contract.yaml")
