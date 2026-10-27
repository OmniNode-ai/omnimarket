# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Composition adapter for immutable packaged declarations.

Compute handlers receive validated configuration. This boundary owns resource
I/O during composition; it never reads deployment state or supplies a fallback.
"""

from __future__ import annotations

from importlib.resources import files

import yaml


def load_packaged_contract_section(node_name: str, section: str) -> object:
    """Read a declared section; missing/malformed resources stop composition."""
    document = yaml.safe_load(
        files(f"omnimarket.nodes.{node_name}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    if not isinstance(document, dict):
        raise ValueError(f"{node_name}: contract must be a mapping")
    return document[section]
