# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Addresses setup contacts, read from this node's contract.yaml (OMN-23413).

The URL authority gate requires every URL to resolve from a contract, so the
loopback host of the processes setup starts is contract metadata. Claude Code's
vendor installer is a fixed third-party address, kept here with its annotation.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml


@cache
def _metadata() -> dict[str, object]:
    document = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "contract.yaml").read_text()
    )
    metadata = document.get("metadata") if isinstance(document, dict) else None
    if not isinstance(metadata, dict):
        raise ValueError("The setup contract carries no metadata")
    return metadata


def _value(key: str) -> str:
    value = _metadata().get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"The setup contract does not declare {key}")
    return value


def claude_code_installer_url() -> str:
    """Claude Code's own vendor installer, run only when the user selects Claude."""
    return "https://claude.ai/install.sh"  # url-authority-ok: a fixed third-party vendor address, not an ONEX endpoint


def loopback_url(port: int) -> str:
    """The URL of a process setup started on this machine's loopback interface."""
    return f"http://{_value('loopback_host')}:{port}"
