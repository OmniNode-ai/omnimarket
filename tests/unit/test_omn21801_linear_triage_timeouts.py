# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_linear_triage reads its timeouts from its contract, with every value as before."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.nodes.node_linear_triage.handlers import handler_linear_triage
from omnimarket.nodes.node_linear_triage.handlers.handler_git_automation_demotion_guard import (
    LinearGraphQLPerTeamTransport,
)
from omnimarket.nodes.node_linear_triage.handlers.handler_git_automation_guard import (
    LinearGitAutomationProbe,
)
from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubHttpClient,
    LinearHttpClient,
    OccReceiptSubprocessProbe,
)
from omnimarket.nodes.node_linear_triage.services import (
    done_write_receipt_gate,
    local_github_pr_reads,
)
from omnimarket.nodes.node_linear_triage.services.done_write_receipt_gate import (
    DodVerifySubprocessProbe,
)

NODE = "node_linear_triage"

# declared name -> the value the node's code held as a literal before it moved
DECLARED: dict[str, float] = {
    "linear_graphql_timeout_seconds": 30.0,
    "github_rest_timeout_seconds": 30.0,
    "github_graphql_timeout_seconds": 30.0,
    "occ_git_timeout_seconds": 15.0,
    "dod_verify_timeout_seconds": 300.0,
    "local_git_timeout_seconds": 60.0,
}

NODE_DIR = Path(handler_linear_triage.__file__).resolve().parents[1]


@pytest.mark.unit
def test_contract_declares_every_duration_with_its_former_value() -> None:
    assert dict(load_node_timeouts(NODE).seconds) == DECLARED


@pytest.mark.unit
def test_an_undeclared_name_is_refused() -> None:
    with pytest.raises(KeyError, match="declares no timeout named"):
        load_node_timeouts(NODE).get("undeclared_duration")


@pytest.mark.unit
def test_module_constants_come_from_the_contract() -> None:
    assert done_write_receipt_gate.DOD_VERIFY_TIMEOUT_SECONDS == 300.0
    assert local_github_pr_reads._GIT_TIMEOUT_S == 60.0


@pytest.mark.unit
def test_constructor_defaults_come_from_the_contract() -> None:
    assert LinearHttpClient("k")._timeout == 30.0
    github = GitHubHttpClient("t")
    assert (github._rest_timeout, github._graphql_timeout) == (30.0, 30.0)
    assert OccReceiptSubprocessProbe(occ_repo_path=Path("occ"))._git_timeout == 15.0
    assert DodVerifySubprocessProbe()._timeout_seconds == 300.0
    assert LinearGitAutomationProbe("k")._timeout == 30.0
    assert LinearGraphQLPerTeamTransport("k")._timeout == 30.0


@pytest.mark.unit
def test_an_injected_duration_wins_over_the_contract() -> None:
    assert LinearHttpClient("k", timeout=1.5)._timeout == 1.5
    github = GitHubHttpClient("t", timeout=2.5)
    assert (github._rest_timeout, github._graphql_timeout) == (2.5, 2.5)
    probe = OccReceiptSubprocessProbe(occ_repo_path=Path("occ"), git_timeout=3.5)
    assert probe._git_timeout == 3.5
    assert DodVerifySubprocessProbe(timeout_seconds=4.5)._timeout_seconds == 4.5
    assert LinearGitAutomationProbe("k", timeout=5.5)._timeout == 5.5
    assert LinearGraphQLPerTeamTransport("k", timeout=6.5)._timeout == 6.5


@pytest.mark.unit
def test_the_node_has_no_literal_timeout_finding() -> None:
    result = ValidatorContractsAndDi().validate([NODE_DIR])
    assert result.metadata is not None
    processed = result.metadata.files_processed
    assert processed is not None
    # Every handler and service module of the node is scanned.
    assert processed >= len(list(NODE_DIR.glob("handlers/*.py"))) + len(
        list(NODE_DIR.glob("services/*.py"))
    )
    findings = [i for i in result.issues if i.rule_name == "literal_timeout"]
    assert not findings, findings
