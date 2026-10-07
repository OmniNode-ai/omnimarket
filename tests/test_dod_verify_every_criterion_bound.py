# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20070 AC3 -- a repo-owned contract binds every acceptance criterion.

A product repository's own ``contracts/OMN-<n>.yaml`` binds each declared
acceptance criterion through ``binds_ac`` on a ``dod_evidence`` item. Before
this change the verifier only ran the items it was given, so removing one
binding left a contract that still read VERIFIED, and the "every criterion is
bound" rule lived only in omnimarket's own contract-binds test. A second
repository adopting the repo-evidence gate would have had no such test.

Runs the real ``HandlerDodVerify`` and ``EvidenceCollector`` over a contract
that lives in the product repository, with only the command runner stubbed.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from omnimarket.nodes.node_dod_verify.services import evidence_collector
from omnimarket.nodes.node_dod_verify.services.occ_verdict_difference import (
    load_new_verdict,
)

pytestmark = pytest.mark.unit

_TICKET = "OMN-20999"
_TEST_A = "uv run pytest tests/test_a.py -q"
_TEST_B = "uv run pytest tests/test_b.py -q"


def _item(item_id: str, command: str, binds: list[str] | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "description": f"{item_id} runs {command}",
        "source": "manual",
        "checks": [
            {
                "check_type": "test_passes",
                "check_value": command,
                "cwd": "${OMNI_HOME}/omnimarket",
            }
        ],
    }
    if binds is not None:
        item["binds_ac"] = binds
    return item


def _contract(
    criteria: list[str] | None, items: list[dict[str, Any]]
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "title": "every criterion bound",
        "dod_evidence": items,
    }
    if criteria is not None:
        contract["requirements"] = [
            {
                "id": "req-every-criterion-bound",
                "statement": "every criterion is bound",
                "acceptance": [
                    {"id": label, "statement": f"{label}: does a thing"}
                    for label in criteria
                ],
            }
        ]
    return contract


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract: dict[str, Any],
    *,
    failing: frozenset[str] = frozenset(),
) -> ModelDodVerifyState:
    omni_home = tmp_path / "omni_home"
    repo = omni_home / "omnimarket"
    (repo / "tests").mkdir(parents=True)
    (repo / "contracts").mkdir()
    (repo / "tests" / "test_a.py").write_text("")
    (repo / "tests" / "test_b.py").write_text("")
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "uv.lock").write_text("")
    monkeypatch.setenv("OMNI_HOME", str(omni_home))
    monkeypatch.setenv(evidence_collector._ALLOW_STALE_PRODUCT_CLONE_ENV, "1")

    def _fake_run(
        _self: Any, check: dict[str, Any], *_a: Any, **_k: Any
    ) -> tuple[bool, str]:
        value = str(check.get("check_value") or check.get("command"))
        if value in failing:
            return False, "1 failed"
        return True, "1 passed"

    monkeypatch.setattr(
        evidence_collector.EvidenceCollector, "_run_command_check", _fake_run
    )

    path = repo / "contracts" / f"{_TICKET}.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False))
    command = ModelDodVerifyStartCommand(
        correlation_id=uuid.uuid4(),
        ticket_id=_TICKET,
        contract_path=str(path),
        execution_audience="hosted",
        requested_at=datetime.now(tz=UTC),
    )
    state = HandlerDodVerify()._handle_typed(command)
    assert isinstance(state, ModelDodVerifyState)
    return state


def test_every_criterion_bound_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2"],
            [_item("dod-a", _TEST_A, ["AC1"]), _item("dod-b", _TEST_B, ["AC2"])],
        ),
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.acceptance_unbound_criteria == ()
    assert state.error_message is None


def test_one_binding_removed_reports_no_acceptance_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AC3 falsifier: a copy of the contract with one binding removed."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2"],
            [_item("dod-a", _TEST_A, ["AC1"]), _item("dod-b", _TEST_B, None)],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_unbound_criteria == ("AC2",)
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")
    assert "AC2" in state.error_message
    assert "AC1" not in state.error_message.split("(", 1)[1]


def test_only_binding_removed_reports_no_acceptance_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract left with no binding at all is not the legacy shape."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(["AC1"], [_item("dod-a", _TEST_A, None)]),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_unbound_criteria == ("AC1",)
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")


def test_binding_label_matches_criterion_canonically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AC-1`` binds criterion ``AC1``, the label form the falsifier path uses."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(["AC1"], [_item("dod-a", _TEST_A, ["ac-1"])]),
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.acceptance_unbound_criteria == ()


def test_contract_without_declared_criteria_keeps_its_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``requirements[].acceptance`` means nothing to bind: unchanged."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(None, [_item("dod-a", _TEST_A, None)]),
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.acceptance_unbound_criteria == ()


def test_failing_bound_check_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unbound refusal only demotes VERIFIED; a failure stays FAILED."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2"],
            [_item("dod-a", _TEST_A, ["AC1"]), _item("dod-b", _TEST_B, None)],
        ),
        failing=frozenset({_TEST_A}),
    )
    assert state.status is EnumDodVerifyStatus.FAILED
    assert state.acceptance_unbound_criteria == ("AC2",)


def test_unbound_head_is_incomplete_criterion_coverage_in_the_difference_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The S5 difference step names the unbound refusal, not readback_only."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2"],
            [_item("dod-a", _TEST_A, ["AC1"]), _item("dod-b", _TEST_B, None)],
        ),
    )
    dod_dir = tmp_path / "dod"
    dod_dir.mkdir()
    (dod_dir / f"head-{_TICKET}.json").write_text(
        state.model_dump_json(), encoding="utf-8"
    )
    (dod_dir / f"base-{_TICKET}.control.txt").write_text("passed\n")
    head = json.loads((dod_dir / f"head-{_TICKET}.json").read_text())
    assert head["acceptance_unbound_criteria"] == ["AC2"]
    verdict = load_new_verdict(dod_dir, [_TICKET])
    assert not verdict.admitted
    assert verdict.reason == "incomplete_criterion_coverage"


def test_failed_head_beside_an_unbound_criterion_stays_unclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing bound test is a behavioural refusal; no reason code hides it."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2"],
            [_item("dod-a", _TEST_A, ["AC1"]), _item("dod-b", _TEST_B, None)],
        ),
        failing=frozenset({_TEST_A}),
    )
    dod_dir = tmp_path / "dod"
    dod_dir.mkdir()
    (dod_dir / f"head-{_TICKET}.json").write_text(
        state.model_dump_json(), encoding="utf-8"
    )
    (dod_dir / f"base-{_TICKET}.control.txt").write_text("passed\n")
    verdict = load_new_verdict(dod_dir, [_TICKET])
    assert not verdict.admitted
    assert verdict.reason is None
