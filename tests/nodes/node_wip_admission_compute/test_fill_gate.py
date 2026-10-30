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
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import ModelIdleSlotFillRequest
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV
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
    return watcher(*(pr(REPO_A, i) for i in range(1, 12)))


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


def test_new_work_in_a_full_repository_waits_and_a_fix_kind_does_not(
    tmp_path: Path,
) -> None:
    refusal, finding = gate(tmp_path)
    assert finding is None
    why = refusal({"kind": "ticket", "repo": REPO_A, "key": "k"})
    assert why is not None
    assert why.startswith(f"wip-limit: REFUSE wip:{REPO_A} ")
    assert refusal({"kind": FIX_KIND, "repo": REPO_A, "key": "k"}) is None


def test_new_work_in_an_empty_fleet_goes_on(tmp_path: Path) -> None:
    refusal, finding = gate(tmp_path, state=watcher())
    assert finding is None
    assert refusal({"kind": "ticket", "repo": REPO_A, "key": "k"}) is None


def test_live_claims_count_beside_the_pull_requests(tmp_path: Path) -> None:
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


HOST = {
    "name": "host-a",
    "local": False,
    "admission": {"claude_slots": 2, "codex_slots": 0, "probe_error": None},
}
LINES = [
    "MISSING lab-headroom:host-a | free Claude slots 2 | FIX: dispatch",
    "MISSING repo-status:fixture-repo | shared_red_unowned: check pytest red | FIX: dispatch",
]


def request(**extra: Any) -> ModelIdleSlotFillRequest:
    return ModelIdleSlotFillRequest(
        now="2026-10-30T12:00:00Z",
        hosts=[HOST],
        lines=LINES,
        standing_tickets={"cause-red": "OMN-9101"},
        **extra,
    )


def test_the_fill_skips_what_the_gate_refuses_and_sends_what_it_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    held = handler.HandlerIdleSlotFill().handle(
        request(wip_refusal=lambda _item: "wip-limit: REFUSE")
    )
    assert held.sends == []
    assert {
        "key": "repo-status:fixture-repo",
        "why": "wip-limit: REFUSE",
    } in held.skipped
    sent = handler.HandlerIdleSlotFill().handle(request(wip_refusal=lambda _item: None))
    assert [s["key"] for s in sent.sends] == ["repo-status:fixture-repo"]
