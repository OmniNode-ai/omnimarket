# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Code gate digest compute node (OMN-19527).

Pure COMPUTE: what ``ruff check``, ``ruff format --check`` and ``mypy
--strict`` printed about one delegated file, plus the file's source, in; one
bounded, fingerprinted digest out, small enough to paste verbatim into a
repair prompt. Used by the delegated test loop to turn a repository-gate miss
into one repair round instead of a failed answer.
"""

from omnimarket.nodes.node_code_gate_digest_compute.handlers.handler_code_gate_digest import (
    HandlerCodeGateDigest,
    digest_code_gates,
)
from omnimarket.nodes.node_code_gate_digest_compute.models.model_code_gate_digest import (
    TOOL_GATES,
    EnumCodeGate,
    ModelCodeGateDigest,
    ModelCodeGateDigestRequest,
    ModelGateFinding,
    ModelGateToolOutput,
)


class NodeCodeGateDigestCompute(HandlerCodeGateDigest):
    """ONEX entry-point wrapper for HandlerCodeGateDigest."""


__all__ = [
    "TOOL_GATES",
    "EnumCodeGate",
    "HandlerCodeGateDigest",
    "ModelCodeGateDigest",
    "ModelCodeGateDigestRequest",
    "ModelGateFinding",
    "ModelGateToolOutput",
    "NodeCodeGateDigestCompute",
    "digest_code_gates",
]
