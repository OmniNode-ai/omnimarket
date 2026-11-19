# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill's seam: a refused candidate waits, a fix-forward kind never does, an unread gate says why.

Every repository name and ticket id below is an invented fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.wip_admission_fill_gate import fill_wip_gate
from tests.nodes.node_wip_admission_compute.test_wip_admission import (
    NOW,
    REPO_A,
    overlay_doc,
    pr,
    watcher,
)

pytestmark = pytest.mark.unit

FIX_KIND = "fixture-red-kind"


def env_with_overlay(tmp_path: Path) -> dict[str, str]:
    folder = tmp_path / "wip_admission"
    folder.mkdir(exist_ok=True)
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump(overlay_doc()), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def full_watcher() -> dict[str, Any]:
    """94 open pull requests: they are not work sent to lanes and refuse nothing."""
    return watcher(*(pr(REPO_A, i) for i in range(1, 95)))


def claims_over_the_bound() -> tuple[set[str], dict[str, str]]:
    return {f"OMN-{i}" for i in range(1, 12)}, {}


def no_claims() -> tuple[set[str], dict[str, str]]:
    return set(), {}


def gate(
    tmp_path: Path,
    *,
    claims: Any = no_claims,
    state: Any = None,
    env: dict[str, str] | None = None,
) -> Any:
    return fill_wip_gate(
        ledger_tickets=claims,
        watcher_state=full_watcher() if state is None else state,
        now=NOW,
        environ=env or env_with_overlay(tmp_path),
    )


def test_94_open_pull_requests_do_not_hold_new_work(tmp_path: Path) -> None:
    refusal, finding = gate(tmp_path)
    assert finding is None
    assert refusal({"kind": "ticket", "repo": REPO_A, "key": "k"}) is None


def test_new_work_in_a_full_repository_waits_and_a_fix_kind_does_not(
    tmp_path: Path,
) -> None:
    refusal, finding = gate(tmp_path, claims=claims_over_the_bound)
    assert finding is None
    why = refusal({"kind": "ticket", "repo": REPO_A, "key": "k"})
    assert why is not None
    assert why.startswith(f"wip-limit: REFUSE wip:{REPO_A} ")
    assert refusal({"kind": FIX_KIND, "repo": REPO_A, "key": "k"}) is None


def test_new_work_in_an_empty_fleet_goes_on(tmp_path: Path) -> None:
    refusal, finding = gate(tmp_path, state=watcher())
    assert finding is None
    assert refusal({"kind": "ticket", "repo": REPO_A, "key": "k"}) is None


def test_live_claims_are_the_count_whatever_the_pull_requests(tmp_path: Path) -> None:
    claims = {f"OMN-{i}" for i in range(1, 12)}
    refusal, _ = gate(tmp_path, claims=lambda: (claims, {}), state=watcher())
    assert refusal({"kind": "ticket", "repo": "", "key": "k"}) is not None


def test_an_unreadable_overlay_judges_nothing_and_says_why(tmp_path: Path) -> None:
    refusal, finding = gate(tmp_path, env={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert refusal({"kind": "ticket", "repo": REPO_A, "key": "k"}) is None
    assert finding is not None
    assert finding[:2] == ("unknown", "lab-fill-wip")


def test_unreadable_claims_refuse_new_work_only(tmp_path: Path) -> None:
    def boom() -> tuple[set[str], dict[str, str]]:
        raise OSError("ledger gone")

    refusal, finding = gate(tmp_path, claims=boom, state=watcher())
    assert refusal({"kind": "ticket", "repo": REPO_A, "key": "k"}) is not None
    assert refusal({"kind": FIX_KIND, "repo": REPO_A, "key": "k"}) is None
    assert finding is not None
    assert "ledger gone" in finding[2]
