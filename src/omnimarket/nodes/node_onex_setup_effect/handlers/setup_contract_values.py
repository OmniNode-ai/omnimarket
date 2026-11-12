# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Addresses setup contacts, read from this node's committed files (OMN-23413).

The URL authority gate requires every URL to resolve from a contract or an
integration catalog, never a literal in source. The loopback host of the
processes setup starts is contract metadata; third-party addresses (Claude
Code's vendor installer) are in the node's integration_endpoints.yaml, because
the contract overlay boundary keeps endpoint literals out of contract.yaml.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

_NODE = Path(__file__).resolve().parents[1]


@cache
def _mapping(name: str, key: str | None) -> dict[str, object]:
    document = yaml.safe_load((_NODE / name).read_text())
    section = document.get(key) if key and isinstance(document, dict) else document
    if not isinstance(section, dict):
        raise ValueError(f"The setup node's {name} carries no {key or 'mapping'}")
    return section


def _value(name: str, key: str | None, field: str) -> str:
    value = _mapping(name, key).get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"The setup node's {name} does not declare {field}")
    return value


def claude_code_installer_url() -> str:
    """Claude Code's own vendor installer, run only when the user selects Claude."""
    return _value("integration_endpoints.yaml", None, "claude_code_installer_url")


def loopback_url(port: int) -> str:
    """The URL of a process setup started on this machine's loopback interface."""
    return f"http://{_value('contract.yaml', 'metadata', 'loopback_host')}:{port}"
