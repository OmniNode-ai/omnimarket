# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the install-proof declaration at composition, before the pure decision (OMN-23616)."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.pr_handoff_install_proof import ModelInstallDeclaration

NODE_NAME = "node_pr_handoff_install_proof_compute"


def load_install_declaration(
    environ: Mapping[str, str] | None = None,
) -> ModelInstallDeclaration:
    """Read the deployment overlay's declaration.

    The overlay is ``<root>/node_pr_handoff_install_proof_compute/overlay.yaml`` with every
    declaration key. A missing or invalid overlay raises; it never silently falls back.
    """
    return load_structured_overlay(NODE_NAME, ModelInstallDeclaration, environ)
