# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the read-only principal."""

from __future__ import annotations

from pathlib import Path

from .handlers.handler_read_only_principal_probe import HandlerReadOnlyPrincipalProbe


class NodeReadOnlyPrincipalEffect(HandlerReadOnlyPrincipalProbe):
    """Declarative entry point: the principal's probe, with the gate and the key install beside it."""

    contract_path = Path(__file__).with_name("contract.yaml")
