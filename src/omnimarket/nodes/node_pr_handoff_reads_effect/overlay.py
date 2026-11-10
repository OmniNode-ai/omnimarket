# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_pr_handoff_reads_effect: the host_install declaration, none of it in code (OMN-21815).

Found at ``<root>/node_pr_handoff_reads_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with the same shape. A missing, unreadable or invalid overlay refuses the load:
it never reads as a declaration that requires nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_declaration import (
    ModelPrHandoffInstallDeclaration,
    ModelPrHandoffReadsOverlay,
)

NODE_NAME = "node_pr_handoff_reads_effect"


def load_install_declaration(
    environ: Mapping[str, str] | None = None,
) -> ModelPrHandoffInstallDeclaration:
    return load_structured_overlay(
        NODE_NAME, ModelPrHandoffReadsOverlay, environ
    ).host_install
