# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Codex harness policy: contract defaults and the lane overlay's document (OMN-21595)."""

from __future__ import annotations

from importlib import resources

import yaml

from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument

from .models.model_codex_harness_attempt import ModelCodexHarnessPolicy

PACKAGE = "omnimarket.nodes.node_llm_delegation_call_effect"
_POLICY_DOCUMENT = "codex_harness"


def load_codex_harness_policy(
    documents: ProtocolOverlayDocument,
) -> ModelCodexHarnessPolicy:
    """Load packaged contract defaults and the lane overlay's ``codex_harness`` document."""
    contract = yaml.safe_load(
        resources.files(PACKAGE).joinpath("contract.yaml").read_text()
    )
    policy = contract["codex_harness"]
    overlay = documents.read(_POLICY_DOCUMENT)
    if overlay is not None:
        if set(overlay) != {"codex_harness"}:
            raise ValueError("expected a codex_harness policy overlay")
        if not isinstance(overlay["codex_harness"], dict):
            raise ValueError("codex_harness policy must be a mapping")
        policy = policy | overlay["codex_harness"]
    return ModelCodexHarnessPolicy.model_validate(policy)
