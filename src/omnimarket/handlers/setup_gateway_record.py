# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where `onex setup` records the gateway it started, and what it bound.

Setup's claude-gateway step writes the record; `onex claude enable` with no
flags reads it, so nobody types a gateway URL, model or credential reference
that setup already chose from the user's selected models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

#: The record's file name under the ONEX home folder.
GATEWAY_RECORD_NAME = "gateway-process.json"
_MAX_BYTES = 65537


class SetupGatewayBinding(NamedTuple):
    gateway_url: str
    model: str
    credential_ref: str


def gateway_record_path(onex_home: Path) -> Path:
    return onex_home / GATEWAY_RECORD_NAME


def read_setup_gateway_binding(onex_home: Path) -> SetupGatewayBinding | None:
    """The URL, model and credential reference setup bound, or None if none is set up."""
    path = gateway_record_path(onex_home)
    try:
        with path.open(encoding="utf-8") as source:
            document = json.loads(source.read(_MAX_BYTES))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    selection = document.get("selection")
    url = document.get("url")
    ref = document.get("credential_ref")
    model = selection.get("model") if isinstance(selection, dict) else None
    if not all(isinstance(v, str) and v for v in (url, ref, model)):
        return None
    return SetupGatewayBinding(
        gateway_url=str(url), model=str(model), credential_ref=str(ref)
    )


__all__: list[str] = [
    "GATEWAY_RECORD_NAME",
    "SetupGatewayBinding",
    "gateway_record_path",
    "read_setup_gateway_binding",
]
