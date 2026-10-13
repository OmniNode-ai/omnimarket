# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local contract -> typed request -> effect -> typed verdict controls."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers import (
    handler_ticket_contract_prerequisite as boundary,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_ticket_contract_prerequisite_effect"
SHA = "a" * 40


@pytest.mark.parametrize("present", [True, False])
def test_golden_chain_contract_resolves_handler_and_returns_verdict(
    monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    handler_spec = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_spec["module"]), handler_spec["class"]
    )
    request_module, _, request_name = handler_spec["input_model"].rpartition(".")
    request_type = getattr(importlib.import_module(request_module), request_name)
    result_module, _, result_name = handler_spec["output_model"].rpartition(".")
    result_type = getattr(importlib.import_module(result_module), result_name)
    commands = []

    def git(*args: str) -> str:
        commands.append(args)
        return (
            "ticket_id: OMN-20864\ndod_evidence: [{id: probe, checks: [{check_type: test_passes, check_value: 'uv run pytest tests/ci/test_ticket_contract_prerequisite_omn20864.py -q'}]}]"
            if args[0] == "show"
            else SHA
        )

    monkeypatch.setattr(boundary, "_git", git)
    monkeypatch.setattr(
        boundary.subprocess,
        "run",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 0 if present else 128),
    )
    result = handler_type().handle(
        request_type(head="outgoing", title="fix(OMN-20864): require contract")
    )
    assert isinstance(result, result_type)
    assert result.head_sha == SHA
    assert result.passed is present
    if not present:
        assert result.failures == (
            "pull request cites OMN-20864 but carries no contracts/OMN-20864.yaml",
        )
    assert all(
        args[0] in {"rev-parse", "show", "merge-base", "diff"} for args in commands
    )
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"]["external_trigger"]


def test_git_read_failure_is_a_failed_typed_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_ticket_contract_prerequisite_request import (
        ModelTicketContractPrerequisiteRequest,
    )

    def unreadable(*_args: str) -> str:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    monkeypatch.setattr(boundary, "_git", unreadable)
    result = boundary.HandlerTicketContractPrerequisite().handle(
        ModelTicketContractPrerequisiteRequest()
    )
    assert not result.passed
    assert result.head_sha is None
    assert "could not read the pushed commit" in result.failures[0]
