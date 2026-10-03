# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Missing GitHub credentials leave declared checks PENDING, never FAIL."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
    ModelReceiptSupersession,
)

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

runner: Any = importlib.import_module("occ_receipt_runner")

TICKET = "OMN-17427"
PR_NUMBER = 3249
ITEM = f"dod-occ-diff-derived-behavior-proof-pr-{PR_NUMBER}"
HEAD_SHA = "a" * 40
BRANCH = "jonah/omn-17427-occ-receipt-gh-token"
REPO = "OmniNode-ai/omnimarket"
RUN_URL = "https://github.com/OmniNode-ai/omnimarket/actions/runs/1"
CHECK = "gh api repos/x/y/commits/abc --jq .sha && true"
KEY = f"{TICKET}:{ITEM}:test_passes"
GH_MESSAGE = (
    "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN "
    "environment variable. Example:\n"
    "  env:\n"
    "    GH_TOKEN: ${{ github.token }}\n"
)


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\nif [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ]; then\n'
        "  cat >&2 <<'MESSAGE'\n"
        f"{GH_MESSAGE}"
        "MESSAGE\n"
        "  exit 4\n"
        "fi\n"
        f"printf '%s\\n' '{HEAD_SHA}'\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=":")


def _occ_root(tmp_path: Path, check_value: str, *, born_pending: bool = True) -> Path:
    root = tmp_path / "occ"
    (root / "contracts").mkdir(parents=True)
    (root / "contracts" / f"{TICKET}.yaml").write_text(
        yaml.safe_dump(
            {
                "ticket_id": TICKET,
                "title": "credential-aware declared checks",
                "dod_evidence": [
                    {
                        "id": ITEM,
                        "description": "the diff-derived behaviour proof",
                        "source": "generated",
                        "checks": [
                            {"check_type": "test_passes", "check_value": check_value}
                        ],
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if born_pending:
        base = root / "drift" / "dod_receipts" / TICKET / ITEM / "test_passes.yaml"
        base.parent.mkdir(parents=True)
        base.write_text(
            yaml.safe_dump({"status": "PENDING", "check_value": check_value}),
            encoding="utf-8",
        )
    return root


def _execute(occ_root: Path, product_root: Path) -> Any:
    return runner.run(
        occ_root=occ_root,
        product_root=product_root,
        ticket_ids=(TICKET,),
        pr_number=PR_NUMBER,
        repo=REPO,
        head_sha=HEAD_SHA,
        branch=BRANCH,
        run_url=RUN_URL,
    )


def _receipt(outcome: Any) -> dict[str, Any]:
    assert len(outcome.wrote) == 1
    data = yaml.safe_load(outcome.wrote[0].read_text(encoding="utf-8"))
    if "replacement" in data:
        record = ModelReceiptSupersession.model_validate(data)
        assert record.tombstone is False
        assert record.replacement is not None
        assert record.replacement.contract_entry_sha256 is not None
        assert record.supersedes.endswith(f"{ITEM}/test_passes.yaml")
        return dict(data["replacement"])
    ModelDodReceipt.model_validate(data)
    return dict(data)


@pytest.mark.unit
def test_every_workflow_runner_step_has_the_job_token() -> None:
    text = (_ROOT / ".github" / "workflows" / "occ-receipt-runner.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(text)
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "occ_receipt_runner.py" in step.get("run", "")
    ]
    assert len(steps) >= 2
    for step in steps:
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert not re.search(r"GH_TOKEN:.*(secrets\.|occ-app-token)", text)


@pytest.mark.unit
@pytest.mark.parametrize("born_pending", [True, False])
def test_missing_credential_is_pending_and_retried(
    tmp_path: Path,
    fake_gh: None,
    monkeypatch: pytest.MonkeyPatch,
    born_pending: bool,
) -> None:
    occ_root = _occ_root(tmp_path, CHECK, born_pending=born_pending)
    outcome = _execute(occ_root, tmp_path)

    assert outcome.failures == ()
    assert outcome.not_run == (KEY,)
    receipt = _receipt(outcome)
    assert receipt["status"] == "PENDING"
    assert receipt["exit_code"] is None
    assert receipt["actual_output"] == (
        "PENDING: NOT RUN (environment error): the runner's environment held no "
        "GitHub credential (GH_TOKEN), so the declared check did not execute in "
        f"the {REPO} checkout at PR #{PR_NUMBER} head. This is not a result for "
        f"the behaviour. Run: {RUN_URL}"
    )
    assert receipt["probe_stdout"] == GH_MESSAGE.strip()
    before = outcome.wrote[0].read_bytes()

    monkeypatch.setenv("GH_TOKEN", "dummy")
    retry = _execute(occ_root, tmp_path)
    assert retry.executed == 1
    assert retry.not_run == ()
    assert _receipt(retry)["status"] == "PASS"
    assert outcome.wrote[0].read_bytes() == before


@pytest.mark.unit
def test_job_token_allows_the_declared_check_to_pass(
    tmp_path: Path, fake_gh: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "dummy")
    outcome = _execute(_occ_root(tmp_path, CHECK), tmp_path)

    assert outcome.failures == ()
    assert outcome.not_run == ()
    receipt = _receipt(outcome)
    assert receipt["status"] == "PASS"
    assert receipt["exit_code"] == 0
    assert receipt["probe_stdout"] == HEAD_SHA


@pytest.mark.unit
@pytest.mark.parametrize(
    ("check_value", "exit_code"),
    [('echo "set the GH_TOKEN environment variable"; exit 1', 1), ("exit 4", 4)],
)
def test_message_or_exit_code_alone_remains_a_failure(
    tmp_path: Path, check_value: str, exit_code: int
) -> None:
    outcome = _execute(_occ_root(tmp_path, check_value), tmp_path)

    assert outcome.failures == (KEY,)
    assert outcome.not_run == ()
    receipt = _receipt(outcome)
    assert receipt["status"] == "FAIL"
    assert receipt["exit_code"] == exit_code


@pytest.mark.unit
@pytest.mark.parametrize("message", [GH_MESSAGE, "Run gh auth login to authenticate"])
def test_authentication_required_messages_are_not_run(message: str) -> None:
    assert runner.is_missing_credential(4, message)
    assert not runner.is_missing_credential(1, message)


@pytest.mark.unit
def test_main_reports_not_run_in_json_and_exits_zero(
    tmp_path: Path, fake_gh: None, capsys: pytest.CaptureFixture[str]
) -> None:
    occ_root = _occ_root(tmp_path, CHECK)
    summary_path = tmp_path / "summary.json"
    exit_code = runner.main(
        [
            "--occ-root",
            str(occ_root),
            "--product-root",
            str(tmp_path),
            "--pr-number",
            str(PR_NUMBER),
            "--repo",
            REPO,
            "--head-sha",
            HEAD_SHA,
            "--branch",
            BRANCH,
            "--run-url",
            RUN_URL,
            "--tickets",
            TICKET,
            "--json-out",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["not_run"] == [KEY]
    assert summary["failures"] == []
    captured = capsys.readouterr()
    assert json.loads(captured.out) == summary
    assert captured.err.strip() == (
        "::warning::declared check not run, GitHub credential missing in the "
        f"runner environment: {KEY}"
    )
