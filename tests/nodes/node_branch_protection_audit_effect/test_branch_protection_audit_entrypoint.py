# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node entry point keeps the script's exit status and report, and fails loud.

0 when every audited repository is compliant, 1 on any deviation, 2 when the
audit cannot run as configured: a required overlay that is absent, an invalid
overlay, an unreadable snapshot, or an empty fleet. The JSONL stream carries
the records the script wrote to BRANCH_PROTECTION_AUDIT_JSONL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_protection_audit_effect.__main__ import main
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    OVERLAY_SCHEMA_VERSION,
)
from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    PROTECTION_CI_SUMMARY,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    snapshot,
)

pytestmark = pytest.mark.unit

REPO = "repo_a"
_VERIFY_ONLY: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": ["verify / verify"],
        "checks": [{"context": "verify / verify", "app_id": 15368}],
    },
    "enforce_admins": {"enabled": True},
}


def _snapshot_file(tmp_path: Path, dev: dict[str, object]) -> Path:
    base = f"repos/{ORG}/{REPO}"
    fx: dict[str, object] = {
        "graphql": GQL_RULES,
        f"{base}/branches/dev/protection": dev,
        base: REPO_SETTINGS,
        f"{base}/rulesets": [RULESET_MERGE_QUEUE_DISABLED],
        f"{base}/rulesets/13269695": RULESET_MERGE_QUEUE_DISABLED,
    }
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot(REPO, fx).model_dump_json(), encoding="utf-8")
    return path


def _overlay(tmp_path: Path) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\nrepos:\n  - {REPO}\n',
        encoding="utf-8",
    )
    return path


def test_compliant_snapshot_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--overlay",
            str(_overlay(tmp_path)),
            "--overlay-required",
            "--branches",
            "dev",
            "--snapshot",
            str(_snapshot_file(tmp_path, PROTECTION_CI_SUMMARY)),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "All repos compliant." in out


def test_missing_required_context_exits_one_and_writes_the_jsonl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jsonl = tmp_path / "audit.jsonl"
    code = main(
        [
            "--repos",
            REPO,
            "--branches",
            "dev",
            "--snapshot",
            str(_snapshot_file(tmp_path, _VERIFY_ONLY)),
            "--jsonl",
            str(jsonl),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1, out
    assert " Summary: 1 failure(s) across 7 checks" in out
    records = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert {
        "repo": REPO,
        "branch": "dev",
        "check": "required_check_ci_summary",
        "status": "FAIL",
        "detail": "CI Summary missing",
    } in records
    assert set(records[0]) == {"repo", "branch", "check", "status", "detail"}


def test_required_overlay_absent_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--overlay-required",
            "--snapshot",
            str(_snapshot_file(tmp_path, PROTECTION_CI_SUMMARY)),
        ]
    )
    assert code == 2
    assert "overlay" in capsys.readouterr().err


def test_empty_fleet_exits_two_rather_than_reading_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--snapshot", str(_snapshot_file(tmp_path, PROTECTION_CI_SUMMARY))])
    assert code == 2
    assert "no repositories to audit" in capsys.readouterr().err


def test_unreadable_snapshot_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "snapshot.json"
    bad.write_text("{not json", encoding="utf-8")
    code = main(["--repos", REPO, "--snapshot", str(bad)])
    assert code == 2
    assert "snapshot" in capsys.readouterr().err
