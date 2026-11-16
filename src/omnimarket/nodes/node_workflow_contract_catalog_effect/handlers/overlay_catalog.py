# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workflow contract catalog read from a named overlay document.

The catalog is deployment data, not code: the overlay carries the document, the
node contract carries the command topics its entries may bind. Every read
re-validates the document, so a catalog that could weaken the boundary is
refused rather than advertised.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import ValidationError

from omnimarket.nodes.node_workflow_contract_catalog_effect.models.model_workflow_contract_catalog import (
    ModelAdvertisedWorkflowContract,
    ModelWorkflowContractDocument,
    ModelWorkflowContractEntry,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.protocols.protocol_workflow_contract_catalog import (
    WorkflowCatalogError,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument

# Canonical ONEX topic grammar, as a regex so this module holds no topic literal.
_CANONICAL_TOPIC = re.compile(
    r"^onex\.(evt|cmd|intent|dlq)\.[a-z0-9-]+\.[a-z0-9._-]+\.v[1-9]\d*$"
)
_COMMAND_TOPIC_SEGMENT = ".cmd."


class OverlayWorkflowContractCatalog:
    """Validate and serve the catalog document a lane overlay carries."""

    def __init__(
        self,
        *,
        documents: ProtocolOverlayDocument,
        document_name: str,
        command_topics: Mapping[str, str],
    ) -> None:
        self._documents = documents
        self._document_name = document_name
        self._command_topics: dict[str, str] = dict(command_topics)

    async def servable_contracts(self) -> tuple[ModelAdvertisedWorkflowContract, ...]:
        raw = self._documents.read(self._document_name)
        if raw is None:
            raise WorkflowCatalogError("the overlay carries no workflow catalog")
        if "workflows" not in raw:
            raise WorkflowCatalogError("the workflow catalog has no 'workflows' key")
        try:
            document = ModelWorkflowContractDocument.model_validate(raw)
        except ValidationError as error:
            raise WorkflowCatalogError("the workflow catalog is invalid") from error
        if not document.workflows:
            raise WorkflowCatalogError("the workflow catalog declares no workflows")

        servable: dict[str, ModelAdvertisedWorkflowContract] = {}
        seen: set[str] = set()
        for entry in document.workflows:
            if entry.workflow_type in seen:
                raise WorkflowCatalogError(
                    f"duplicate workflow_type {entry.workflow_type!r}"
                )
            seen.add(entry.workflow_type)
            self._check_topic(entry)
            # Fenced entries are validated like servable ones and never advertised.
            if entry.enabled:
                servable[entry.workflow_type] = ModelAdvertisedWorkflowContract(
                    workflow_type=entry.workflow_type,
                    contract_id=entry.contract_id,
                    payload_schema=entry.payload_schema,
                )
        if not servable:
            raise WorkflowCatalogError("every workflow in the catalog is fenced")
        return tuple(servable[name] for name in sorted(servable))

    def _check_topic(self, entry: ModelWorkflowContractEntry) -> None:
        topic = self._command_topics.get(entry.command_topic_constant)
        if topic is None:
            raise WorkflowCatalogError(
                f"workflow {entry.workflow_type!r} names an undeclared topic constant"
            )
        if not _CANONICAL_TOPIC.match(topic):
            raise WorkflowCatalogError(
                f"workflow {entry.workflow_type!r} binds a non-canonical topic"
            )
        if _COMMAND_TOPIC_SEGMENT not in topic:
            raise WorkflowCatalogError(
                f"workflow {entry.workflow_type!r} binds a non-command topic"
            )


__all__: list[str] = ["OverlayWorkflowContractCatalog", "WorkflowCatalogError"]
