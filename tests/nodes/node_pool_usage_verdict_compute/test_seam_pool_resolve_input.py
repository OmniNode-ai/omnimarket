# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23607 SEAM 2 (lane poolpin3-2710): drain_labels -> node_claude_token_pool_effect resolve.

The contract's ``seams.pool_resolve`` entry names the field this node's
``drain_labels`` crosses into the pool effect's ``resolve`` operation. While the
peer has not landed the field, the seam sits at ``awaiting_peer``; once the peer
lands it, the status flips to ``bound`` and the two sides must agree on a tuple
of the same label element type.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_pool_usage_verdict_compute as node_package
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models.model_pool_usage_result import (
    ModelPoolUsageResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


def test_seam_pool_resolve_field_status_matches_the_peer_model() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    seam = contract["seams"]["pool_resolve"]
    assert seam["node"] == "node_claude_token_pool_effect"
    assert seam["operation"] == "resolve"
    field_name = seam["field"]

    # My side of the seam: the verdict result always carries the field.
    assert field_name in ModelPoolUsageResult.model_fields

    if field_name in ModelClaudeTokenPoolRequest.model_fields:
        assert seam["status"] == "bound"
        peer_element = _tuple_element(
            ModelClaudeTokenPoolRequest.model_fields[field_name].annotation
        )
        mine_element = _tuple_element(
            ModelPoolUsageResult.model_fields[field_name].annotation
        )
        assert peer_element is str
        assert mine_element is peer_element
    else:
        assert seam["status"] == "awaiting_peer"


def _tuple_element(annotation: object) -> object | None:
    """The element type of a ``tuple[...]`` annotation, or None if it is not one."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is not tuple or not args:
        return None
    return args[0]
