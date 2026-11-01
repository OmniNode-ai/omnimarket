# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The CI fix and delegated fix nodes read their timeouts from their contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.nodes.node_ci_fix_effect.handlers import handler_ci_fix
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers import (
    diff_classifier,
    handler_delegated_fix,
)

# node -> {declared name: the value its code held as a literal before it moved}
DECLARED: dict[str, dict[str, float]] = {
    "node_ci_fix_effect": {
        "subprocess_default_s": 60,
        "github_log_download_s": 60,
        "git_inspect_s": 15,
        "git_patch_s": 30,
        "test_run_s": 300,
    },
    "node_pr_delegated_fix_effect": {
        "git_worktree_s": 60,
        "ruff_s": 120,
        "git_command_s": 30,
        "git_rev_parse_s": 15,
        "polish_cli_s": 600,
        "git_probe_s": 30,
    },
}

NODES_ROOT = Path(handler_ci_fix.__file__).resolve().parents[2]


@pytest.mark.unit
@pytest.mark.parametrize("node", sorted(DECLARED))
def test_node_declares_every_duration_with_its_prior_value(node: str) -> None:
    timeouts = load_node_timeouts(node)
    assert dict(timeouts.seconds) == {k: float(v) for k, v in DECLARED[node].items()}


@pytest.mark.unit
@pytest.mark.parametrize("node", sorted(DECLARED))
def test_undeclared_name_is_refused(node: str) -> None:
    with pytest.raises(KeyError, match="declares no timeout named"):
        load_node_timeouts(node).get("undeclared_duration")


@pytest.mark.unit
def test_module_timeouts_are_the_contract_declarations() -> None:
    assert handler_ci_fix._TIMEOUTS is load_node_timeouts("node_ci_fix_effect")
    assert handler_delegated_fix._TIMEOUTS is load_node_timeouts(
        "node_pr_delegated_fix_effect"
    )
    assert diff_classifier._TIMEOUTS is load_node_timeouts(
        "node_pr_delegated_fix_effect"
    )


@pytest.mark.unit
def test_constructor_defaults_come_from_the_contract() -> None:
    ci = load_node_timeouts("node_ci_fix_effect")
    fix = load_node_timeouts("node_pr_delegated_fix_effect")
    assert handler_ci_fix._run_subprocess.__kwdefaults__["timeout"] == ci.get(
        "subprocess_default_s"
    )
    assert handler_delegated_fix._run_checked.__kwdefaults__["timeout"] == fix.get(
        "git_worktree_s"
    )
    assert handler_delegated_fix._git_stdout.__kwdefaults__["timeout"] == fix.get(
        "git_command_s"
    )


@pytest.mark.unit
def test_slice_has_no_literal_timeout_findings() -> None:
    paths = [
        NODES_ROOT / "node_ci_fix_effect",
        NODES_ROOT / "node_pr_delegated_fix_effect",
    ]
    result = ValidatorContractsAndDi().validate(paths)
    assert result.metadata.files_processed >= len(paths)
    findings = [i for i in result.issues if i.rule_name == "literal_timeout"]
    assert not findings, findings
