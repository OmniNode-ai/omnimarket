# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settings of an example-envelope node: its contract ``config``, then its overlay.

The contract carries every value. A deployment overlay document, named for the
node and bound through ``ProtocolOverlayDocument``, replaces the values it names.
Nothing is read from the process environment and no value is defaulted here: a
contract without the settings, or an overlay that does not validate, raises
``ValueError`` and the runtime refuses to mount the route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.models.model_envelope_example import (
    ModelEnvelopeExampleOverlay,
    ModelEnvelopeExampleSettings,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument

SETTINGS_KEY = "envelope_example"


def read_contract(contract_path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("contract.yaml is not a mapping")
    return loaded


def _fields(error: ValidationError) -> list[str]:
    return sorted(
        {
            ".".join(str(part) for part in item["loc"]) or "<document>"
            for item in error.errors(include_input=False, include_context=False)
        }
    )


def load_settings(
    contract_path: Path, overlay: ProtocolOverlayDocument | None
) -> ModelEnvelopeExampleSettings:
    """The contract's settings with the overlay document's named values applied."""
    contract = read_contract(contract_path)
    config = contract.get("config")
    raw = config.get(SETTINGS_KEY) if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        raise ValueError(f"contract config.{SETTINGS_KEY} is missing")
    try:
        base = ModelEnvelopeExampleSettings.model_validate(raw)
    except ValidationError as error:
        raise ValueError(
            f"contract config.{SETTINGS_KEY} is invalid: fields {_fields(error)}"
        ) from None
    if overlay is None:
        return base
    document = overlay.read(str(contract["name"]))
    if document is None:
        return base
    try:
        named = ModelEnvelopeExampleOverlay.model_validate(document).named_values()
        return ModelEnvelopeExampleSettings.model_validate(base.model_dump() | named)
    except ValidationError as error:
        raise ValueError(
            f"overlay document {contract['name']} is invalid: fields {_fields(error)}"
        ) from None


__all__ = ["SETTINGS_KEY", "load_settings", "read_contract"]
