# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How an answer or a refusal becomes the one result a command prints."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import SecretStr

from omnimarket.nodes.node_tenant_cli_effect.models.model_tenant_cli import (
    EnumTenantCliExit,
    ModelTenantCliResult,
)

# Answer outcomes that are successes, by command; every other outcome word is a failure to report.
_SUCCESS: Mapping[str, frozenset[str]] = {
    "create": frozenset({"created", "already_exists"}),
    "show": frozenset({"found"}),
    "key mint": frozenset({"registered", "already_registered"}),
    "key revoke": frozenset({"revoked", "already_revoked"}),
}
# An outcome that means the node or its store could not answer, not that the request is wrong.
_NODE_PROBLEM_MARKERS: tuple[str, ...] = (
    "store_",
    "failed_",
    "no_answer",
    "not_configured",
)
_SHOWN_FIELDS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "tenant_slug",
        "name",
        "provisioning_state",
        "plan_code",
        "created_at",
        "expires_at",
        "credential_id",
    }
)


def refusal(command: str, outcome: str, detail: str) -> ModelTenantCliResult:
    """A refusal by the command itself, before or without a node's answer."""
    return ModelTenantCliResult(
        command=command,
        outcome=outcome,
        ok=False,
        detail=detail,
        exit_code=EnumTenantCliExit.REQUEST_TO_FIX,
    )


def node_problem(command: str, outcome: str, detail: str) -> ModelTenantCliResult:
    return ModelTenantCliResult(
        command=command,
        outcome=outcome,
        ok=False,
        detail=detail,
        exit_code=EnumTenantCliExit.NODE_OR_BUS_PROBLEM,
    )


def from_answer(
    command: str,
    answer: Mapping[str, object],
    *,
    plaintext: SecretStr | None = None,
    note: str | None = None,
) -> ModelTenantCliResult:
    """The result of one node answer; only declared display fields are copied, and the answer's own detail line is kept."""
    outcome = str(answer.get("outcome", ""))
    ok = outcome in _SUCCESS[command]
    detail = str(answer.get("detail") or outcome)
    if note:
        detail = f"{detail} ({note})" if answer.get("detail") else note
    if ok:
        code = EnumTenantCliExit.OK
    elif any(marker in outcome for marker in _NODE_PROBLEM_MARKERS):
        code = EnumTenantCliExit.NODE_OR_BUS_PROBLEM
    else:
        code = EnumTenantCliExit.REQUEST_TO_FIX
    fields = {
        key: str(value)
        for key, value in answer.items()
        if key in _SHOWN_FIELDS and value is not None
    }
    return ModelTenantCliResult(
        command=command,
        outcome=outcome,
        ok=ok,
        detail=detail,
        exit_code=code,
        fields=fields,
        plaintext=plaintext if ok else None,
    )


__all__: list[str] = ["from_answer", "node_problem", "refusal"]
