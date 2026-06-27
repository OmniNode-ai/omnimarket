# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_contract_sweep (OMN-13675, WS-5 Wave 1).

Variant A (pure COMPUTE): builds a synthetic ``$OMNI_HOME`` tree of repos whose
``src/.../nodes/<node>/contract.yaml`` files exercise each violation class, drives
``NodeContractSweep.handle`` in-process, and asserts typed result fields
(``contracts_checked``, ``violations`` count + structure, ``summary`` severity map).

Negative controls: a contract missing required fields, an invalid ``node_type``,
and a malformed topic name must each produce a typed ``ContractViolation``.
``OMNI_HOME`` is set via ``monkeypatch.setenv`` (env only — no subprocess mock).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    ContractSweepRequest,
    EnumViolationType,
    NodeContractSweep,
)

_VALID_CONTRACT = (
    "name: node_x\n"
    "contract_version: 1\n"
    "node_type: compute\n"
    "node_version: 1\n"
    "description: a valid contract\n"
)

_MISSING_FIELDS_CONTRACT = "name: node_missing\nnode_type: compute\n"

_INVALID_TYPE_CONTRACT = (
    "name: node_badtype\n"
    "contract_version: 1\n"
    "node_type: banana\n"
    "node_version: 1\n"
    "description: bad node_type\n"
)

_BAD_TOPIC_CONTRACT = (
    "name: node_badtopic\n"
    "contract_version: 1\n"
    "node_type: compute\n"
    "node_version: 1\n"
    "description: bad topic\n"
    "event_bus:\n"
    "  publish_topics:\n"
    "    - not-a-valid-topic\n"
)


def _add_node(repo: Path, node: str, contract: str) -> None:
    nd = repo / "src" / "nodes" / node
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "contract.yaml").write_text(contract, encoding="utf-8")


# (contracts spec per repo, repos arg, expected_checked, expected_min_violations,
#  required_violation_type | None)
CASES = [
    pytest.param(
        {"myrepo": [("node_x", _VALID_CONTRACT)]},
        ["myrepo"],
        1,
        0,
        None,
        id="drift-free-single-contract",
    ),
    pytest.param(
        {"myrepo": [("node_missing", _MISSING_FIELDS_CONTRACT)]},
        ["myrepo"],
        1,
        1,
        EnumViolationType.MISSING_REQUIRED_FIELD,
        id="missing-required-field-negative-control",
    ),
    pytest.param(
        {"myrepo": [("node_badtype", _INVALID_TYPE_CONTRACT)]},
        ["myrepo"],
        1,
        1,
        EnumViolationType.INVALID_NODE_TYPE,
        id="invalid-node-type-negative-control",
    ),
    pytest.param(
        {"myrepo": [("node_badtopic", _BAD_TOPIC_CONTRACT)]},
        ["myrepo"],
        1,
        1,
        EnumViolationType.INVALID_TOPIC_NAME,
        id="invalid-topic-name-negative-control",
    ),
    pytest.param(
        {
            "myrepo": [("node_x", _VALID_CONTRACT)],
            "otherrepo": [("node_missing", _MISSING_FIELDS_CONTRACT)],
        },
        ["myrepo"],
        1,
        0,
        None,
        id="repos-subset-excludes-other-repo",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("repo_spec", "repos_arg", "expected_checked", "min_violations", "required_type"),
    CASES,
)
def test_contract_sweep_multiparam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_spec: dict[str, list[tuple[str, str]]],
    repos_arg: list[str],
    expected_checked: int,
    min_violations: int,
    required_type: EnumViolationType | None,
) -> None:
    omni_home = tmp_path / "omni_home"
    for repo_name, nodes in repo_spec.items():
        repo = omni_home / repo_name
        for node_name, contract in nodes:
            _add_node(repo, node_name, contract)
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    result = NodeContractSweep().handle(ContractSweepRequest(repos=repos_arg))

    assert result.contracts_checked == expected_checked
    assert len(result.violations) >= min_violations
    # summary counts must agree with the typed violation list.
    assert sum(result.summary.values()) == len(result.violations)
    if required_type is not None:
        assert any(v.violation_type == required_type for v in result.violations)
        # every violation must reference the scanned node, not leak paths.
        assert all(v.node_name for v in result.violations)
    else:
        assert result.violations == []


@pytest.mark.integration
def test_contract_sweep_multi_repo_aggregates_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanning all repos aggregates contracts_checked and the severity summary."""
    omni_home = tmp_path / "omni_home"
    _add_node(omni_home / "repo_a", "node_x", _VALID_CONTRACT)
    _add_node(omni_home / "repo_b", "node_badtype", _INVALID_TYPE_CONTRACT)
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    result = NodeContractSweep().handle(
        ContractSweepRequest(repos=["repo_a", "repo_b"])
    )

    assert result.contracts_checked == 2
    assert any(
        v.violation_type == EnumViolationType.INVALID_NODE_TYPE
        for v in result.violations
    )
    assert result.summary.get("major", 0) >= 1
