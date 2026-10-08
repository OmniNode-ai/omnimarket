# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same-head OCC retirement S5 difference check coverage (OMN-20072)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnimarket.enums.enum_occ_verdict_difference_reason import (
    EnumOccVerdictDifferenceReason,
)
from omnimarket.nodes.node_dod_verify.__main__ import main
from omnimarket.nodes.node_dod_verify.models.model_occ_verdict_difference import (
    ModelNewPathVerdict,
    ModelOccVerdict,
)
from omnimarket.nodes.node_dod_verify.services.occ_verdict_difference import (
    EXPECTED_DIFFERENCES,
    classify,
    load_new_verdict,
    load_occ_verdict,
    parse_occ_verdict,
)

pytestmark = pytest.mark.unit


def test_inventory_exact() -> None:
    """Pin the published table, including the PR code's capitalization."""
    assert EXPECTED_DIFFERENCES == {
        "readback_only": ("may_admit", "refuse", True),
        "incomplete_criterion_coverage": ("may_admit", "refuse", True),
        "circular_contract": ("may_admit", "refuse", True),
        "final_newline": ("admits_after_receipt_hash_recompute", "refuse", True),
        "PR_number_only_binding": ("admits_stale_or_foreign_commit", "refuse", True),
        "contract_in_another_repo": ("may_admit", "refuse", True),
        "foreign_policy_outside_declared_manifest": ("may_refuse", "admit", True),
        "old_behavioral_refusal": ("refuse", "admit", False),
        "unclassified": ("any", "any", False),
        "accepted_negative_control": ("any", "admit", False),
    }
    assert {reason.value for reason in EnumOccVerdictDifferenceReason} == set(
        EXPECTED_DIFFERENCES
    )


@pytest.mark.parametrize(
    "reason",
    [
        "readback_only",
        "incomplete_criterion_coverage",
        "circular_contract",
        "final_newline",
        "PR_number_only_binding",
        "contract_in_another_repo",
    ],
)
def test_new_path_stricter_expected_difference(reason: str) -> None:
    result = classify(
        ModelOccVerdict(admitted=True, conclusion="success"),
        ModelNewPathVerdict(admitted=False, reason=reason),
        negative_control=False,
    )
    assert result.passed
    assert result.outcome == "expected_difference"
    assert result.reason_code == reason
    assert result.old_admitted is True
    assert result.new_admitted is False
    assert "OCC=admitted" in result.message
    assert "new=refused" in result.message
    assert "reason=null" in result.message
    assert f'reason="{reason}"' in result.message
    assert f"reason_code={reason}" in result.message


def test_occ_foreign_policy_expected_difference() -> None:
    result = classify(
        ModelOccVerdict(admitted=False, conclusion="failure", reason="occ_not_on_main"),
        ModelNewPathVerdict(admitted=True),
        negative_control=False,
    )
    assert result.passed
    assert result.outcome == "expected_difference"
    assert result.reason_code == "foreign_policy_outside_declared_manifest"
    assert result.old_reason == "occ_not_on_main"


@pytest.mark.parametrize(
    ("old_admitted", "old_reason", "new_reason"),
    [
        (True, None, None),
        (True, None, "unknown"),
        (True, None, "foreign_policy_outside_declared_manifest"),
        (False, "missing_receipt", None),
        (False, "unknown_occ_reason", None),
        (False, None, None),
    ],
)
def test_unclassified_difference(
    old_admitted: bool, old_reason: str | None, new_reason: str | None
) -> None:
    result = classify(
        ModelOccVerdict(admitted=old_admitted, reason=old_reason),
        ModelNewPathVerdict(admitted=not old_admitted, reason=new_reason),
        negative_control=False,
    )
    assert not result.passed
    assert result.outcome == "unclassified_difference"
    assert result.reason_code == "unclassified"


@pytest.mark.parametrize(
    "reason",
    [
        "nonpass_receipt",
        "goal_attempt_nonpass",
        "goal_criterion_coverage_missing",
        "goal_criterion_baseline_mismatch",
    ],
)
def test_old_behavioural_refusal_forbidden(reason: str) -> None:
    result = classify(
        ModelOccVerdict(admitted=False, conclusion="failure", reason=reason),
        ModelNewPathVerdict(admitted=True),
        negative_control=False,
    )
    assert not result.passed
    assert result.outcome == "forbidden_difference"
    assert result.reason_code == "old_behavioral_refusal"
    assert "OCC=refused" in result.message
    assert "new=admitted" in result.message
    assert reason in result.message


@pytest.mark.parametrize("old_admitted", [True, False, None])
def test_accepted_negative_control_takes_precedence(old_admitted: bool | None) -> None:
    result = classify(
        ModelOccVerdict(admitted=old_admitted, reason="occ_not_on_main"),
        ModelNewPathVerdict(admitted=True),
        negative_control=True,
    )
    assert not result.passed
    assert result.outcome == "forbidden_difference"
    assert result.reason_code == "accepted_negative_control"


@pytest.mark.parametrize("old_admitted", [True, False, None])
def test_refused_negative_control_passes(old_admitted: bool | None) -> None:
    result = classify(
        ModelOccVerdict(admitted=old_admitted, reason="nonpass_receipt"),
        ModelNewPathVerdict(admitted=False),
        negative_control=True,
    )
    assert result.passed
    assert result.outcome == "negative_control_refused"
    assert result.reason_code is None
    assert "new=refused" in result.message


@pytest.mark.parametrize("admitted", [True, False])
def test_agreement_passes(admitted: bool) -> None:
    result = classify(
        ModelOccVerdict(admitted=admitted, reason="nonpass_receipt"),
        ModelNewPathVerdict(admitted=admitted, reason="readback_only"),
        negative_control=False,
    )
    assert result.passed
    assert result.outcome == "agree"
    assert result.reason_code is None
    assert "no reason code" in result.message


@pytest.mark.parametrize(
    "conclusion",
    [
        "cancelled",
        None,
        "pending",
        "timed_out",
        "skipped",
        "neutral",
        "stale",
        "action_required",
        "unknown_conclusion",
    ],
)
def test_occ_unavailable(conclusion: str | None) -> None:
    old = parse_occ_verdict({"conclusion": conclusion})
    result = classify(old, ModelNewPathVerdict(admitted=False), negative_control=False)
    assert not result.passed
    assert result.outcome == "not_compared"
    assert result.reason_code is None
    assert result.old_admitted is None
    assert "OCC verdict unavailable" in result.message
    assert f"conclusion={json.dumps(conclusion)}" in result.message
    assert "new=refused" in result.message
    assert "no reason code" in result.message


def test_occ_annotation_first_matching_reason(tmp_path: Path) -> None:
    path = tmp_path / "occ.json"
    path.write_text(
        json.dumps(
            {
                "conclusion": "failure",
                "annotations": [
                    {"message": "unrelated"},
                    {
                        "message": "prefix OCC PREFLIGHT FAILED: reason=nonpass_receipt end"
                    },
                    {"message": "OCC PREFLIGHT FAILED: reason=occ_not_on_main"},
                ],
            }
        ),
        encoding="utf-8",
    )
    verdict = load_occ_verdict(path)
    assert verdict.admitted is False
    assert verdict.conclusion == "failure"
    assert verdict.reason == "nonpass_receipt"


@pytest.mark.parametrize("reason", ["future_unknown_reason", "PR_number_only_binding"])
def test_occ_annotation_unknown_values_preserved(reason: str) -> None:
    verdict = parse_occ_verdict(
        {
            "conclusion": "failure",
            "annotations": [{"message": f"OCC PREFLIGHT FAILED: reason={reason}"}],
        }
    )
    assert verdict.reason == reason


@pytest.mark.parametrize("annotations", [[], [{"message": "no reason"}], None])
def test_occ_refusal_without_reason(annotations: object) -> None:
    verdict = parse_occ_verdict({"conclusion": "failure", "annotations": annotations})
    assert verdict.admitted is False
    assert verdict.reason is None


def test_occ_success_ignores_annotation_reason() -> None:
    verdict = parse_occ_verdict(
        {
            "conclusion": "success",
            "annotations": [
                {"message": "OCC PREFLIGHT FAILED: reason=nonpass_receipt"}
            ],
        }
    )
    assert verdict.admitted is True
    assert verdict.reason is None


@pytest.mark.parametrize("content", [None, "null", "[]", "not JSON", "{} {}"])
def test_occ_missing_or_invalid_json(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "occ.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert load_occ_verdict(path).admitted is None


def _write_ticket(
    directory: Path, head: object, control: str | None, ticket: str = "OMN-20072"
) -> None:
    (directory / f"head-{ticket}.json").write_text(json.dumps(head), encoding="utf-8")
    if control is not None:
        (directory / f"base-{ticket}.control.txt").write_text(control, encoding="utf-8")


def test_omnibase_core_1907_shape_is_contract_in_another_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The caller stops before writing a head when the product contract is elsewhere."""
    tickets = ["OMN-20704"]
    (tmp_path / "contract-home-OMN-20704.txt").write_text(
        "OmniNode-ai/omnimarket\n", encoding="utf-8"
    )
    tickets_file = tmp_path / "tickets.txt"
    tickets_file.write_text("OMN-20704\n", encoding="utf-8")
    occ_file = tmp_path / "occ.json"
    occ_file.write_text('{"conclusion": "success"}', encoding="utf-8")

    new = load_new_verdict(tmp_path, tickets)
    assert new.admitted is False
    assert new.reason == "contract_in_another_repo"
    result = classify(load_occ_verdict(occ_file), new, negative_control=False)
    assert result.passed is True
    assert result.outcome == "expected_difference"
    assert result.reason_code == "contract_in_another_repo"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "node_dod_verify",
            "occ-difference",
            "--dod-dir",
            str(tmp_path),
            "--tickets-file",
            str(tickets_file),
            "--occ-check-run",
            str(occ_file),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    assert printed == result.model_dump(mode="json")
    assert printed["passed"] is True
    assert printed["outcome"] == "expected_difference"
    assert printed["reason_code"] == "contract_in_another_repo"
    assert printed["old_admitted"] is True
    assert printed["new_admitted"] is False
    assert printed["old_reason"] is None
    assert printed["new_reason"] == "contract_in_another_repo"
    assert captured.err == ""


@pytest.mark.parametrize(
    "marker",
    [
        "onex_change_control\n",
        "OmniNode-ai/onex_change_control\n",
        "\n  OmniNode-ai/onex_change_control  \nOmniNode-ai/omnimarket\n",
    ],
)
def test_onex_change_control_only_contract_is_contract_in_another_repo(
    tmp_path: Path, marker: str
) -> None:
    """omniclaude#2591 has no head or base control and an OCC-only contract."""
    (tmp_path / "contract-home-OMN-18983.txt").write_text(marker, encoding="utf-8")
    new = load_new_verdict(tmp_path, ["OMN-18983"])
    assert new.admitted is False
    assert new.reason == "contract_in_another_repo"
    result = classify(ModelOccVerdict(admitted=True), new, negative_control=False)
    assert result.passed is True
    assert result.outcome == "expected_difference"
    assert result.reason_code == "contract_in_another_repo"


def test_omnibase_infra_4725_shape_is_contract_in_another_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The caller stops before writing a head when the contract is in OCC."""
    (tmp_path / "contract-home-OMN-16106.txt").write_text(
        "OmniNode-ai/onex_change_control\n", encoding="utf-8"
    )
    tickets_file = tmp_path / "tickets.txt"
    tickets_file.write_text("OMN-16106\n", encoding="utf-8")
    occ_file = tmp_path / "occ.json"
    occ_file.write_text('{"conclusion": "success"}', encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "node_dod_verify",
            "occ-difference",
            "--dod-dir",
            str(tmp_path),
            "--tickets-file",
            str(tickets_file),
            "--occ-check-run",
            str(occ_file),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    assert printed["passed"] is True
    assert printed["outcome"] == "expected_difference"
    assert printed["reason_code"] == "contract_in_another_repo"
    assert printed["old_admitted"] is True
    assert printed["new_admitted"] is False
    assert printed["old_reason"] is None
    assert printed["new_reason"] == "contract_in_another_repo"
    assert captured.err == ""


@pytest.mark.parametrize("marker", ["", "\n \n", None])
def test_contract_home_absent_or_empty_stays_unclassified(
    tmp_path: Path, marker: str | None
) -> None:
    if marker is not None:
        (tmp_path / "contract-home-OMN-20704.txt").write_text(marker, encoding="utf-8")
    new = load_new_verdict(tmp_path, ["OMN-20704"])
    assert new.admitted is False
    assert new.reason is None
    result = classify(ModelOccVerdict(admitted=True), new, negative_control=False)
    assert result.passed is False
    assert result.outcome == "unclassified_difference"
    assert result.reason_code == "unclassified"


@pytest.mark.parametrize("unreadable", ["directory", "invalid_utf8"])
def test_unreadable_contract_home_stays_unclassified(
    tmp_path: Path, unreadable: str
) -> None:
    marker = tmp_path / "contract-home-OMN-20704.txt"
    if unreadable == "directory":
        marker.mkdir()
    else:
        marker.write_bytes(b"\xff")
    new = load_new_verdict(tmp_path, ["OMN-20704"])
    assert new.admitted is False
    assert new.reason is None
    result = classify(ModelOccVerdict(admitted=True), new, negative_control=False)
    assert result.passed is False
    assert result.outcome == "unclassified_difference"


@pytest.mark.parametrize("head_content", [b"null", b"not JSON", b"\xff", None])
@pytest.mark.parametrize(
    "marker",
    ["omnimarket", "\n  OmniNode-ai/omnimarket  \n", "OmniNode-ai/ONEX_CHANGE_CONTROL"],
)
def test_unloaded_head_uses_first_nonempty_case_sensitive_contract_home(
    tmp_path: Path, head_content: bytes | None, marker: str
) -> None:
    head = tmp_path / "head-OMN-20704.json"
    if head_content is None:
        head.mkdir()
    else:
        head.write_bytes(head_content)
    (tmp_path / "contract-home-OMN-20704.txt").write_text(marker, encoding="utf-8")
    new = load_new_verdict(tmp_path, ["OMN-20704"])
    assert new.admitted is False
    assert new.reason == "contract_in_another_repo"


def test_verified_head_and_passed_control_ignore_contract_home(tmp_path: Path) -> None:
    _write_ticket(tmp_path, {"status": "verified"}, "passed", "OMN-20704")
    (tmp_path / "contract-home-OMN-20704.txt").write_text(
        "OmniNode-ai/omnimarket\n", encoding="utf-8"
    )
    new = load_new_verdict(tmp_path, ["OMN-20704"])
    assert new.admitted is True
    assert new.reason is None


@pytest.mark.parametrize(
    ("head", "control", "admitted", "reason"),
    [
        ({"status": "verified", "checks": []}, "passed: base refused\n", True, None),
        (
            {"status": "verified", "checks": []},
            "refused\n",
            False,
            "incomplete_criterion_coverage",
        ),
        (
            {"status": "verified", "checks": []},
            None,
            False,
            "incomplete_criterion_coverage",
        ),
        (
            {"status": "verified", "checks": []},
            "",
            False,
            "incomplete_criterion_coverage",
        ),
        (
            {"status": "verified", "checks": []},
            "refused\npassed",
            False,
            "incomplete_criterion_coverage",
        ),
        (
            {"status": "verified", "checks": [{"binds_ac": ["AC1"]}]},
            "refused",
            False,
            None,
        ),
        (
            {"status": "failed", "error_message": "NO_ACCEPTANCE_CHECKS: none"},
            "passed",
            False,
            "readback_only",
        ),
        (
            {"status": "failed", "error_message": "NO_PROBATIVE_EVIDENCE: none"},
            "passed",
            False,
            "readback_only",
        ),
        ({"status": "failed", "error_message": "other failure"}, "passed", False, None),
        ({"status": "skipped"}, "passed", False, None),
        ({"status": "unresolved"}, "passed", False, None),
    ],
)
def test_new_ticket_verdict(
    tmp_path: Path,
    head: object,
    control: str | None,
    admitted: bool,
    reason: str | None,
) -> None:
    _write_ticket(tmp_path, head, control)
    verdict = load_new_verdict(tmp_path, ["OMN-20072"])
    assert verdict.admitted is admitted
    assert verdict.reason == reason


@pytest.mark.parametrize(
    ("checks", "reason"),
    [
        (
            [{"status": "failed", "binds_ac": ["AC1"], "proof_class": "merge-state"}],
            "circular_contract",
        ),
        ([{"status": "failed", "binds_ac": [], "proof_class": "merge-state"}], None),
        (
            [{"status": "verified", "binds_ac": ["AC1"], "proof_class": "merge-state"}],
            None,
        ),
        ([{"status": "failed", "binds_ac": ["AC1"], "proof_class": "behavior"}], None),
        (
            [
                {"status": "failed", "binds_ac": ["AC1"], "proof_class": "merge-state"},
                {"status": "failed", "binds_ac": ["AC2"], "proof_class": "behavior"},
            ],
            None,
        ),
        (
            [
                {"status": "failed", "binds_ac": ["AC1"], "proof_class": "merge-state"},
                {"status": "failed", "binds_ac": [], "proof_class": "behavior"},
                {"status": "verified", "binds_ac": ["AC2"], "proof_class": "behavior"},
            ],
            "circular_contract",
        ),
    ],
)
def test_new_circular_contract(
    tmp_path: Path, checks: list[dict[str, object]], reason: str | None
) -> None:
    _write_ticket(tmp_path, {"status": "failed", "checks": checks}, "passed")
    verdict = load_new_verdict(tmp_path, ["OMN-20072"])
    assert not verdict.admitted
    assert verdict.reason == reason


def test_new_error_prefix_precedes_circular_checks(tmp_path: Path) -> None:
    _write_ticket(
        tmp_path,
        {
            "status": "failed",
            "error_message": "NO_PROBATIVE_EVIDENCE",
            "checks": [
                {"status": "failed", "binds_ac": ["AC1"], "proof_class": "merge-state"}
            ],
        },
        "passed",
    )
    assert load_new_verdict(tmp_path, ["OMN-20072"]).reason == "readback_only"


@pytest.mark.parametrize("content", [None, "null", "[]", "not JSON", "{} {}"])
def test_new_head_missing_or_not_one_object(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "head-OMN-20072.json").write_text(content, encoding="utf-8")
    (tmp_path / "base-OMN-20072.control.txt").write_text("passed", encoding="utf-8")
    verdict = load_new_verdict(tmp_path, ["OMN-20072"])
    assert not verdict.admitted
    assert verdict.reason is None


def test_new_all_tickets_required_and_first_sorted_refusal(tmp_path: Path) -> None:
    _write_ticket(tmp_path, {"status": "verified"}, "passed", "OMN-1")
    _write_ticket(
        tmp_path,
        {"status": "failed", "error_message": "NO_ACCEPTANCE_CHECKS"},
        "passed",
        "OMN-2",
    )
    _write_ticket(tmp_path, {"status": "verified", "checks": []}, "refused", "OMN-3")
    assert (
        load_new_verdict(tmp_path, ["OMN-3", "OMN-1", "OMN-2"]).reason
        == "readback_only"
    )
    _write_ticket(tmp_path, {"status": "verified"}, "passed", "OMN-2")
    assert (
        load_new_verdict(tmp_path, ["OMN-3", "OMN-1", "OMN-2"]).reason
        == "incomplete_criterion_coverage"
    )
    _write_ticket(tmp_path, {"status": "verified"}, "passed", "OMN-3")
    assert load_new_verdict(tmp_path, ["OMN-3", "OMN-1", "OMN-2"]).admitted


def test_new_empty_ticket_list_refuses(tmp_path: Path) -> None:
    assert not load_new_verdict(tmp_path, []).admitted


@pytest.mark.parametrize(
    ("case", "expected_exit", "outcome", "reason"),
    [
        ("expected", 0, "expected_difference", "readback_only"),
        ("unclassified", 1, "unclassified_difference", "unclassified"),
        ("negative", 1, "forbidden_difference", "accepted_negative_control"),
        ("missing_occ", 1, "not_compared", None),
        ("agree", 0, "agree", None),
    ],
)
def test_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected_exit: int,
    outcome: str,
    reason: str | None,
) -> None:
    tickets_file = tmp_path / "tickets.txt"
    tickets_file.write_text("\nOMN-20072\n\n", encoding="utf-8")
    occ_file = tmp_path / "occ.json"
    if case != "missing_occ":
        occ_file.write_text('{"conclusion": "success"}', encoding="utf-8")
    head: dict[str, object] = {"status": "verified"}
    if case in {"expected", "unclassified"}:
        head = {"status": "failed"}
    if case == "expected":
        head["error_message"] = "NO_ACCEPTANCE_CHECKS"
    _write_ticket(tmp_path, head, "passed")
    argv = [
        "node_dod_verify",
        "occ-difference",
        "--dod-dir",
        str(tmp_path),
        "--tickets-file",
        str(tickets_file),
        "--occ-check-run",
        str(occ_file),
    ]
    if case == "negative":
        argv.append("--negative-control")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == expected_exit
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["passed"] is (expected_exit == 0)
    assert result["outcome"] == outcome
    assert result["reason_code"] == reason
    assert result["old_admitted"] is (None if case == "missing_occ" else True)
    assert result["new_admitted"] is (case not in {"expected", "unclassified"})
    assert result["old_reason"] is None
    assert result["new_reason"] == ("readback_only" if case == "expected" else None)
    assert "\n" not in result["message"]
    assert captured.err == (f"::error::{result['message']}\n" if expected_exit else "")
