# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15443: local DoD evidence audiences fail closed before effects."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _write_contract(tmp_path: Path, dod_evidence: list[dict[str, Any]]) -> Path:
    contract_path = tmp_path / "OMN-15443.yaml"
    contract_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": "OMN-15443",
                "dod_evidence": dod_evidence,
            }
        ),
        encoding="utf-8",
    )
    return contract_path


def _command_item(
    item_id: str,
    command: str,
    *,
    execution_scope: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "description": f"Evidence for {item_id}",
        "checks": [{"check_type": "command", "check_value": command}],
    }
    if execution_scope is not None:
        item["execution_scope"] = execution_scope
    return item


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scope_key", "scope_value"),
    [
        pytest.param("execution_scope", "hosted_maybe", id="unknown-value"),
        pytest.param("execution_scpoe", "local_done_gate", id="misspelled-key"),
    ],
)
def test_invalid_scope_preflight_blocks_all_checks_and_github_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope_key: str,
    scope_value: str,
) -> None:
    """A later malformed audience prevents every earlier or later effect."""
    early_marker = tmp_path / "early-valid-sibling-ran"
    invalid_marker = tmp_path / "invalid-audience-ran"
    invalid_item = _command_item(
        "dod-invalid-audience",
        f"touch {shlex.quote(str(invalid_marker))}",
    )
    invalid_item[scope_key] = scope_value
    # Bind the invalid item to a PR so reaching either live-state path is an
    # observable GitHub EFFECT call, not a vacuous zero-call assertion.
    invalid_item["pr"] = {"repo": "OmniNode-ai/omnimarket", "number": 1957}
    early_item = _command_item(
        "dod-early-valid-sibling",
        f"touch {shlex.quote(str(early_marker))}",
    )
    # The earlier valid sibling is PR-bound too.  Preflight must scan the later
    # malformed item before this sibling can reach its first live GitHub read.
    early_item["pr"] = {"repo": "OmniNode-ai/omnimarket", "number": 1957}
    contract_path = _write_contract(
        tmp_path,
        [
            early_item,
            invalid_item,
        ],
    )

    check_calls: list[str] = []
    github_calls: list[str] = []
    original_check = EvidenceCollector._check_evidence_item

    def _spy_check(
        self: EvidenceCollector,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None = None,
    ) -> Any:
        check_calls.append(str(item.get("id")))
        return original_check(self, item, ticket_id, contract_path)

    def _merged(self: EvidenceCollector, repo: str, pr_number: int) -> tuple[bool, str]:
        github_calls.append(f"merge:{repo}#{pr_number}")
        return True, "MERGED"

    def _checks_green(
        self: EvidenceCollector, repo: str, pr_number: int
    ) -> tuple[bool, str]:
        github_calls.append(f"checks:{repo}#{pr_number}")
        return True, "all required checks green"

    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "1")
    monkeypatch.setattr(EvidenceCollector, "_check_evidence_item", _spy_check)
    monkeypatch.setattr(EvidenceCollector, "_fetch_pr_merge_state", _merged)
    monkeypatch.setattr(EvidenceCollector, "_fetch_pr_checks_green", _checks_green)

    result = HandlerDodVerify().handle(
        {"ticket_id": "OMN-15443", "contract_path": str(contract_path)}
    )

    assert isinstance(result, dict)
    result_dict = cast(dict[str, Any], result)
    observed = (
        result_dict["status"],
        result_dict["failed_count"],
        early_marker.exists(),
        invalid_marker.exists(),
        check_calls,
        github_calls,
    )
    assert observed == ("failed", 1, False, False, [], [])
    messages = " ".join(
        str(check.get("message") or "") for check in result_dict["checks"]
    )
    assert scope_key in messages
    assert scope_value in messages


@pytest.mark.unit
@pytest.mark.parametrize(
    "execution_scope",
    [None, "hosted_and_local", "local_done_gate"],
    ids=["omitted-default", "hosted-and-local", "local-done-gate"],
)
def test_valid_execution_scopes_execute_locally_and_preserve_uuid(
    tmp_path: Path,
    execution_scope: str | None,
) -> None:
    marker = tmp_path / "valid-audience-ran"
    contract_path = _write_contract(
        tmp_path,
        [
            _command_item(
                "dod-valid-audience",
                f"touch {shlex.quote(str(marker))}",
                execution_scope=execution_scope,
            )
        ],
    )
    correlation_id = uuid4()
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-15443",
        correlation_id=correlation_id,
        contract_path=str(contract_path),
    )

    state = HandlerDodVerify().handle(command)

    assert isinstance(state, ModelDodVerifyState)
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.correlation_id == correlation_id
    assert marker.exists()


@pytest.mark.unit
def test_local_done_gate_wrong_private_identifier_remains_a_real_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid local audience executes its check and preserves a 404 failure."""
    contract_path = _write_contract(
        tmp_path,
        [
            _command_item(
                "dod-private-run-readback",
                "gh api repos/OmniNode-ai/private-proof/actions/runs/000000",
                execution_scope="local_done_gate",
            )
        ],
    )
    subprocess_calls: list[list[str]] = []

    def _not_found(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=22,
            stdout="",
            stderr="HTTP 404: Not Found",
        )

    monkeypatch.setattr(subprocess, "run", _not_found)
    correlation_id = uuid4()
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-15443",
        correlation_id=correlation_id,
        contract_path=str(contract_path),
    )

    state = HandlerDodVerify().handle(command)

    assert isinstance(state, ModelDodVerifyState)
    assert state.status is EnumDodVerifyStatus.FAILED
    assert state.correlation_id == correlation_id
    assert state.failed_count == 1
    assert len(subprocess_calls) == 1
    assert "404" in (state.checks[0].message or "")
