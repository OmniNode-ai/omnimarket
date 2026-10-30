# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WIP admission: at the limit new work is refused, a fix lane is admitted, a ticket is always created.

Every repository name and ticket id below is an invented fixture.
"""

from __future__ import annotations

import ast
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import rules_wip_admission as rules
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.wip_admission import (
    EnumWipFixClass,
    EnumWipOutcome,
    EnumWipReason,
    EnumWipWorkKind,
    ModelWipAdmissionConfig,
    ModelWipAdmissionRequest,
    ModelWipClaim,
    ModelWipFix,
    ModelWipLimit,
    ModelWipPolicy,
    ModelWipRead,
)
from omnimarket.nodes.node_wip_admission_compute.handlers import cli
from omnimarket.nodes.node_wip_admission_compute.handlers.handler_wip_admission import (
    HandlerWipAdmission,
)
from omnimarket.nodes.node_wip_admission_compute.overlay import (
    ModelWipAdmissionOverlay,
    load_wip_admission_config,
    load_wip_admission_policy,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
REPO_A, REPO_B = "fixture-repo-a", "fixture-repo-b"
CONFIG = load_wip_admission_config()


def limit(rate: float, hours: float = 3.0) -> dict[str, Any]:
    return {
        "limit": rules.derive_limit(rate, hours, CONFIG),
        "merges_per_hour": rate,
        "lead_time_hours": hours,
        "method": "fixture measurement",
        "evidence": "fixture evidence",
    }


def overlay_doc() -> dict[str, Any]:
    return {
        "enforce_ruling": "fixture ruling stamp",
        "dispatchable_repos": [REPO_A, REPO_B],
        "fleet": limit(2.0),
        "limits": {REPO_A: limit(1.0), REPO_B: limit(1.0)},
        "fix_forward_classes": ["red", "repeat_break", "landing_blocker"],
        "fix_forward_kinds": ["fixture-red-kind"],
    }


def policy(doc: dict[str, Any] | None = None) -> ModelWipPolicy:
    return ModelWipAdmissionOverlay.model_validate(doc or overlay_doc()).policy(CONFIG)


def pr(
    repo: str, n: int, *, title: str = "", draft: bool = False, cls: str = "pending"
) -> dict[str, Any]:
    return {
        "cls": cls,
        "facts": {
            "repo": repo,
            "number": n,
            "state": "OPEN",
            "draft": draft,
            "title": title,
            "head_ref": "",
        },
    }


def watcher(*records: dict[str, Any], at: datetime = NOW) -> dict[str, Any]:
    return {
        "last_tick": at.isoformat(),
        "prs": {f"{r['facts']['repo']}#{r['facts']['number']}": r for r in records},
    }


def read_of(
    *records: dict[str, Any],
    claims: tuple[ModelWipClaim, ...] = (),
    states: dict[str, str] | None = None,
    at: datetime = NOW,
) -> ModelWipRead:
    return rules.build_wip_read(
        pr_state=watcher(*records, at=at),
        claims=claims,
        ticket_states=states or {},
        config=CONFIG,
    )


def ask(
    kind: EnumWipWorkKind,
    read: ModelWipRead,
    *,
    repo: str = REPO_A,
    ticket: str = "",
    fix: ModelWipFix | None = None,
    pol: ModelWipPolicy | None = None,
    now: datetime = NOW,
) -> Any:
    return HandlerWipAdmission().handle(
        ModelWipAdmissionRequest(
            kind=kind,
            repo=repo,
            ticket=ticket,
            now=now,
            read=read,
            policy=pol or policy(),
            config=CONFIG,
            fix=fix,
        )
    )


def at_repo_limit() -> ModelWipRead:
    n = policy().limits[REPO_A].limit
    return read_of(*(pr(REPO_A, i) for i in range(1, n + 1)))


def test_under_the_limit_new_work_is_admitted() -> None:
    d = ask(EnumWipWorkKind.NEW_WORK, read_of(pr(REPO_A, 1)))
    assert (d.outcome, d.reason) == (EnumWipOutcome.ADMIT, EnumWipReason.UNDER_LIMIT)
    assert d.repo_count == 1


def test_at_the_repository_limit_new_work_is_refused() -> None:
    d = ask(EnumWipWorkKind.NEW_WORK, at_repo_limit())
    assert (d.outcome, d.reason) == (EnumWipOutcome.REFUSE, EnumWipReason.AT_REPO_LIMIT)
    assert d.line.startswith(f"REFUSE wip:{REPO_A} ")


def test_another_repository_under_its_limit_is_still_admitted_until_the_fleet_fills() -> (
    None
):
    d = ask(EnumWipWorkKind.NEW_WORK, at_repo_limit(), repo=REPO_B)
    assert d.outcome is EnumWipOutcome.ADMIT


def test_at_the_fleet_limit_new_work_is_refused_in_any_repository() -> None:
    fleet = policy().fleet.limit
    spread = [pr(REPO_A if i % 2 else REPO_B, i) for i in range(1, fleet + 1)]
    for repo in (REPO_A, REPO_B):
        d = ask(EnumWipWorkKind.NEW_WORK, read_of(*spread), repo=repo)
        assert d.outcome is EnumWipOutcome.REFUSE
        assert d.reason in (EnumWipReason.AT_REPO_LIMIT, EnumWipReason.AT_FLEET_LIMIT)
    assert (
        ask(EnumWipWorkKind.NEW_WORK, read_of(*spread), repo=REPO_A).fleet_count
        == fleet
    )


def test_a_fix_for_a_red_pull_request_is_admitted_at_the_limit() -> None:
    records = [pr(REPO_A, i) for i in range(1, 9)] + [pr(REPO_A, 99, cls="red")]
    fix = ModelWipFix(fix_class=EnumWipFixClass.RED, target=f"{REPO_A}#99")
    d = ask(EnumWipWorkKind.FIX_FORWARD, read_of(*records), fix=fix)
    assert (d.outcome, d.reason) == (EnumWipOutcome.ADMIT, EnumWipReason.FIX_FORWARD)


def test_a_fix_for_a_landing_blocker_is_admitted_at_the_limit() -> None:
    fix = ModelWipFix(fix_class=EnumWipFixClass.LANDING_BLOCKER, target=f"{REPO_A}#1")
    d = ask(EnumWipWorkKind.FIX_FORWARD, at_repo_limit(), fix=fix)
    assert d.reason is EnumWipReason.FIX_FORWARD


def test_a_repeat_break_fix_needs_the_configured_repeat_count() -> None:
    twice = ModelWipFix(
        fix_class=EnumWipFixClass.REPEAT_BREAK, repeat_count=CONFIG.repeat_break_min
    )
    once = ModelWipFix(
        fix_class=EnumWipFixClass.REPEAT_BREAK, repeat_count=CONFIG.repeat_break_min - 1
    )
    assert (
        ask(EnumWipWorkKind.FIX_FORWARD, at_repo_limit(), fix=twice).outcome
        is EnumWipOutcome.ADMIT
    )
    assert (
        ask(EnumWipWorkKind.FIX_FORWARD, at_repo_limit(), fix=once).outcome
        is EnumWipOutcome.REFUSE
    )


def test_a_fix_claim_the_read_cannot_confirm_is_new_work() -> None:
    not_red = ModelWipFix(fix_class=EnumWipFixClass.RED, target=f"{REPO_A}#1")
    absent = ModelWipFix(fix_class=EnumWipFixClass.RED, target=f"{REPO_A}#404")
    for fix in (not_red, absent):
        d = ask(EnumWipWorkKind.FIX_FORWARD, at_repo_limit(), fix=fix)
        assert d.outcome is EnumWipOutcome.REFUSE
    assert (
        ask(EnumWipWorkKind.FIX_FORWARD, at_repo_limit()).outcome
        is EnumWipOutcome.REFUSE
    )


def test_a_fix_class_the_overlay_does_not_list_is_new_work() -> None:
    doc = overlay_doc()
    doc["fix_forward_classes"] = ["red"]
    fix = ModelWipFix(fix_class=EnumWipFixClass.LANDING_BLOCKER, target=f"{REPO_A}#1")
    d = ask(EnumWipWorkKind.FIX_FORWARD, at_repo_limit(), fix=fix, pol=policy(doc))
    assert d.outcome is EnumWipOutcome.REFUSE


def test_a_ticket_is_always_created_whatever_the_count() -> None:
    fleet = policy().fleet.limit
    full = read_of(*(pr(REPO_A, i) for i in range(1, fleet + 5)))
    for read in (full, ModelWipRead(readable=False, unreadable_reason="x")):
        d = ask(EnumWipWorkKind.TICKET_CREATE, read, repo="")
        assert (d.outcome, d.reason) == (
            EnumWipOutcome.ADMIT,
            EnumWipReason.TICKET_RECORD,
        )


def test_an_unreadable_read_refuses_new_work_and_admits_a_fix_and_a_ticket() -> None:
    down = ModelWipRead(readable=False, unreadable_reason="state absent")
    assert ask(EnumWipWorkKind.NEW_WORK, down).reason is EnumWipReason.READ_MISSING
    fix = ModelWipFix(fix_class=EnumWipFixClass.RED, target=f"{REPO_A}#1")
    d = ask(EnumWipWorkKind.FIX_FORWARD, down, fix=fix)
    assert (d.outcome, d.reason) == (
        EnumWipOutcome.ADMIT,
        EnumWipReason.FIX_FORWARD_READ_UNUSABLE,
    )


def test_a_stale_read_refuses_new_work_by_the_configured_age() -> None:
    old = NOW - timedelta(minutes=CONFIG.reading_max_age_minutes + 1)
    fresh = NOW - timedelta(minutes=CONFIG.reading_max_age_minutes - 1)
    assert (
        ask(EnumWipWorkKind.NEW_WORK, read_of(at=old)).reason
        is EnumWipReason.READ_STALE
    )
    assert (
        ask(EnumWipWorkKind.NEW_WORK, read_of(at=fresh)).outcome is EnumWipOutcome.ADMIT
    )


def test_without_an_enforcing_ruling_nothing_is_refused() -> None:
    doc = overlay_doc()
    doc["enforce_ruling"] = ""
    d = ask(EnumWipWorkKind.NEW_WORK, at_repo_limit(), pol=policy(doc))
    assert (d.outcome, d.reason) == (EnumWipOutcome.ADMIT, EnumWipReason.NOT_ENFORCING)


def test_a_repository_with_no_limit_refuses_naming_the_reason() -> None:
    assert (
        ask(EnumWipWorkKind.NEW_WORK, read_of(), repo="fixture-unknown").reason
        is EnumWipReason.LIMIT_MISSING
    )


def test_a_draft_pull_request_is_not_counted() -> None:
    counts = rules.count_started_unlanded(
        read_of(pr(REPO_A, 1, draft=True), pr(REPO_A, 2))
    )
    assert counts.pull_requests == 1
    assert counts.by_repo == {REPO_A: 1}


def test_a_held_pull_request_is_not_counted() -> None:
    assert (
        rules.count_started_unlanded(read_of(pr(REPO_A, 1, cls="held-excluded"))).fleet
        == 0
    )


CLAIM = ModelWipClaim(lane="fixture-lane", ticket="OMN-1", repo=REPO_A)


def test_a_started_ticket_with_a_live_claim_counts_once_beside_a_pull_request() -> None:
    states = {"OMN-1": "In Progress"}
    alone = rules.count_started_unlanded(read_of(claims=(CLAIM,), states=states))
    assert (alone.ticket_claims, alone.fleet) == (1, 1)
    carried = rules.count_started_unlanded(
        read_of(pr(REPO_A, 7, title="feat(OMN-1): x"), claims=(CLAIM,), states=states)
    )
    assert (carried.pull_requests, carried.ticket_claims, carried.fleet) == (1, 0, 1)


def test_a_ticket_whose_only_pull_request_is_a_draft_still_counts() -> None:
    counts = rules.count_started_unlanded(
        read_of(
            pr(REPO_A, 7, title="OMN-1", draft=True),
            claims=(CLAIM,),
            states={"OMN-1": "In Review"},
        )
    )
    assert counts.fleet == 1
    assert counts.ticket_claims == 1


def test_a_ticket_not_in_a_started_state_is_not_counted() -> None:
    for state in ("Backlog", "Done", None):
        states = {} if state is None else {"OMN-1": state}
        assert (
            rules.count_started_unlanded(read_of(claims=(CLAIM,), states=states)).fleet
            == 0
        )


def test_the_callers_own_ticket_never_blocks_itself() -> None:
    n = policy().limits[REPO_A].limit
    claims = tuple(
        ModelWipClaim(lane=f"l{i}", ticket=f"OMN-{i}", repo=REPO_A)
        for i in range(1, n + 1)
    )
    states = {c.ticket: "In Progress" for c in claims}
    read = read_of(claims=claims, states=states)
    assert ask(EnumWipWorkKind.NEW_WORK, read).outcome is EnumWipOutcome.REFUSE
    assert (
        ask(EnumWipWorkKind.NEW_WORK, read, ticket="OMN-1").outcome
        is EnumWipOutcome.ADMIT
    )


def test_a_malformed_watcher_state_is_an_unreadable_read_never_zero() -> None:
    for bad in (None, {}, {"last_tick": NOW.isoformat(), "prs": []}, {"prs": {}}):
        read = rules.build_wip_read(
            pr_state=bad, claims=(), ticket_states={}, config=CONFIG
        )
        assert not read.readable


def test_the_limit_is_littles_law_from_its_own_measurement(tmp_path: Path) -> None:
    assert rules.derive_limit(2.0, 3.0, CONFIG) == 6
    assert rules.derive_limit(0.1, 3.0, CONFIG) == CONFIG.limit_floor
    doc = overlay_doc()
    doc["limits"][REPO_A]["limit"] = 99
    with pytest.raises(NodeOverlayError, match=rf"limits\.{REPO_A}\.limit"):
        policy(doc)
    del doc["limits"][REPO_B]
    with pytest.raises(NodeOverlayError, match=rf"wip_admission\.limits\.{REPO_B}"):
        policy(doc)


def test_the_overlay_loads_from_the_overlay_root(tmp_path: Path) -> None:
    folder = tmp_path / "wip_admission"
    folder.mkdir()
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump(overlay_doc()), encoding="utf-8"
    )
    loaded = load_wip_admission_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert loaded == policy()
    with pytest.raises(NodeOverlayError):
        load_wip_admission_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")})


def test_no_limit_number_is_in_the_code() -> None:
    root = Path(rules.__file__).parents[1]
    sources = [
        Path(rules.__file__),
        root / "models/wip_admission.py",
        root / "nodes/node_wip_admission_compute/overlay.py",
        root / "nodes/node_wip_admission_compute/handlers/handler_wip_admission.py",
        root / "nodes/node_wip_admission_compute/handlers/cli.py",
    ]
    for path in sources:
        numbers = [
            n.value
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, int | float)
            and not isinstance(n.value, bool)
            and n.value not in (0, 1, 2, 3)  # exit codes and zero
        ]
        assert numbers == [], f"{path.name}: {numbers}"


def test_no_repository_or_ticket_budget_name_is_in_the_package() -> None:
    node = Path(rules.__file__).parents[1] / "nodes/node_wip_admission_compute"
    text = " ".join(p.read_text(encoding="utf-8") for p in node.rglob("*.py")).lower()
    assert "daily" not in text
    assert "per day" not in text


def run_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict[str, Any]
) -> tuple[int, str]:
    folder = tmp_path / "wip_admission"
    folder.mkdir(exist_ok=True)
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump(overlay_doc()), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    return cli.main(), out.getvalue()


def test_the_command_line_admits_refuses_and_reports_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = policy().limits[REPO_A].limit
    full = watcher(*(pr(REPO_A, i) for i in range(1, n + 1)))
    code, out = run_cli(
        monkeypatch,
        tmp_path,
        {"kind": "new_work", "repo": REPO_A, "now": NOW.isoformat(), "pr_state": full},
    )
    assert code == cli.EXIT_REFUSE
    assert json.loads(out)["reason"] == "at_repo_limit"
    code, out = run_cli(
        monkeypatch,
        tmp_path,
        {"kind": "ticket_create", "now": NOW.isoformat(), "pr_state": full},
    )
    assert code == cli.EXIT_ADMIT
    assert json.loads(out)["reason"] == "ticket_record"
    code, _ = run_cli(monkeypatch, tmp_path, {"kind": "bogus", "now": NOW.isoformat()})
    assert code == cli.EXIT_UNREADABLE


def test_the_frozen_models_reject_extra_keys() -> None:
    with pytest.raises(ValueError, match="extra"):
        ModelWipLimit.model_validate({**limit(1.0), "extra": 1})
    assert isinstance(CONFIG, ModelWipAdmissionConfig)
