# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for node_github_repo_gateway_effect (contract-first).

Three boundary protocols are proven here:

* (A) parent<->caller: every operation returns its own typed shape, never a bare
  dict.
* (B) parent<->read function: dispatch output is byte-identical to calling that
  read function directly, and no read function calls another.
* (E) shared token: the token is resolved via
  resolve_api_key(contract_secret_ref(...)), never from a raw env read, and is
  never logged.

Plus focused functional coverage of each read's classification.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, SecretStr

from omnimarket.nodes.node_github_repo_gateway_effect import (
    token_resolver,
)
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelBranchProtectionResult,
    ModelCiChecksResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelMergeCommitShaResult,
    ModelOpenPrsResult,
    ModelPrStatusResult,
    ModelReviewGateResult,
    ModelTicketRefResult,
)
from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
    read_branch_protection,
    read_ci_checks,
    read_merge_commit_sha,
    read_open_prs_list,
    read_pr_status,
    read_review_gate,
    read_ticket_ref,
)

_REPO = "OmniNode-ai/omnimarket"
_PR = 1683

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_github_repo_gateway_effect"
)
_NODE_SOURCE_FILES = [
    _NODE_DIR / "transport.py",
    _NODE_DIR / "read_operations.py",
    _NODE_DIR / "dispatcher.py",
    _NODE_DIR / "token_resolver.py",
    _NODE_DIR / "__main__.py",
    _NODE_DIR / "handlers" / "handler_github_repo_gateway.py",
]

_PR_DETAIL: dict[str, Any] = {
    "number": _PR,
    "title": "example pr",
    "isDraft": False,
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "BLOCKED",
    "reviewDecision": "CHANGES_REQUESTED",
    "headRefName": "jonah/omn-14307-typed-github-repo-gateway-read",
    "baseRefName": "dev",
    "merged": False,
    "mergeCommitOid": None,
    "reviewThreads": [{"isResolved": False}, {"isResolved": True}],
    "statusCheckRollup": [
        {
            "name": "verify",
            "conclusion": "FAILURE",
            "status": "COMPLETED",
            "isRequired": True,
        },
        {
            "name": "lint",
            "conclusion": "SUCCESS",
            "status": "COMPLETED",
            "isRequired": True,
        },
        {
            "name": "build",
            "conclusion": "",
            "status": "IN_PROGRESS",
            "isRequired": True,
        },
    ],
    "rollupState": "FAILURE",
}

_OPEN_PRS: list[dict[str, Any]] = [
    {
        "number": 10,
        "title": "first",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
    },
    {
        "number": 11,
        "title": "second",
        "isDraft": True,
        "mergeStateStatus": "DRAFT",
        "reviewDecision": None,
    },
]


class _StubTransport:
    """Canned read transport — returns fixtures, touches no network."""

    def __init__(
        self,
        *,
        open_prs: list[dict[str, Any]] | None = None,
        branch_protection: int | None = None,
        pr_detail: dict[str, Any] | None = None,
    ) -> None:
        self._open_prs = open_prs if open_prs is not None else _OPEN_PRS
        self._branch_protection = branch_protection
        self._pr_detail = pr_detail if pr_detail is not None else _PR_DETAIL

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return self._open_prs

    def fetch_branch_protection(self, repo: str) -> int | None:
        return self._branch_protection

    def fetch_pr_detail(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self._pr_detail


def _request(op: EnumGithubGatewayOperation) -> ModelGithubGatewayRequest:
    pr_scoped = op not in (
        EnumGithubGatewayOperation.OPEN_PRS_LIST,
        EnumGithubGatewayOperation.BRANCH_PROTECTION,
    )
    return ModelGithubGatewayRequest(
        operation=op,
        repo=_REPO,
        pr_number=_PR if pr_scoped else None,
    )


_EXPECTED_MODEL = {
    EnumGithubGatewayOperation.PR_STATUS: ModelPrStatusResult,
    EnumGithubGatewayOperation.CI_CHECKS: ModelCiChecksResult,
    EnumGithubGatewayOperation.OPEN_PRS_LIST: ModelOpenPrsResult,
    EnumGithubGatewayOperation.BRANCH_PROTECTION: ModelBranchProtectionResult,
    EnumGithubGatewayOperation.REVIEW_GATE: ModelReviewGateResult,
    EnumGithubGatewayOperation.MERGE_COMMIT_SHA: ModelMergeCommitShaResult,
    EnumGithubGatewayOperation.TICKET_REF: ModelTicketRefResult,
}


# --- Protocol A: every operation returns its own typed shape ----------------


@pytest.mark.unit
@pytest.mark.parametrize("op", list(EnumGithubGatewayOperation))
def test_protocol_a_operation_returns_its_own_typed_shape(
    op: EnumGithubGatewayOperation,
) -> None:
    transport = _StubTransport(branch_protection=2)
    result = dispatch(_request(op), transport)

    assert isinstance(result, BaseModel), "result must be a typed model, not a dict"
    assert not isinstance(result, dict)
    assert isinstance(result, _EXPECTED_MODEL[op])
    # the discriminator on the result equals the requested operation
    assert result.operation == op.value


@pytest.mark.unit
def test_protocol_a_covers_every_operation() -> None:
    """Every declared operation has an expected typed result (no gaps)."""
    assert set(_EXPECTED_MODEL) == set(EnumGithubGatewayOperation)


# --- Protocol B: dispatch == direct read; no read calls another -------------


@pytest.mark.unit
def test_protocol_b_dispatch_is_byte_identical_to_direct_read() -> None:
    transport = _StubTransport(branch_protection=1)

    pairs = [
        (
            EnumGithubGatewayOperation.PR_STATUS,
            read_pr_status(transport, _REPO, _PR),
        ),
        (
            EnumGithubGatewayOperation.CI_CHECKS,
            read_ci_checks(transport, _REPO, _PR),
        ),
        (
            EnumGithubGatewayOperation.OPEN_PRS_LIST,
            read_open_prs_list(transport, _REPO),
        ),
        (
            EnumGithubGatewayOperation.BRANCH_PROTECTION,
            read_branch_protection(transport, _REPO),
        ),
        (
            EnumGithubGatewayOperation.REVIEW_GATE,
            read_review_gate(transport, _REPO, _PR),
        ),
        (
            EnumGithubGatewayOperation.MERGE_COMMIT_SHA,
            read_merge_commit_sha(transport, _REPO, _PR),
        ),
        (
            EnumGithubGatewayOperation.TICKET_REF,
            read_ticket_ref(transport, _REPO, _PR),
        ),
    ]
    for op, direct in pairs:
        routed = dispatch(_request(op), transport)
        assert routed.model_dump_json() == direct.model_dump_json(), (
            f"dispatch output for {op.value} diverged from the direct read call"
        )


@pytest.mark.unit
def test_protocol_b_no_read_function_calls_another() -> None:
    """Static proof: no read_* function references another read_* by name."""
    source = (_NODE_DIR / "read_operations.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    read_fns = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("read_")
    }
    assert len(read_fns) == 7, f"expected 7 read functions, found {sorted(read_fns)}"

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("read_"):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    called = getattr(inner.func, "id", None)
                    assert called not in read_fns or called == node.name, (
                        f"read function {node.name} calls another read function "
                        f"{called}; reads must stay independent"
                    )


# --- Protocol E: shared token via contract ref, never env, never logged -----


@pytest.mark.unit
def test_protocol_e_token_resolves_via_contract_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def _fake_contract_secret_ref(contract_path: Path, secret_name: str) -> str:
        calls["ref_args"] = (Path(contract_path).name, secret_name)
        return secret_name

    def _fake_resolve_api_key(ref: str, **_kw: Any) -> SecretStr:
        calls["resolve_ref"] = ref
        return SecretStr("resolved-token-value")

    monkeypatch.setattr(
        token_resolver, "contract_secret_ref", _fake_contract_secret_ref
    )
    monkeypatch.setattr(token_resolver, "resolve_api_key", _fake_resolve_api_key)

    token = token_resolver.resolve_github_token()

    assert token == "resolved-token-value"
    assert calls["ref_args"] == ("contract.yaml", "GITHUB_TOKEN")
    assert calls["resolve_ref"] == "GITHUB_TOKEN"


@pytest.mark.unit
def test_protocol_e_no_raw_env_read_in_node_sources() -> None:
    for path in _NODE_SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for needle in ("os.environ", "os.getenv", "getenv("):
            assert needle not in text, f"{path.name} performs a raw env read ({needle})"


@pytest.mark.unit
def test_protocol_e_token_is_never_logged() -> None:
    log_markers = (
        "print(",
        ".info(",
        ".debug(",
        ".warning(",
        ".error(",
        ".exception(",
        "getlogger",
    )
    for path in _NODE_SOURCE_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            lower = line.lower()
            if any(marker in lower for marker in log_markers):
                assert "token" not in lower, f"{path.name} may log the token: {line!r}"
                assert "get_secret_value" not in line, (
                    f"{path.name} may log the secret: {line!r}"
                )


# --- Focused functional coverage --------------------------------------------


@pytest.mark.unit
def test_pr_status_classifies_red_and_blocked() -> None:
    result = read_pr_status(_StubTransport(), _REPO, _PR)
    assert result.overall == "red"
    assert result.blocked is True
    assert result.merge_state_status == "BLOCKED"
    assert result.review_decision == "CHANGES_REQUESTED"
    assert result.failing_contexts == ["verify"]


@pytest.mark.unit
def test_ci_checks_counts_states() -> None:
    result = read_ci_checks(_StubTransport(), _REPO, _PR)
    assert result.overall == "red"
    assert result.total == 3
    assert result.passed == 1
    assert result.failed == 1
    assert result.pending == 1
    assert result.failing_contexts == ["verify"]


@pytest.mark.unit
def test_pr_status_green_when_no_required_checks() -> None:
    detail = dict(_PR_DETAIL)
    detail["statusCheckRollup"] = []
    detail["mergeStateStatus"] = "CLEAN"
    result = read_pr_status(_StubTransport(pr_detail=detail), _REPO, _PR)
    assert result.overall == "green"
    assert result.blocked is False
    assert result.failing_contexts == []


@pytest.mark.unit
def test_review_gate_blocked_on_changes_requested_and_unresolved() -> None:
    result = read_review_gate(_StubTransport(), _REPO, _PR)
    assert result.unresolved_threads == 1
    assert result.review_decision == "CHANGES_REQUESTED"
    assert result.blocked is True


@pytest.mark.unit
def test_merge_commit_sha_reports_merge_state() -> None:
    detail = dict(_PR_DETAIL)
    detail["merged"] = True
    detail["mergeCommitOid"] = "abc123"
    result = read_merge_commit_sha(_StubTransport(pr_detail=detail), _REPO, _PR)
    assert result.merged is True
    assert result.merge_commit_sha == "abc123"


@pytest.mark.unit
def test_ticket_ref_extracts_omn_token() -> None:
    result = read_ticket_ref(_StubTransport(), _REPO, _PR)
    assert result.ticket_id == "OMN-14307"
    assert result.head_ref.startswith("jonah/omn-14307")


@pytest.mark.unit
def test_ticket_ref_none_when_no_token() -> None:
    detail = dict(_PR_DETAIL)
    detail["headRefName"] = "chore/no-ticket-here"
    result = read_ticket_ref(_StubTransport(pr_detail=detail), _REPO, _PR)
    assert result.ticket_id is None


@pytest.mark.unit
def test_open_prs_list_summarizes() -> None:
    result = read_open_prs_list(_StubTransport(), _REPO)
    assert result.count == 2
    assert result.prs[0].number == 10
    assert result.prs[1].is_draft is True


@pytest.mark.unit
def test_branch_protection_passthrough() -> None:
    assert (
        read_branch_protection(
            _StubTransport(branch_protection=2), _REPO
        ).required_approving_review_count
        == 2
    )
    assert (
        read_branch_protection(
            _StubTransport(branch_protection=None), _REPO
        ).required_approving_review_count
        is None
    )


# --- Request validation + response union round-trip -------------------------


@pytest.mark.unit
def test_request_requires_pr_number_for_pr_scoped_operation() -> None:
    with pytest.raises(ValueError, match="requires pr_number"):
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.PR_STATUS, repo=_REPO
        )


@pytest.mark.unit
def test_request_allows_missing_pr_for_repo_scoped_operation() -> None:
    req = ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.OPEN_PRS_LIST, repo=_REPO
    )
    assert req.pr_number is None


@pytest.mark.unit
def test_response_wrapper_round_trips_discriminated_union() -> None:
    result = read_pr_status(_StubTransport(), _REPO, _PR)
    response = ModelGithubGatewayResponse(correlation_id=uuid4(), result=result)
    reparsed = ModelGithubGatewayResponse.model_validate(
        response.model_dump(mode="json")
    )
    assert isinstance(reparsed.result, ModelPrStatusResult)
    assert reparsed.result.operation == "pr_status"
