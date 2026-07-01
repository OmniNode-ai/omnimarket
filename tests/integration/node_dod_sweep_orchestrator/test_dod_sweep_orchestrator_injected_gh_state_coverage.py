# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full-state-coverage suite for node_dod_sweep_orchestrator's ``gh``-backed
checks (OMN-13783, WS-M Wave 5).

``pr_merged`` / ``ci_green`` and the ``gh_search_query`` batch-enumeration path
touch the ``gh`` CLI via ``subprocess.run``. The pre-existing golden-chain
tests exercise those states by monkeypatching ``subprocess.run`` directly,
which the state-coverage wave's own mechanics rule forbids for new coverage
("never monkeypatch subprocess/asyncpg; add an injectable collector seam
first"). ``HandlerDodSweepOrchestrator`` now accepts the three ``gh``
collaborators as constructor-injected callables
(``gh_find_merged_pr_fn`` / ``gh_pr_checks_pass_fn`` / ``enumerate_tickets_fn``)
so this suite proves every declared check-status branch with deterministic
fakes at the I/O boundary — no subprocess, no monkeypatching.

Declared state space covered here:
  - pr_merged: pass, fail
  - ci_green: pass, fail (skip is covered by the pre-existing filesystem-only
    multiparam suite, which never reaches the gh boundary)
  - batch gh_search_query ticket enumeration: hit (valid ids), miss (query
    returns zero/invalid ids -> no_valid_ticket_ids skip, NEGATIVE CONTROL)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)


def _write_contract(root: Path, ticket_id: str) -> None:
    contracts_dir = root / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "schema_version: 1.0.0\n"
        f"ticket_id: {ticket_id}\n"
        "dod_evidence:\n"
        "  - id: dod-001\n"
        "    description: Code change shipped\n"
        "    checks:\n"
        "      - check_type: command\n"
        "        check_value: 'true'\n"
    )
    (contracts_dir / f"{ticket_id}.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# pr_merged: pass / fail via injected gh_find_merged_pr_fn
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pr_merged_pass_via_injected_fn(tmp_path: Path) -> None:
    def _fake_find_merged_pr(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
        assert ticket_id == "OMN-9001"
        return {"number": "7", "repo": "OmniNode-ai/omnimarket"}

    handler = HandlerDodSweepOrchestrator(gh_find_merged_pr_fn=_fake_find_merged_pr)
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-9001",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("pr_merged",),
        )
    )

    pr_check = next(c for c in result.batch_results[0].checks if c.check == "pr_merged")
    assert pr_check.status == "pass"
    assert pr_check.details == {"pr_number": "7", "repo": "OmniNode-ai/omnimarket"}
    assert result.failed == 0


@pytest.mark.integration
def test_pr_merged_fail_via_injected_fn(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: the injected fn reports no merged PR -> pr_merged fails.
    def _fake_find_merged_pr_empty(
        ticket_id: str, repos: tuple[str, ...]
    ) -> dict[str, str]:
        return {}

    handler = HandlerDodSweepOrchestrator(
        gh_find_merged_pr_fn=_fake_find_merged_pr_empty
    )
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-9002",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("pr_merged",),
        )
    )

    pr_check = next(c for c in result.batch_results[0].checks if c.check == "pr_merged")
    assert pr_check.status == "fail"
    assert pr_check.details["reason"] == "no_merged_pr_found"
    assert result.failed == 1


# ---------------------------------------------------------------------------
# ci_green: pass / fail via injected gh_pr_checks_pass_fn
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ci_green_pass_via_injected_fn(tmp_path: Path) -> None:
    def _fake_find_merged_pr(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
        return {"number": "11", "repo": "OmniNode-ai/omnimarket"}

    def _fake_checks_pass(pr_number: str, repo: str) -> tuple[bool, str]:
        assert pr_number == "11"
        assert repo == "OmniNode-ai/omnimarket"
        return True, "all_3_checks_green"

    handler = HandlerDodSweepOrchestrator(
        gh_find_merged_pr_fn=_fake_find_merged_pr,
        gh_pr_checks_pass_fn=_fake_checks_pass,
    )
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-9003",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("pr_merged", "ci_green"),
        )
    )

    ci_check = next(c for c in result.batch_results[0].checks if c.check == "ci_green")
    assert ci_check.status == "pass"
    assert ci_check.details["detail"] == "all_3_checks_green"
    assert result.failed == 0


@pytest.mark.integration
def test_ci_green_fail_via_injected_fn(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: the injected fn reports a red check -> ci_green fails.
    def _fake_find_merged_pr(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
        return {"number": "12", "repo": "OmniNode-ai/omnimarket"}

    def _fake_checks_fail(pr_number: str, repo: str) -> tuple[bool, str]:
        return False, "failed_checks: unit-tests"

    handler = HandlerDodSweepOrchestrator(
        gh_find_merged_pr_fn=_fake_find_merged_pr,
        gh_pr_checks_pass_fn=_fake_checks_fail,
    )
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-9004",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("pr_merged", "ci_green"),
        )
    )

    ci_check = next(c for c in result.batch_results[0].checks if c.check == "ci_green")
    assert ci_check.status == "fail"
    assert "unit-tests" in ci_check.details["detail"]
    assert result.failed == 1


# ---------------------------------------------------------------------------
# batch mode: gh_search_query enumeration via injected enumerate_tickets_fn
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_batch_gh_search_query_enumeration_hit(tmp_path: Path) -> None:
    _write_contract(tmp_path, "OMN-9100")
    _write_contract(tmp_path, "OMN-9101")

    def _fake_enumerate(query: str) -> list[str]:
        assert query == "label:dod-sweep-candidate"
        return ["OMN-9100", "OMN-9101"]

    handler = HandlerDodSweepOrchestrator(enumerate_tickets_fn=_fake_enumerate)
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            scope="batch-label",
            gh_search_query="label:dod-sweep-candidate",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("contract_exists", "receipt_exists"),
            dry_run=True,
        )
    )

    assert result.mode == "batch"
    assert result.batch_total == 2
    assert result.batch_verified == 2
    assert result.status == "verified"
    ids = {tr.ticket_id for tr in result.batch_results}
    assert ids == {"OMN-9100", "OMN-9101"}


@pytest.mark.integration
def test_batch_gh_search_query_enumeration_miss(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: the search query enumerates zero valid OMN ids ->
    # batch mode fails closed to the no_valid_ticket_ids skip path.
    def _fake_enumerate_empty(query: str) -> list[str]:
        return ["not-a-ticket", "also-not-one"]

    handler = HandlerDodSweepOrchestrator(enumerate_tickets_fn=_fake_enumerate_empty)
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            scope="batch-label",
            gh_search_query="label:dod-sweep-candidate",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("contract_exists", "receipt_exists"),
        )
    )

    assert result.mode == "batch"
    assert result.status == "skipped"
    assert result.details["reason"] == "no_valid_ticket_ids"
    assert result.details["raw_count"] == "2"


@pytest.mark.integration
def test_default_handler_still_uses_real_gh_collaborators() -> None:
    """Constructing the handler with no args must not raise — defaults bind
    to the real subprocess-backed functions (production wiring unaffected)."""
    handler = HandlerDodSweepOrchestrator()
    assert handler._gh_find_merged_pr_fn is not None
    assert handler._gh_pr_checks_pass_fn is not None
    assert handler._enumerate_tickets_fn is not None
