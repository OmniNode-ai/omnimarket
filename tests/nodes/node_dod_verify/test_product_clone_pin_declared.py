# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

import json
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.model_dod_verify_state import EnumEvidenceCheckStatus
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "pin_state", ["exact", "older", "missing", "unreadable", "version"]
)
def test_product_clone_pin_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_state: str
) -> None:
    clone = tmp_path / "product"
    clone.mkdir()
    _git(clone, "init")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "test@example.invalid")
    _git(clone, "commit", "--allow-empty", "-m", "first")
    older = _git(clone, "rev-parse", "HEAD")
    _git(clone, "commit", "--allow-empty", "-m", "deployed")
    head = _git(clone, "rev-parse", "HEAD")

    pin_file = tmp_path / "pins.json"
    if pin_state == "unreadable":
        pin_file.mkdir()
    elif pin_state != "missing":
        pin_file.write_text(
            json.dumps(
                {
                    "version": 2 if pin_state == "version" else 1,
                    "pins": [
                        {
                            "repo_root": str(clone),
                            "pinned_sha": older if pin_state == "older" else head,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_PRODUCT_CLONE_PIN_FILE", str(pin_file))
    monkeypatch.setenv("DOD_VERIFY_REQUIRE_EXACT_PRODUCT_CLONE_PIN", "1")
    monkeypatch.delenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", raising=False)

    collector = EvidenceCollector()
    original_run_git = collector._run_git

    def no_live_upstream(repo: Path, *args: str) -> tuple[int, str, str]:
        assert args[0] != "fetch"
        assert "@{upstream}" not in args
        return original_run_git(repo, *args)

    monkeypatch.setattr(collector, "_run_git", no_live_upstream)
    result = collector._check_evidence_item(
        {
            "id": "cwd",
            "description": "Run in the deployed checkout",
            "checks": [{"check_type": "command", "cwd": str(clone), "command": "pwd"}],
        },
        "OMN-TEST",
    )
    assert result.message is not None
    if pin_state == "exact":
        assert result.status is EnumEvidenceCheckStatus.VERIFIED
        assert result.product_clones[0].head_sha == head
        assert result.product_clones[0].comparison_pinned
    else:
        assert result.status is EnumEvidenceCheckStatus.SKIPPED
        assert "PRODUCT_CLONE_NOT_FRESH" in result.message
