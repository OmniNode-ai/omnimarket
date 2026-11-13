# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A product repository's contract, from the gateway's ``repo_file`` answer (OMN-24187)."""

from __future__ import annotations

from omnimarket.models.model_github_gateway_io import ModelRepoFileResult
from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    parse_repo_contract,
)
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumRepoContractReadStatus,
    ModelRepoContractRead,
)


def repo_contract_from_gateway(
    read: ModelRepoFileResult | str,
) -> ModelRepoContractRead:
    """An unreadable file (a string saying why) is an error, a missing one is absent."""
    if isinstance(read, str):
        return ModelRepoContractRead(
            status=EnumRepoContractReadStatus.ERROR, error=read
        )
    if read.text is None:
        return ModelRepoContractRead(status=EnumRepoContractReadStatus.ABSENT)
    return parse_repo_contract(read.text)


__all__: list[str] = ["repo_contract_from_gateway"]
