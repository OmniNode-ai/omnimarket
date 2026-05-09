# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""AdapterAdrIngestion — translates ProtocolAdrIngestion to node_adr_document_ingestion_effect.

This adapter lives inside the orchestrator package and is the ONLY place that
imports from the sibling node's private package. The orchestrator handler never
imports sibling node packages directly.
"""

from __future__ import annotations

import logging
from importlib import import_module

from omnimarket.models.adr import ModelAdrDocumentRef, ModelAdrIngestionResult

logger = logging.getLogger(__name__)


class AdapterAdrIngestion:
    """Default ProtocolAdrIngestion implementation using HandlerDocumentIngestion."""

    async def ingest(self, root_paths: list[str]) -> ModelAdrIngestionResult:
        from omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion import (
            HandlerDocumentIngestion,
        )

        ingestion_models = import_module(
            "omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_request"
        )
        model_ingestion_request = ingestion_models.ModelIngestionRequest

        handler = HandlerDocumentIngestion()
        result = await handler.handle(
            request=model_ingestion_request(root_paths=root_paths)
        )

        docs = [
            ModelAdrDocumentRef(
                source_path=str(getattr(doc, "source_path", "")),
                repo_name=str(getattr(doc, "repo_name", "")),
                file_size_bytes=int(getattr(doc, "file_size_bytes", 0)),
                source_content_sha256=str(getattr(doc, "source_content_sha256", "")),
            )
            for doc in getattr(result, "documents", [])
        ]
        return ModelAdrIngestionResult(documents=docs, root_paths=root_paths)


__all__: list[str] = ["AdapterAdrIngestion"]
