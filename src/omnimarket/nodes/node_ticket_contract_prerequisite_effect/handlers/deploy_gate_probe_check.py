# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime ticket's contract carries a probe the hosted deploy gate accepts.

The hosted ``deploy-gate / deploy-gate`` check reads every ``check_value`` of the
ticket's ``contracts/<ticket>.yaml`` at the pull request head and passes a runtime
change only when one of them is a live-surface probe in command position
(``deploy_gate_probe``). The integration-proof check beside it resolves the head
of a command through ``uv run``, so a contract whose only live probe reads
``uv run onex lab-work run ...`` passed the push and was refused by the hosted
gate, which stops at ``uv``. This check applies the hosted rule at push time so
the refusal comes with the push and not with the pull request's required check.

Pure: no I/O. The git reads that supply ``changed_paths`` and the contract text
belong to ``HandlerTicketContractPrerequisite``.
"""

from __future__ import annotations

import yaml
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.deploy_gate_probe import (
    contract_check_values,
    is_hosted_falsifiable,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_integration_proof import (
    runtime_paths,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_result import (
    ModelIntegrationProofResult,
)

_EXAMPLE_LIMIT = 3


def _refusal(ticket_id: str, hits: tuple[str, ...]) -> str:
    shown = ", ".join(hits[:_EXAMPLE_LIMIT])
    more = (
        f" (+{len(hits) - _EXAMPLE_LIMIT} more)" if len(hits) > _EXAMPLE_LIMIT else ""
    )
    return (
        f"contracts/{ticket_id}.yaml: this change runs ({len(hits)} runtime "
        f"path(s): {shown}{more}) and no check in it is a probe the hosted "
        "deploy-gate accepts, so deploy-gate / deploy-gate fails on the pull "
        "request. The gate reads the head of each command as written: it looks "
        "through env, sudo, time, timeout, nohup, stdbuf, nice, command, exec "
        "and xargs but not through `uv run`, and a shell function, a git read "
        "or an echo is not a live surface. Add a check whose command starts "
        "with docker, kubectl, rpk, psql, curl, ssh, or onex (for example "
        "`onex lab-work run ...` without `uv run`), or a bash -c script whose "
        "command is the gh CLI with its api verb reading the changed file at "
        "`$(git rev-parse HEAD)` and grepping a symbol the change adds; a "
        "check that reads this contract or a receipt tree does not count."
    )


def check_deploy_gate_probe(
    request: ModelIntegrationProofRequest,
) -> ModelIntegrationProofResult:
    """Refuse a runtime ticket whose contract holds no hosted-falsifiable probe."""
    hits = runtime_paths(request.changed_paths)
    if not hits:
        return ModelIntegrationProofResult()
    try:
        raw = load_yaml_mapping_no_duplicates(
            request.contract_text, source=f"contracts/{request.ticket_id}.yaml"
        )
    except (yaml.YAMLError, ValueError) as exc:
        return ModelIntegrationProofResult(
            runtime_paths=hits,
            failure=f"contracts/{request.ticket_id}.yaml: {exc}",
        )
    items = raw.get("dod_evidence")
    values = contract_check_values(items if isinstance(items, list) else [])
    if any(is_hosted_falsifiable(value) for value in values):
        return ModelIntegrationProofResult(runtime_paths=hits)
    return ModelIntegrationProofResult(
        runtime_paths=hits, failure=_refusal(request.ticket_id, hits)
    )


__all__ = ["check_deploy_gate_probe"]
