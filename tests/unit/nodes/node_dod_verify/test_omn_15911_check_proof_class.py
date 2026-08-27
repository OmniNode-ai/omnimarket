# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15911 — a dod_verify verdict must say WHAT each green check proved.

Before this ticket, ``ModelEvidenceCheckResult`` carried only
``status: verified``. A check that reads a PR's merge state off GitHub and a
check that executes the ticket's tests both terminate in the identical
``verified``, so a downstream tally of "N/N verified" cannot tell
"this ticket's code landed" from "this ticket's behavior works". Every
consuming lane (autoclose, merge-sweep, closeout) reads that tally as
completion.

These tests pin the discrimination:

* per-check classification is derived from the COMMAND, never from
  author-supplied ``check_type``/``description`` prose (OMN-15391: the prose
  and the command are allowed to disagree and nothing checks them);
* the verdict carries the class and a ``behavior_proving_count`` rollup;
* two contracts that BOTH end ``status: verified`` — one merge-state-only, one
  with a real executed test — differ in that rollup.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_check,
    classify_item_checks,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# 1. Per-check classification (pure, command-derived)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # An ASSERTED merge probe — it can go red, so OMN-15391 correctly
        # calls it probative. It still proves only that a merge happened,
        # which is the residual that module records as out of its scope and
        # this axis exists to catch.
        "gh pr view 1559 --repo OmniNode-ai/omnibase_core --json state "
        "--jq '.state' | grep -q MERGED",
        "gh pr checks 2166 --repo OmniNode-ai/omnimarket",
        "gh api repos/OmniNode-ai/omnimarket/pulls/2166",
        "git merge-base --is-ancestor abc1234 origin/dev",
        "git log --oneline -1 origin/dev",
    ],
)
def test_asserted_merge_probes_classify_merge_state(command: str) -> None:
    """A command whose only effect is reading merge/PR state proves a merge."""
    assert (
        classify_check({"check_type": "command", "check_value": command})
        is EnumCheckProofClass.MERGE_STATE
    )


@pytest.mark.parametrize(
    "command",
    [
        # The exact shapes node_occ_companion_compute autobinds into every
        # generated contract — including this ticket's own,
        # contracts/OMN-15911.yaml on onex_change_control@dev.
        "gh pr view 1559 --repo OmniNode-ai/omnibase_core --json number,state",
        "gh pr view 1559 --repo OmniNode-ai/omnibase_core --json files",
    ],
)
def test_bare_pr_view_is_a_surrogate_via_the_omn15391_predicate(
    command: str,
) -> None:
    """A bare ``gh pr view`` cannot go red at all — it is not even merge proof.

    Delegated to ``omnimarket.occ_evidence_probative_class.is_surrogate_check_value``
    (OMN-15391): ``gh pr view`` exits 0 iff the PR is visible, so it is green
    for every PR on GitHub, before the change and with the change reverted.
    That is provenance, not a merge verdict — which is why it lands in
    SURROGATE rather than MERGE_STATE. One definition of the surrogate corpus,
    shared between the two lanes.
    """
    assert (
        classify_check({"check_type": "command", "check_value": command})
        is EnumCheckProofClass.SURROGATE
    )


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/unit/nodes/node_dod_verify -q",
        "python3 -m pytest tests/unit -q",
        "pytest -m unit",
        "make test-unit",
        "npm test",
        "cargo test --locked",
        "go test ./...",
        "uv run onex skill dod_verify OMN-15911",
    ],
)
def test_executed_suites_and_cli_runs_classify_behavior(command: str) -> None:
    """A command that executes a test runner or the product CLI proves behavior."""
    assert (
        classify_check({"check_type": "command", "check_value": command})
        is EnumCheckProofClass.BEHAVIOR
    )


@pytest.mark.parametrize(
    "check",
    [
        {"check_type": "file_exists", "check_value": "docs/plans/some-plan.md"},
        {"check_type": "command", "check_value": "test -f docs/plans/some-plan.md"},
        {"check_type": "command", "check_value": "grep -q 'proof_class' README.md"},
        {"check_type": "command", "check_value": "jq -e '.status' receipt.json"},
    ],
)
def test_static_artifact_inspection_classifies_surrogate(
    check: dict[str, str],
) -> None:
    """Reading a file proves an artifact exists, not that anything works."""
    assert classify_check(check) is EnumCheckProofClass.SURROGATE


def test_occ_admissibility_suite_is_a_surrogate_not_behavior() -> None:
    """OMN-15391's measured stand-in: a pytest shape that proves nothing here.

    ``uv run pytest tests/test_evidence_admissibility.py -q`` is OCC's own
    OMN-15309 admissibility-predicate suite. It is genuinely executed and
    genuinely falsifiable, which is why every existing gate passes it — and it
    is ticket-independent by construction, so it can never be a foreign
    ticket's proof. Measured at 29 occurrences across 13 OCC contracts
    (OMN-15391), including this ticket's own contract. The command shape says
    BEHAVIOR; the denylist is what makes the verdict honest.
    """
    check = {
        "check_type": "command",
        "check_value": "uv run pytest tests/test_evidence_admissibility.py -q",
    }
    assert classify_check(check) is EnumCheckProofClass.SURROGATE


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/unit -q || true",
        "pytest tests/unit -q || echo 'ok'",
        "uv run pytest tests/unit -q; true",
    ],
)
def test_exit_code_laundering_never_counts_as_behavior(command: str) -> None:
    """A test run whose failure is swallowed proves nothing — fail closed."""
    assert (
        classify_check({"check_type": "command", "check_value": command})
        is not EnumCheckProofClass.BEHAVIOR
    )


def test_unrecognized_command_shape_fails_closed_to_indeterminate() -> None:
    """Unclassifiable is never BEHAVIOR — the flip rule must not be released."""
    check = {"check_type": "command", "check_value": "./scripts/mystery_wrapper.sh"}
    assert classify_check(check) is EnumCheckProofClass.INDETERMINATE


def test_declared_check_type_alone_cannot_manufacture_behavior() -> None:
    """``check_type: test_passes`` is author-supplied prose, not evidence.

    OMN-15391's whole finding is that the authored surface and the executed
    surface are allowed to disagree. Classification therefore reads the
    command.
    """
    check = {"check_type": "test_passes", "check_value": "test -f pyproject.toml"}
    assert classify_check(check) is EnumCheckProofClass.SURROGATE


def test_item_rollup_takes_the_strongest_class_actually_executed() -> None:
    """An item is VERIFIED only if every check passed, so the strongest holds."""
    checks = [
        {
            "check_type": "command",
            "check_value": (
                "gh pr view 1 --repo o/r --json state --jq '.state' | grep -q MERGED"
            ),
        },
        {"check_type": "command", "check_value": "uv run pytest tests/unit -q"},
    ]
    assert classify_item_checks(checks) is EnumCheckProofClass.BEHAVIOR

    merge_only = [
        {
            "check_type": "command",
            "check_value": (
                "gh pr view 1 --repo o/r --json state --jq '.state' | grep -q MERGED"
            ),
        },
        {"check_type": "file_exists", "check_value": "README.md"},
    ]
    assert classify_item_checks(merge_only) is EnumCheckProofClass.MERGE_STATE

    assert classify_item_checks([]) is EnumCheckProofClass.INDETERMINATE


# --------------------------------------------------------------------------
# 2. The verdict model carries the class
# --------------------------------------------------------------------------


def test_check_result_carries_proof_class_and_defaults_closed() -> None:
    result = ModelEvidenceCheckResult(
        evidence_id="dod-001",
        description="anything",
        status=EnumEvidenceCheckStatus.VERIFIED,
    )
    assert result.proof_class is EnumCheckProofClass.INDETERMINATE


def test_state_behavior_proving_count_only_counts_verified_behavior() -> None:
    """A FAILED behavior check is not proof — it must not release the flip."""
    handler = HandlerDodVerify()
    command = ModelDodVerifyStartCommand(ticket_id="OMN-15911", correlation_id=uuid4())
    state = handler._handle_typed(
        command,
        [
            ModelEvidenceCheckResult(
                evidence_id="dod-merge",
                description="PR merged",
                status=EnumEvidenceCheckStatus.VERIFIED,
                proof_class=EnumCheckProofClass.MERGE_STATE,
            ),
            ModelEvidenceCheckResult(
                evidence_id="dod-behavior",
                description="tests run",
                status=EnumEvidenceCheckStatus.FAILED,
                proof_class=EnumCheckProofClass.BEHAVIOR,
            ),
        ],
    )
    assert state.behavior_proving_count == 0


# --------------------------------------------------------------------------
# 3. DoD item 3 — end-to-end discrimination on two real contracts
# --------------------------------------------------------------------------


def _write_contract(path: Path, ticket_id: str, items: list[dict[str, object]]) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": ticket_id,
                "title": f"synthetic contract for {ticket_id}",
                "dod_evidence": items,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_merge_only_and_behavior_contracts_both_verify_but_differ(
    tmp_path: Path,
) -> None:
    """The ticket's central claim, executed.

    Two contracts. Both end ``status: verified`` with zero failures — which is
    all a consuming lane could see before this change. One proves only that a
    commit exists in git; the other executes a test runner. The verdicts must
    now be distinguishable.
    """
    handler = HandlerDodVerify()

    merge_only = _write_contract(
        tmp_path / "merge_only.yaml",
        "OMN-15911",
        [
            {
                "id": "dod-merge-state-only",
                "description": "Reads repo/merge state and nothing else.",
                "checks": [
                    {"check_type": "command", "check_value": "git rev-parse HEAD"},
                ],
            }
        ],
    )
    behavior = _write_contract(
        tmp_path / "behavior.yaml",
        "OMN-15911",
        [
            {
                "id": "dod-behavior-proving",
                "description": "Executes a test runner.",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": "python3 -m pytest --version",
                    },
                ],
            }
        ],
    )

    merge_state = handler._handle_typed(
        ModelDodVerifyStartCommand(
            ticket_id="OMN-15911",
            contract_path=str(merge_only),
            correlation_id=uuid4(),
        )
    )
    behavior_state = handler._handle_typed(
        ModelDodVerifyStartCommand(
            ticket_id="OMN-15911",
            contract_path=str(behavior),
            correlation_id=uuid4(),
        )
    )

    # Indistinguishable on the pre-OMN-15911 surface.
    assert merge_state.status is EnumDodVerifyStatus.VERIFIED
    assert behavior_state.status is EnumDodVerifyStatus.VERIFIED
    assert merge_state.verified_count == merge_state.total_checks == 1
    assert behavior_state.verified_count == behavior_state.total_checks == 1

    # Distinguishable on the surface this ticket adds.
    assert merge_state.checks[0].proof_class is EnumCheckProofClass.MERGE_STATE
    assert behavior_state.checks[0].proof_class is EnumCheckProofClass.BEHAVIOR
    assert merge_state.behavior_proving_count == 0
    assert behavior_state.behavior_proving_count == 1


def test_this_tickets_own_autobound_contract_proves_no_behavior(
    tmp_path: Path,
) -> None:
    """Byte-for-byte the live specimen: contracts/OMN-15911.yaml @ OCC dev.

    Two ``gh pr view`` reads and OCC's own admissibility suite. It is green,
    and it proves nothing about whether node_dod_verify discriminates proof.
    That is the defect this ticket exists to close, and the reason the flip
    rule needs more than a count.
    """
    contract = _write_contract(
        tmp_path / "OMN-15911.yaml",
        "OMN-15911",
        [
            {
                "id": "dod-OmniNode-ai-omnibase_core-pr-1559",
                "description": (
                    "PR #1559 on OmniNode-ai/omnibase_core — Evidence-Source autobind."
                ),
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": (
                            "gh pr view 1559 --repo OmniNode-ai/omnibase_core "
                            "--json number,state"
                        ),
                    },
                ],
            },
            {
                "id": "dod-occ-evidence-admissibility-validator",
                "description": "Hosted OCC evidence admissibility validator.",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": (
                            "uv run pytest tests/test_evidence_admissibility.py -q"
                        ),
                    },
                ],
            },
        ],
    )
    raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
    classes = [classify_item_checks(item["checks"]) for item in raw["dod_evidence"]]
    # Both legs are surrogates, by two different routes: the bare `gh pr view`
    # cannot go red at all, and the admissibility suite is ticket-independent
    # by construction. Neither says anything about this ticket's behavior.
    assert classes == [
        EnumCheckProofClass.SURROGATE,
        EnumCheckProofClass.SURROGATE,
    ]
    assert EnumCheckProofClass.BEHAVIOR not in classes
