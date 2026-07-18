# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the merge-controller branch-safety gate (OMN-14768 / F-11 + F-12).

F-11: the branch-rename scanner flags a planted rename invocation (positive
control, so a passing scan of the real tree is NOT vacuous) and the live
merge-controller tree is clean. F-12: the Evidence-Ticket branch-identity check
binds exactly like the Receipt Gate branch axis (including the OMN-13395
dual-ticket cluster), fails with the exact required fragment, and no-ops when no
Evidence-Ticket is present.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "check_branch_identity.py"
_spec = importlib.util.spec_from_file_location("check_branch_identity", _MODULE_PATH)
assert _spec is not None
assert _spec.loader is not None
cbi = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cbi
_spec.loader.exec_module(cbi)


# ---------------------------------------------------------------------------
# F-11 — branch-rename prohibition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'run(["gh", "api", "-X", "POST", "repos/O/R/branches/old/rename"])',
        "gh api --method PATCH repos/OmniNode-ai/omnidash/branches/x/rename",
        "subprocess.run(['git', 'branch', '-m', 'old', 'new'])",
        "git branch --move old new",
    ],
)
def test_rename_scanner_flags_planted_rename(line: str) -> None:
    findings = cbi.find_branch_rename_calls(line)
    assert findings, f"scanner missed a branch-rename invocation: {line!r}"


def test_rename_scanner_honors_annotation() -> None:
    line = "gh api repos/O/R/branches/x/rename  # branch-rename-ok: forensic quote"
    assert cbi.find_branch_rename_calls(line) == []


@pytest.mark.parametrize(
    "line",
    [
        "gh pr edit ${PR_NUMBER} --repo ${REPO} --add-label ready",
        "gh api repos/O/R/pulls/1 --json state",
        "git checkout -b jonah/omn-1-x",
        "self._rename_field = 'ok'  # a variable named rename is fine",
    ],
)
def test_rename_scanner_ignores_benign_lines(line: str) -> None:
    assert cbi.find_branch_rename_calls(line) == []


def test_rename_scan_on_real_merge_controller_tree_is_clean() -> None:
    # The live tree must have ZERO branch-rename calls (the preventive baseline).
    assert cbi.run_rename_scan([], _REPO_ROOT) == 0


def test_rename_scan_flags_a_planted_file(tmp_path: Path) -> None:
    # Positive control at the scan-runner level: a planted file fails the scan.
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def bad():\n    gh_api('repos/O/R/branches/x/rename')\n", encoding="utf-8"
    )
    assert cbi.run_rename_scan([planted], _REPO_ROOT) == 1


# ---------------------------------------------------------------------------
# F-12 — Evidence-Ticket branch-identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "ticket", "expected"),
    [
        ("jonah/omn-14766-emitter", "OMN-14766", True),
        ("jonah/OMN-14766-emitter", "omn-14766", True),  # case-insensitive
        ("jonah/feature-emitter", "OMN-14766", False),
        ("jonah/omn-1476-x", "OMN-14766", False),  # prefix must not partial-match
        ("jonah/omn-13234-13362-typed", "OMN-13362", True),  # dual-ticket (13395)
        ("jonah/omn-13234-13362-typed", "OMN-99999", False),
    ],
)
def test_branch_binds_ticket(branch: str, ticket: str, expected: bool) -> None:
    assert cbi.branch_binds_ticket(branch, ticket) is expected


def test_required_branch_hint() -> None:
    assert cbi.required_branch_hint("OMN-14766") == "omn-14766"
    assert cbi.required_branch_hint("omn-14766") == "omn-14766"


def test_extract_evidence_tickets() -> None:
    body = (
        "Some prose about the change.\n"
        "Evidence-Ticket: OMN-14766\n"
        "Evidence-Ticket: OMN-14766\n"  # dedup
        "Evidence-Ticket: OMN-13395\n"
        "not-a-ticket: OMN-9999\n"
    )
    assert cbi.extract_evidence_tickets(body) == ["OMN-14766", "OMN-13395"]


def test_identity_check_passes_when_branch_matches() -> None:
    rc = cbi.run_identity_check(
        branch="jonah/omn-14766-x", evidence_text="Evidence-Ticket: OMN-14766\n"
    )
    assert rc == 0


def test_identity_check_fails_with_hint_when_branch_omits_ticket(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cbi.run_identity_check(
        branch="jonah/feature-x", evidence_text="Evidence-Ticket: OMN-14766\n"
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "omn-14766" in err  # the exact required fragment is surfaced


def test_identity_check_noop_without_evidence_ticket() -> None:
    rc = cbi.run_identity_check(branch="jonah/whatever", evidence_text="no trailer\n")
    assert rc == 0
