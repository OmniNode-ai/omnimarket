# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared cause ownership counts only the PRs judged by repo-status (OMN-19998)."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_cause_claim_ownership_effect import handler
from omnimarket.nodes.node_cause_claim_ownership_effect.models import (
    ModelCauseClaimOwnershipRequest,
)

pytestmark = pytest.mark.unit


def test_landing_open_records_excludes_drafts_held_and_merged_prs() -> None:
    kept = {"facts": {"state": "OPEN", "draft": False}, "cls": "red"}
    watcher_state = {
        "prs": {
            "omnibase_infra#1": kept,
            "omnibase_infra#2": {
                "facts": {"state": "OPEN", "draft": True},
                "cls": "held-excluded",
            },
            "omnibase_infra#3": {
                "facts": {"state": "OPEN", "draft": False},
                "cls": "held-excluded",
            },
            "omnibase_infra#4": {"facts": {"state": "MERGED"}},
            "omnibase_infra#5": "not a record",
        }
    }

    assert handler.landing_open_records(watcher_state) == {"omnibase_infra#1": kept}


def test_handle_passes_only_landing_open_records_to_claim_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[str] = []

    def fake_claim_owners_of_check(
        qs: Any,
        local: Any,
        registry_root: Path,
        now: datetime,
        repo: str,
        check: str,
        ledger: Path | None,
    ) -> SimpleNamespace:
        recorded.extend(sorted(local.open_records()))
        return SimpleNamespace(owners=[], prs=[], refresh={}, inherits=[])

    monkeypatch.setattr(handler, "load_queue_status", lambda _path: SimpleNamespace())
    monkeypatch.setattr(handler, "claim_owners_of_check", fake_claim_owners_of_check)
    monkeypatch.setattr(handler, "emit_near_misses", lambda *_args: [])
    request = ModelCauseClaimOwnershipRequest(
        result={
            "lines": [
                "MISSING repo-status:omnibase_infra | shared_red_unowned: Gate on 3 | FIX: x"
            ],
            "missing": ["repo-status:omnibase_infra"],
            "repo_status": [
                {
                    "repo": "omnibase_infra",
                    "verdict": "shared_red_unowned",
                    "check": "Gate",
                }
            ],
        },
        watcher_state={
            "prs": {
                "omnibase_infra#1": {
                    "facts": {"state": "OPEN", "draft": False},
                    "cls": "red",
                    "ci": {"red": ["Gate"]},
                },
                "omnibase_infra#2": {
                    "facts": {"state": "OPEN", "draft": False},
                    "cls": "red",
                    "ci": {"red": ["Gate"]},
                },
                "omnibase_infra#3": {
                    "facts": {"state": "OPEN", "draft": True},
                    "cls": "held-excluded",
                    "ci": {"red": ["Gate"]},
                },
            }
        },
        now=datetime(2026, 10, 31, tzinfo=UTC),
        ledger=None,
        registry_root=str(tmp_path),
        queue_status=tmp_path / "qs.py",
        report_type=object,
    )

    handler.HandlerCauseClaimOwnership().handle(request)

    assert recorded == ["omnibase_infra#1", "omnibase_infra#2"]
