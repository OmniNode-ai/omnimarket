# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same-head OCC retirement S5 difference check coverage (OMN-20072), and the
S7 replay with dependency re-pins as an expected difference (OMN-20917)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnimarket.enums.enum_occ_verdict_difference_reason import (
    EnumOccVerdictDifferenceReason,
)
from omnimarket.nodes.node_dod_verify.__main__ import main
from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    DodEvidenceLocalSource,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
    ModelPrHeadFacts,
)
from omnimarket.nodes.node_dod_verify.models.model_occ_replay import (
    ModelOccReplayRecord,
)
from omnimarket.nodes.node_dod_verify.models.model_occ_verdict_difference import (
    ModelNewPathVerdict,
    ModelOccVerdict,
)
from omnimarket.nodes.node_dod_verify.services.occ_replay import (
    must_fail_control_line,
    render_replay_table,
    replay_records,
)
from omnimarket.nodes.node_dod_verify.services.occ_verdict_difference import (
    EXPECTED_DIFFERENCES,
    classify,
    classify_dependency_repin,
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
        "dependency_repin": ("may_admit", "refuse", True),
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


# --------------------------------------------------------------------------
# OMN-20917: S7 replay of a repository's last N merged PRs, and dependency
# re-pins as an expected difference.
# --------------------------------------------------------------------------

_REPIN_BASE = '[project]\nname = "pkg"\nversion = "1.0.0"\ndependencies = ["omnibase-core==0.40.0"]\n'
_REPIN_HEAD = '[project]\nname = "pkg"\nversion = "1.0.0"\ndependencies = ["omnibase-core==0.41.0"]\n'
_BOUND_HEAD = {
    "status": "verified",
    "checks": [{"evidence_id": "dod-1", "binds_ac": ["AC1"], "status": "verified"}],
}


@pytest.mark.parametrize(
    "paths",
    [
        ["pyproject.toml", "uv.lock"],
        ["uv.lock"],
        ["package-lock.json"],
        ["web/pnpm-lock.yaml", "yarn.lock", "pyproject.toml"],
    ],
)
def test_dependency_repin_paths_only_pins_and_locks(paths: list[str]) -> None:
    repin, why = classify_dependency_repin(
        paths, pyproject_head=_REPIN_HEAD, pyproject_base=_REPIN_BASE
    )
    assert repin, why


@pytest.mark.parametrize(
    ("paths", "head"),
    [
        (["pyproject.toml", "uv.lock", "src/pkg/mod.py"], _REPIN_HEAD),
        (["uv.lock", "src/pkg/mod.py", "tests/test_mod.py"], _REPIN_HEAD),
        # Only the evidence side changed: nothing to re-pin.
        (["tests/test_mod.py"], _REPIN_HEAD),
        # A contract the title does not cite is not the evidence side.
        (["uv.lock", "contracts/OMN-1.yaml"], _REPIN_HEAD),
        (["package.json"], _REPIN_HEAD),
        ([], _REPIN_HEAD),
        (
            ["pyproject.toml"],
            _REPIN_HEAD + '\n[project.scripts]\nrun = "pkg.cli:main"\n',
        ),
        (["pyproject.toml"], None),
    ],
)
def test_dependency_repin_refuses_source_or_non_pin_change(
    paths: list[str], head: str | None
) -> None:
    repin, _why = classify_dependency_repin(
        paths, pyproject_head=head, pyproject_base=_REPIN_BASE
    )
    assert not repin


def test_dependency_repin_control_refusal_is_expected_difference(
    tmp_path: Path,
) -> None:
    """A pin-only PR whose bound test also passes at the merge base."""
    _write_ticket(tmp_path, _BOUND_HEAD, "refused\n", "OMN-20917")
    repin, _ = classify_dependency_repin(
        ["pyproject.toml", "uv.lock"],
        pyproject_head=_REPIN_HEAD,
        pyproject_base=_REPIN_BASE,
    )
    new = load_new_verdict(tmp_path, ["OMN-20917"], dependency_repin=repin)
    assert new.admitted is False
    assert new.reason == "dependency_repin"
    result = classify(ModelOccVerdict(admitted=True, conclusion="success"), new, False)
    assert result.passed is True
    assert result.outcome == "expected_difference"
    assert result.reason_code == "dependency_repin"


def test_dependency_repin_same_refusal_with_source_is_unclassified(
    tmp_path: Path,
) -> None:
    _write_ticket(tmp_path, _BOUND_HEAD, "refused\n", "OMN-20917")
    repin, _ = classify_dependency_repin(
        ["pyproject.toml", "uv.lock", "src/pkg/mod.py"],
        pyproject_head=_REPIN_HEAD,
        pyproject_base=_REPIN_BASE,
    )
    assert repin is False
    new = load_new_verdict(tmp_path, ["OMN-20917"], dependency_repin=repin)
    assert new.reason is None
    result = classify(ModelOccVerdict(admitted=True, conclusion="success"), new, False)
    assert result.passed is False
    assert result.outcome == "unclassified_difference"
    assert result.reason_code == "unclassified"


@pytest.mark.parametrize(
    ("head", "control", "reason"),
    [
        # A failed head is not a control refusal: the re-pin code never applies.
        ({"status": "failed", "checks": []}, "passed", None),
        # No bound check keeps the existing coverage reason.
        (
            {"status": "verified", "checks": []},
            "refused",
            "incomplete_criterion_coverage",
        ),
        # A passed control is admitted whatever the diff.
        (_BOUND_HEAD, "passed: every bound check failed", None),
    ],
)
def test_dependency_repin_only_names_a_bound_control_refusal(
    tmp_path: Path, head: object, control: str, reason: str | None
) -> None:
    _write_ticket(tmp_path, head, control, "OMN-20917")
    new = load_new_verdict(tmp_path, ["OMN-20917"], dependency_repin=True)
    assert new.reason == reason


def _replay_record(
    pr: int,
    occ: object,
    admitted: bool,
    reason: str | None = None,
    negative_control: bool = False,
) -> dict[str, object]:
    return {
        "pr": pr,
        "head_sha": f"{pr:040x}",
        "merged_at": f"2026-10-{pr % 28 + 1:02d}T00:00:00Z",
        "tickets": [f"OMN-{pr}"],
        "occ_check_run": occ,
        "new_verdict": {"admitted": admitted, "reason": reason},
        "negative_control": negative_control,
    }


_OCC_ADMIT = {"conclusion": "success"}


def _run_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    records: list[dict[str, object]],
    count: int,
) -> tuple[int, dict[str, object]]:
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "node_dod_verify",
            "occ-difference",
            "replay",
            "--repository",
            "OmniNode-ai/omnibase_spi",
            "--count",
            str(count),
            "--rows-file",
            str(rows_file),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    printed = json.loads(capsys.readouterr().out)
    assert isinstance(exc.value.code, int)
    return exc.value.code, printed


def _classified_records() -> list[dict[str, object]]:
    return [
        _replay_record(30, _OCC_ADMIT, True),
        _replay_record(29, None, False),  # no OCC run: not_compared
        _replay_record(28, _OCC_ADMIT, False, "contract_in_another_repo"),
        _replay_record(27, _OCC_ADMIT, False, "dependency_repin"),
        _replay_record(
            26,
            {
                "conclusion": "failure",
                "annotations": [
                    {"message": "OCC PREFLIGHT FAILED: reason=occ_not_on_main"}
                ],
            },
            True,
        ),
        _replay_record(25, None, True),  # no OCC run: not_compared
    ]


def test_replay_all_classified_rows_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, report = _run_replay(
        tmp_path, monkeypatch, capsys, _classified_records(), count=4
    )
    assert code == 0
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["compared"] == 4
    assert summary["not_compared"] == 1
    assert summary["target_met"] is True
    assert summary["passed"] is True
    assert summary["window_newest_pr"] == 30
    assert summary["window_oldest_pr"] == 26
    assert summary["by_reason_code"] == {
        "agree": 1,
        "contract_in_another_repo": 1,
        "dependency_repin": 1,
        "foreign_policy_outside_declared_manifest": 1,
    }
    rows = report["rows"]
    assert isinstance(rows, list)
    # Rows past the window that reached N compared rows are not reported.
    assert [row["pr"] for row in rows] == [30, 29, 28, 27, 26]
    assert rows[1]["outcome"] == "not_compared"


def test_replay_one_unclassified_row_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = _classified_records()
    records.insert(2, _replay_record(40, _OCC_ADMIT, False, None))
    code, report = _run_replay(tmp_path, monkeypatch, capsys, records, count=4)
    assert code == 1
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["passed"] is False
    assert summary["unclassified"] == 1
    rows = report["rows"]
    assert isinstance(rows, list)
    flagged = [row for row in rows if row["outcome"] == "unclassified_difference"]
    assert [row["pr"] for row in flagged] == [40]
    assert flagged[0]["reason_code"] == "unclassified"


@pytest.mark.parametrize(
    ("record", "reason_code"),
    [
        (
            _replay_record(
                41,
                {
                    "conclusion": "failure",
                    "annotations": [
                        {"message": "OCC PREFLIGHT FAILED: reason=nonpass_receipt"}
                    ],
                },
                True,
            ),
            "old_behavioral_refusal",
        ),
        (
            _replay_record(42, _OCC_ADMIT, True, negative_control=True),
            "accepted_negative_control",
        ),
    ],
)
def test_replay_forbidden_rows_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record: dict[str, object],
    reason_code: str,
) -> None:
    code, report = _run_replay(
        tmp_path, monkeypatch, capsys, [record, *_classified_records()], count=4
    )
    assert code == 1
    rows = report["rows"]
    assert isinstance(rows, list)
    assert rows[0]["outcome"] == "forbidden_difference"
    assert rows[0]["reason_code"] == reason_code


def test_replay_short_window_reports_how_far_back_and_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, report = _run_replay(
        tmp_path, monkeypatch, capsys, _classified_records(), count=30
    )
    assert code == 2
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["compared"] == 4
    assert summary["target"] == 30
    assert summary["target_met"] is False
    assert summary["examined"] == 6
    assert summary["window_oldest_pr"] == 25


def test_replay_markdown_table_names_every_column() -> None:
    report = replay_records(
        [ModelOccReplayRecord.model_validate(r) for r in _classified_records()],
        repository="OmniNode-ai/omnibase_spi",
        count=4,
    )
    table = render_replay_table(report)
    header = table.splitlines()[0]
    for column in (
        "pr",
        "head",
        "ticket",
        "OCC verdict",
        "OCC reason",
        "new-path verdict",
        "new-path reason",
        "outcome",
        "reason code",
    ):
        assert column in header
    assert "| 28 |" in table


@pytest.mark.parametrize(
    ("bound", "carried", "test_only", "head", "first"),
    [
        # Every own bound check fails at the merge base: the control passes.
        ([("dod-1", "failed")], set(), False, None, "passed"),
        # A bound check also passes at the merge base: always-pass.
        ([("dod-1", "verified")], set(), False, None, "refused"),
        # A bound check that did not run is not a pass.
        ([("dod-1", "skipped")], set(), False, None, "refused"),
        # Every bound check carried from the merge base's contract.
        ([("dod-1", "failed")], {"dod-1"}, False, None, "refused"),
        # A carried check is excluded; the own check failed.
        (
            [("dod-1", "verified"), ("dod-2", "failed")],
            {"dod-1"},
            False,
            None,
            "passed",
        ),
        # Test-only diff with verified head evidence for the own bound check.
        ([("dod-1", "verified")], set(), True, _BOUND_HEAD, "passed"),
        # Test-only diff without verified head evidence.
        ([("dod-1", "verified")], set(), True, {"status": "failed"}, "refused"),
    ],
)
def test_replay_must_fail_control_matches_receipt_gate(
    bound: list[tuple[str, str]],
    carried: set[str],
    test_only: bool,
    head: object,
    first: str,
) -> None:
    base = {
        "status": "failed",
        "checks": [
            {"evidence_id": eid, "binds_ac": ["AC1"], "status": status}
            for eid, status in bound
        ],
    }
    line = must_fail_control_line(
        base, head, carried_ids=carried, test_only=test_only, at_merge_base=True
    )
    assert line.split(":", 1)[0].split()[0] == first


def test_replay_must_fail_control_without_bound_checks_refuses() -> None:
    line = must_fail_control_line(
        {"status": "failed", "checks": [{"evidence_id": "x", "binds_ac": []}]},
        None,
        carried_ids=set(),
        test_only=False,
        at_merge_base=True,
    )
    assert line.startswith("refused")


# --------------------------------------------------------------------------
# OMN-20917 review fixes: the bot exemption reads the PR's GitHub login, an
# unavailable OCC verdict is ``unknown`` and fails the replay, and a pure pin
# bump PR (its own contract and tests beside the pins) is ``dependency_repin``.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    [
        ["pyproject.toml", "uv.lock", "contracts/OMN-20917.yaml"],
        [
            "pyproject.toml",
            "uv.lock",
            "contracts/OMN-20917.yaml",
            "tests/test_pins.py",
        ],
    ],
)
def test_dependency_repin_ignores_the_prs_own_evidence_side(
    paths: list[str],
) -> None:
    """The receipt gate needs the PR's contract (and its tests for the control)."""
    repin, why = classify_dependency_repin(
        paths,
        tickets=("OMN-20917",),
        pyproject_head=_REPIN_HEAD,
        pyproject_base=_REPIN_BASE,
    )
    assert repin, why


def test_dependency_repin_with_evidence_side_still_refuses_source() -> None:
    repin, _why = classify_dependency_repin(
        ["uv.lock", "src/pkg/mod.py", "contracts/OMN-20917.yaml", "tests/test_x.py"],
        tickets=("OMN-20917",),
        pyproject_head=_REPIN_HEAD,
        pyproject_base=_REPIN_BASE,
    )
    assert repin is False


def test_pure_pin_bump_pr_is_expected_difference(tmp_path: Path) -> None:
    """Pins, lock, the PR's contract and its always-pass test: dependency_repin."""
    _write_ticket(
        tmp_path,
        _BOUND_HEAD,
        "refused: [dod-1] bound test also passes at the control\n",
        "OMN-20917",
    )
    repin, why = classify_dependency_repin(
        ["pyproject.toml", "uv.lock", "contracts/OMN-20917.yaml", "tests/test_pins.py"],
        tickets=("OMN-20917",),
        pyproject_head=_REPIN_HEAD,
        pyproject_base=_REPIN_BASE,
    )
    assert repin, why
    new = load_new_verdict(tmp_path, ["OMN-20917"], dependency_repin=repin)
    result = classify(ModelOccVerdict(admitted=True, conclusion="success"), new, False)
    assert result.outcome == "expected_difference"
    assert result.reason_code == "dependency_repin"
    assert result.passed is True


@pytest.mark.parametrize(
    ("occ", "unreadable"),
    [
        ({"conclusion": "cancelled"}, False),
        ({"conclusion": "skipped"}, False),
        ({"conclusion": None}, False),
        (None, True),
    ],
)
def test_replay_unavailable_occ_verdict_is_unknown_and_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    occ: object,
    unreadable: bool,
) -> None:
    record = _replay_record(43, occ, True)
    if unreadable:
        record["occ_unreadable"] = True
    code, report = _run_replay(
        tmp_path, monkeypatch, capsys, [record, *_classified_records()], count=4
    )
    assert code == 1
    rows = report["rows"]
    assert isinstance(rows, list)
    assert rows[0]["outcome"] == "unknown"
    assert rows[0]["passed"] is False
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["unknown"] == 1
    assert summary["passed"] is False
    # Never counted as a compared (let alone agreeing) row.
    assert summary["compared"] == 4
    assert summary["by_reason_code"].get("agree") == 1


@pytest.mark.parametrize(
    ("occ", "unreadable"),
    [({"conclusion": "cancelled"}, False), (None, True), (None, False)],
)
def test_replay_accepted_negative_control_without_occ_verdict_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    occ: object,
    unreadable: bool,
) -> None:
    record = _replay_record(44, occ, True, negative_control=True)
    if unreadable:
        record["occ_unreadable"] = True
    code, report = _run_replay(
        tmp_path, monkeypatch, capsys, [record, *_classified_records()], count=4
    )
    assert code == 1
    rows = report["rows"]
    assert isinstance(rows, list)
    assert rows[0]["outcome"] == "forbidden_difference"
    assert rows[0]["reason_code"] == "accepted_negative_control"


# ---- HandlerOccReplay.handle over a real canonical clone and a fake effect.


def _git_run(repo: Path, *args: str, name: str = "Dev Person") -> str:
    import os
    import subprocess

    from omnibase_core.validators.no_unguarded_git_subprocess import (
        scrub_git_location_env,
    )

    identity = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": "dev@example.invalid",
        "GIT_COMMITTER_NAME": "GitHub",
        "GIT_COMMITTER_EMAIL": "noreply@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**scrub_git_location_env(os.environ), **identity},
    ).stdout.strip()


def _canonical_clone_with_pr(
    registry_root: Path, *, git_author_name: str
) -> tuple[str, str]:
    """omnimarket clone: base commit, a PR head bumping uv.lock, the squash."""
    repo = registry_root / "omnimarket"
    repo.mkdir(parents=True)
    _git_run(repo, "init", "--quiet", "-b", "main")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git_run(repo, "add", "uv.lock")
    _git_run(repo, "commit", "--quiet", "-m", "init")
    base = _git_run(repo, "rev-parse", "HEAD")
    _git_run(repo, "checkout", "--quiet", "-b", "pr-7")
    (repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    _git_run(repo, "commit", "--quiet", "-am", "bump")
    head = _git_run(repo, "rev-parse", "HEAD")
    _git_run(repo, "checkout", "--quiet", "main")
    _git_run(repo, "merge", "--quiet", "--squash", "pr-7")
    _git_run(
        repo, "commit", "--quiet", "-m", "chore: bump pins (#7)", name=git_author_name
    )
    _git_run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git_run(
        repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
    )
    return base, head


_OCC_ADMIT_RUN: dict[str, object] = {"conclusion": "success"}


class _FakeGithub(HandlerDodEvidenceGithubEffect):
    """The real effect handler with its lookups answered from fixed facts."""

    def __init__(
        self,
        *,
        head: str,
        login: str,
        labels: tuple[str, ...] = (),
        occ: dict[str, object] | None,
        occ_resolved: bool = True,
    ) -> None:
        super().__init__(local_source=DodEvidenceLocalSource())
        self._head, self._login, self._labels = head, login, labels
        self._occ, self._occ_resolved = occ, occ_resolved

    def _dispatch(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        if command.operation is EnumDodEvidenceGithubOperation.FETCH_PR_HEAD_FACTS:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                pr_head_facts=ModelPrHeadFacts(
                    head_sha=self._head,
                    author=self._login,
                    labels=self._labels,
                    title="chore: bump pins",
                ),
                detail="fake",
            )
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            resolved=self._occ_resolved,
            check_run=self._occ,
            detail="fake",
        )


def _handle_one_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    git_author_name: str,
    login: str,
    labels: tuple[str, ...] = (),
    occ: dict[str, object] | None = _OCC_ADMIT_RUN,
    occ_resolved: bool = True,
) -> dict[str, object]:
    from omnimarket.nodes.node_dod_verify.handlers.handler_occ_replay import (
        HandlerOccReplay,
    )
    from omnimarket.nodes.node_dod_verify.models.model_occ_replay import (
        ModelOccReplayRequest,
    )

    registry_root = tmp_path / "registry"
    _base, head = _canonical_clone_with_pr(
        registry_root, git_author_name=git_author_name
    )
    monkeypatch.setenv("OMNI_HOME", str(registry_root))
    fake = _FakeGithub(
        head=head, login=login, labels=labels, occ=occ, occ_resolved=occ_resolved
    )
    report = HandlerOccReplay(github=fake).handle(
        ModelOccReplayRequest(
            repository="OmniNode-ai/omnimarket",
            count=1,
            max_examined=1,
            work_dir=tmp_path / "work",
            verifier_python=Path(sys.executable),
        )
    )
    assert len(report.rows) == 1
    return report.rows[0].model_dump(mode="json")


@pytest.mark.parametrize(
    "spoofed_name",
    ["dependabot[bot]", "renovate", "onexbot-occ-writer[bot]"],
)
def test_handler_git_author_name_grants_no_bot_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoofed_name: str
) -> None:
    """A human PR whose squash commit carries a bot's author name is not exempt."""
    row = _handle_one_pr(
        tmp_path, monkeypatch, git_author_name=spoofed_name, login="some-human"
    )
    assert row["new_admitted"] is False
    assert not str(row["note"]).startswith("exempt")
    assert row["outcome"] == "unclassified_difference"


@pytest.mark.parametrize(
    "login", ["dependabot[bot]", "app/renovate", "onexbot-occ-writer[bot]"]
)
def test_handler_github_login_grants_the_bot_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: str
) -> None:
    row = _handle_one_pr(
        tmp_path, monkeypatch, git_author_name="Some Human", login=login
    )
    assert row["new_admitted"] is True
    assert str(row["note"]).startswith("exempt")
    assert row["outcome"] == "agree"


@pytest.mark.parametrize(
    ("occ", "occ_resolved"),
    [({"conclusion": "cancelled"}, True), (None, False), (None, True)],
)
def test_handler_accepted_negative_control_is_forbidden_without_occ_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occ: dict[str, object] | None,
    occ_resolved: bool,
) -> None:
    """A negative control the new path admits fails whatever OCC's side reads."""
    row = _handle_one_pr(
        tmp_path,
        monkeypatch,
        git_author_name="Some Human",
        login="dependabot[bot]",
        labels=("dod-negative-control",),
        occ=occ,
        occ_resolved=occ_resolved,
    )
    assert row["outcome"] == "forbidden_difference"
    assert row["reason_code"] == "accepted_negative_control"


@pytest.mark.parametrize(
    ("occ", "occ_resolved"), [({"conclusion": "cancelled"}, True), (None, False)]
)
def test_handler_unavailable_occ_verdict_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occ: dict[str, object] | None,
    occ_resolved: bool,
) -> None:
    row = _handle_one_pr(
        tmp_path,
        monkeypatch,
        git_author_name="Some Human",
        login="dependabot[bot]",
        occ=occ,
        occ_resolved=occ_resolved,
    )
    assert row["outcome"] == "unknown"
    assert row["passed"] is False
