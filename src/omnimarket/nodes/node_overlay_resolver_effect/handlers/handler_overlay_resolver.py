# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve a skill's structured overlay from the overlay roots and validate it against its schema.

The path rule is ``<root>/<overlay id>/overlay.yaml`` with the schema beside it at
``<root>/<overlay id>/overlay.schema.json``; the first root that holds the overlay wins. The overlay id is the
skill name with each hyphen turned into an underscore. There is no default: an overlay that is absent, a schema
that is absent or broken, and an overlay the schema rejects all refuse, naming the overlay key or the schema
path. A refusal names the failing fields and never a document value, which may be a private deployment fact.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
    not_configured_message,
)
from omnimarket.nodes.node_overlay_resolver_effect.models.model_overlay_resolver import (
    ModelOverlayResolveRequest,
    ModelOverlayResolveResult,
)

SCHEMA_FILE_NAME = "overlay.schema.json"


def overlay_id_for(skill: str) -> str:
    """The underscore overlay id of a skill name."""
    return skill.replace("-", "_")


class HandlerOverlayResolver:
    """Resolve one overlay by skill name; refuse when it is absent or invalid."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    def handle(self, request: ModelOverlayResolveRequest) -> ModelOverlayResolveResult:
        overlay_id = overlay_id_for(request.skill)
        environ = (
            {OVERLAY_ROOTS_ENV: os.pathsep.join(request.roots)}
            if request.roots
            else (os.environ if self._environ is None else self._environ)
        )
        overlay_path = find_node_overlay(overlay_id, environ)
        if overlay_path is None:
            raise NodeOverlayError(
                not_configured_message(overlay_id, f"{overlay_id}/{OVERLAY_FILE_NAME}")
            )
        schema_path = overlay_path.with_name(SCHEMA_FILE_NAME)
        validator = _validator(schema_path)
        document = _read_overlay(overlay_path)
        try:
            fields = sorted(
                {
                    ".".join(str(part) for part in error.absolute_path) or "<document>"
                    for error in validator.iter_errors(document)
                }
            )
        except Unresolvable:
            raise NodeOverlayError(
                f"schema {schema_path} holds a $ref that does not resolve"
            ) from None
        if fields:
            raise NodeOverlayError(
                f"overlay {overlay_path} is invalid against schema {schema_path} "
                f"for {overlay_id}: fields {fields}"
            )
        if not isinstance(document, dict):  # the schema admitted a non-mapping
            raise NodeOverlayError(
                f"overlay {overlay_path} for {overlay_id} must be a mapping"
            )
        return ModelOverlayResolveResult(
            overlay_id=overlay_id,
            overlay_path=str(overlay_path),
            schema_path=str(schema_path),
            document=document,
        )


def _validator(schema_path: Path) -> Draft202012Validator:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NodeOverlayError(
            f"schema {schema_path} is absent: an overlay is validated against "
            f"{SCHEMA_FILE_NAME} beside it"
        ) from None
    except (OSError, UnicodeError, ValueError) as exc:
        raise NodeOverlayError(
            f"schema {schema_path} is unreadable ({type(exc).__name__})"
        ) from None
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise NodeOverlayError(
            f"schema {schema_path} is not a valid JSON Schema"
        ) from None
    return Draft202012Validator(schema)


def _read_overlay(overlay_path: Path) -> object:
    try:
        return yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise NodeOverlayError(
            f"overlay {overlay_path} is unreadable ({type(exc).__name__})"
        ) from None
