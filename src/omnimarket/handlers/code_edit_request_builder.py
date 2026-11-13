# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the delegated code edit request from command line values (OMN-23930).

A limit the caller leaves out is taken from the node's overlay, else from the
request model's own default. Checks declared in a ticket contract come before
the checks given on the command line.
"""

from __future__ import annotations

import json
import shlex
import sys
import uuid
from pathlib import Path

from omnimarket.delegated_code_edit.loop_ports import resolve_delegate_pin
from omnimarket.handlers.code_edit_contract_checks import (
    declared_checks_from_contract,
)
from omnimarket.handlers.code_edit_loop_overlay import (
    load_code_edit_loop_overlay,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelDelegatePin,
)


def announced_delegate_pin(backend_id: str | None) -> ModelDelegatePin | None:
    """The pin for ``backend_id`` (``None`` for no pin), printed to stderr."""
    if backend_id is None:
        return None
    pin = resolve_delegate_pin(backend_id)
    sys.stderr.write(
        f"delegate pin: backend_id={pin.backend_id} host={pin.host} "
        f"endpoint={pin.endpoint_url} model={pin.model_name or '?'}\n"
    )
    sys.stderr.flush()
    return pin


def request_from_receipt(
    state_root: Path, resume_id: uuid.UUID
) -> ModelDelegatedCodeEditRequest:
    """The request a loop receipt recorded; ``ValueError`` when it is unreadable."""
    try:
        receipt = json.loads(
            (state_root / "runs" / str(resume_id) / "loop_receipt.json").read_text()
        )
        if not isinstance(receipt, dict):
            raise ValueError("receipt is not an object")
        return ModelDelegatedCodeEditRequest.model_validate(receipt["request"])
    except (OSError, ValueError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"unreadable receipt: {exc}") from exc


def request_from_file(
    request_path: Path, new_correlation: bool
) -> ModelDelegatedCodeEditRequest:
    """The request a ``--request`` file holds, under a new correlation id on demand."""
    try:
        request = ModelDelegatedCodeEditRequest.model_validate_json(
            request_path.read_text()
        )
    except ValueError as exc:
        raise ValueError(f"unreadable request: {exc}") from exc
    if new_correlation:
        request = request.model_copy(update={"correlation_id": str(uuid.uuid4())})
    return request


def build_code_edit_request(
    *,
    worktree: Path | None,
    task_file: Path | None,
    writable: tuple[str, ...],
    context_paths: tuple[str, ...],
    file_list: tuple[str, ...],
    cli_checks: tuple[ModelDeclaredCheck, ...],
    formatter: tuple[str, ...],
    max_turns: int | None,
    check_timeout_s: int | None,
    wall_budget_s: int | None,
    ticket_contract: Path | None,
    caller_lane: str | None,
    ticket: str | None,
) -> ModelDelegatedCodeEditRequest:
    """The request; ``ValueError`` when it is invalid or no check is declared."""
    if worktree is None or task_file is None or not writable:
        raise ValueError(
            "without --request, give --worktree, --task-file and at least one --writable"
        )
    overlay = load_code_edit_loop_overlay()
    fields = ModelDelegatedCodeEditRequest.model_fields
    if max_turns is None:
        max_turns = overlay.max_turns if overlay else fields["max_turns"].default
    if check_timeout_s is None:
        check_timeout_s = (
            overlay.check_timeout_seconds
            if overlay
            else ModelDeclaredCheck.model_fields["timeout_seconds"].default
        )
    max_no_change = (
        overlay.max_no_change_finishes
        if overlay
        else fields["max_no_change_finishes"].default
    )
    if wall_budget_s is None:
        wall_budget_s = overlay.wall_budget_seconds if overlay else None
    checks = cli_checks
    if ticket_contract is not None:
        contract_checks = declared_checks_from_contract(
            ticket_contract, check_timeout_s
        )
        checks = contract_checks + cli_checks
        context_paths = context_paths + tuple(
            target
            for check in contract_checks
            for target in check.targets
            if (worktree / target).is_file()
        )
    elif not cli_checks:
        raise ValueError(
            "without --request or --ticket-contract, give at least one --check"
        )
    task = task_file.read_text()
    try:
        return ModelDelegatedCodeEditRequest(
            correlation_id=str(uuid.uuid4()),
            file_list=file_list,
            task=task,
            workspace_root=str(worktree.resolve()),
            writable_globs=writable,
            context_paths=context_paths,
            checks=checks,
            formatter=tuple(tuple(shlex.split(step)) for step in formatter),
            max_turns=max_turns,
            wall_budget_seconds=wall_budget_s,
            max_no_change_finishes=max_no_change,
            caller_lane=caller_lane,
            ticket=ticket,
        )
    except ValueError as exc:
        raise ValueError(f"invalid request: {exc}") from exc
