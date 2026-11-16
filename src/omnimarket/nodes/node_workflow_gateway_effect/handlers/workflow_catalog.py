# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validation and lookup of the workflow catalog the gateway serves."""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_catalog import (
    ModelWorkflowCatalog,
    ModelWorkflowCatalogEntry,
)

_COMMAND_TOPIC_RE = re.compile(
    r"^onex\.(evt|cmd|intent|dlq)\.[a-z0-9-]+\.[a-z0-9._-]+\.v[1-9]\d*$"
)


class WorkflowCatalogError(ValueError):
    """The catalog is malformed or a payload violates its schema."""


class WorkflowCatalog:
    """A validated catalog and its lookup methods (deny by default)."""

    def __init__(self, catalog: ModelWorkflowCatalog) -> None:
        self._catalog = catalog
        self._validate(catalog)

    @staticmethod
    def _validate(catalog: ModelWorkflowCatalog) -> None:
        seen: set[str] = set()
        for entry in catalog.workflows:
            if entry.workflow_type in seen:
                raise WorkflowCatalogError(
                    f"duplicate workflow_type: {entry.workflow_type!r}"
                )
            seen.add(entry.workflow_type)
            if not _COMMAND_TOPIC_RE.match(entry.command_topic):
                raise WorkflowCatalogError(
                    f"command_topic does not match the onex contract shape: {entry.command_topic!r}"
                )
            if ".cmd." not in entry.command_topic:
                raise WorkflowCatalogError(
                    f"command_topic must carry the .cmd. segment: {entry.command_topic!r}"
                )
        if not any(entry.enabled for entry in catalog.workflows):
            raise WorkflowCatalogError("catalog has no enabled entry")
        for entry in catalog.workflows:
            if not entry.enabled:
                continue
            _check_schema(entry)
            schema = entry.payload_schema
            if (
                not isinstance(schema, dict)
                or schema.get("additionalProperties") is not False
            ):
                raise WorkflowCatalogError(
                    f"enabled entry {entry.workflow_type!r} must declare "
                    "additionalProperties: false at the top level"
                )

    def resolve(self, workflow_type: str) -> ModelWorkflowCatalogEntry | None:
        """The enabled entry for ``workflow_type``, or ``None`` (deny by default)."""
        for entry in self._catalog.workflows:
            if entry.workflow_type == workflow_type and entry.enabled:
                return entry
        return None

    def fenced(self, workflow_type: str) -> ModelWorkflowCatalogEntry | None:
        """The disabled (fenced) entry for ``workflow_type``, or ``None``."""
        for entry in self._catalog.workflows:
            if entry.workflow_type == workflow_type and not entry.enabled:
                return entry
        return None

    def allowed(self) -> tuple[str, ...]:
        """The sorted workflow types that are enabled."""
        return tuple(
            sorted(
                entry.workflow_type
                for entry in self._catalog.workflows
                if entry.enabled
            )
        )

    def contracts(self) -> tuple[ModelWorkflowCatalogEntry, ...]:
        """The enabled entries, sorted by workflow_type."""
        enabled = [entry for entry in self._catalog.workflows if entry.enabled]
        return tuple(sorted(enabled, key=lambda entry: entry.workflow_type))


def _check_schema(entry: ModelWorkflowCatalogEntry) -> None:
    """Refuse a payload schema that is not itself a valid JSON Schema."""
    try:
        Draft202012Validator.check_schema(entry.payload_schema)
    except SchemaError as exc:
        raise WorkflowCatalogError(
            f"invalid payload_schema for {entry.workflow_type!r}: {exc.message}"
        ) from exc


def validate_workflow_payload(
    entry: ModelWorkflowCatalogEntry, payload: Any
) -> list[dict[str, str]]:
    """Validate ``payload`` against ``entry.payload_schema``.

    Returns one ``{"field", "message"}`` per violation, sorted by field then
    message; an empty list means the payload is valid. A schema that is itself
    invalid raises ``WorkflowCatalogError``.
    """
    _check_schema(entry)
    validator = Draft202012Validator(entry.payload_schema)
    errors = sorted(
        (
            {
                "field": ".".join(str(part) for part in error.absolute_path) or "$",
                "message": error.message,
            }
            for error in validator.iter_errors(payload)
        ),
        key=lambda item: (item["field"], item["message"]),
    )
    return errors


__all__: list[str] = [
    "WorkflowCatalog",
    "WorkflowCatalogError",
    "validate_workflow_payload",
]
