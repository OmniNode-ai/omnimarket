# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pre-placement admission reads live host state before a lane is placed (OMN-26214).

Each criterion's test names carry the criterion's key: ``allowed_models_per_host``,
``parity_dry_run_pin_fallback``, ``reconcile_lock_probe``, ``same_errno_refusal`` and ``errno30_replay``.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_placement_compute.handlers import (
    HandlerLabAdmission,
    HandlerLabReconcileLockProbe,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    ModelAdmissionHost,
    ModelAdmissionRequest,
    ModelLockProbeRequest,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "errno30_1120"
NOW = datetime(2026, 11, 20, 14, 2, tzinfo=UTC)


def host(name: str, **cells: Any) -> ModelAdmissionHost:
    return ModelAdmissionHost.model_validate(
        {"name": name, "parity": "ok", "lock_probe_read": True, **cells}
    )


def admit(
    hosts: Sequence[ModelAdmissionHost],
    *,
    lane: str = "fill-1555",
    rows: Sequence[str] = (),
    **fields: Any,
) -> Any:
    request = ModelAdmissionRequest(
        lane=lane, hosts=tuple(hosts), ledger_rows=tuple(rows), now=NOW, **fields
    )
    return HandlerLabAdmission().handle(request)


def friction(ts: str, lane: str, host_name: str, text: str = "Errno 30") -> str:
    return f"{ts} | FRICTION | lane={lane} | ticket=OMN-1 | host={host_name} | {text} on the registry"


# ---- AC1: allowed models per host ------------------------------------------------------------------------


def test_allowed_models_per_host_default_model_is_sonnet() -> None:
    result = admit([host("h1"), host("h2")])
    assert result.admitted
    assert result.model == "sonnet"
    assert result.eligible_hosts == ("h1", "h2")


@pytest.mark.parametrize(
    ("brief_ok", "ruling_ok"), [(False, False), (True, False), (False, True)]
)
def test_allowed_models_per_host_codex_refused_without_both(
    brief_ok: bool, ruling_ok: bool
) -> None:
    result = admit(
        [host("h1", codex=True)],
        model="codex",
        brief_codex_ok=brief_ok,
        ruling_allows_codex=ruling_ok,
    )
    assert not result.admitted
    assert result.model is None
    refusal = result.refusals[0]
    assert (refusal.check, refusal.subject) == ("allowed_models", "lane")
    assert "codexOk" in refusal.reason


def test_allowed_models_per_host_codex_only_on_hosts_whose_row_allows_it() -> None:
    result = admit(
        [host("h1", codex=True), host("h2", codex=False)],
        model="codex",
        brief_codex_ok=True,
        ruling_allows_codex=True,
        ruling_ref="RULING 2026-11-16T19:43:11Z",
    )
    assert result.admitted
    assert result.model == "codex"
    assert result.eligible_hosts == ("h1",)
    by_host = {v.host: v for v in result.hosts}
    assert "codex: false" in by_host["h2"].reasons[0]


def test_allowed_models_per_host_codex_with_no_codex_host_places_nothing() -> None:
    result = admit(
        [host("h2", codex=False)],
        model="codex",
        brief_codex_ok=True,
        ruling_allows_codex=True,
    )
    assert not result.admitted
    assert result.eligible_hosts == ()


def test_allowed_models_per_host_fill_tick_emits_sonnet_by_default() -> None:
    """The fill planner (the fill tick's dispatch) plans sonnet unless the brief carries codex_ok AND a RULING allows it."""
    from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
        HandlerLabFillDispatchPlan,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.models import (
        ModelLabFillDispatchPlanRequest,
    )

    def plan(*, codex_ok: bool, ruling: bool) -> list[str]:
        request = ModelLabFillDispatchPlanRequest.model_validate(
            {
                "candidates": [
                    {
                        "kind": "ticket",
                        "ticket": "OMN-51",
                        "repo": "omnimarket",
                        "title": "t",
                        "milestone": "M4",
                        "project_id": "proj-1",
                        "under_parent": True,
                        "self_contained_code": True,
                        "codex_ok": codex_ok,
                    }
                ],
                "capacity": {
                    "hosts": [
                        {
                            "name": "h1",
                            "lanes": 2,
                            "idleLanes": 2,
                            "runnerSlots": 2,
                            "capBound": False,
                            "codex": True,
                            "claude": True,
                            "running": 0,
                            "loadPerCore": 0.1,
                            "reason": "",
                        }
                    ],
                    "free": 2,
                    "budget": 2,
                },
                "config": _fill_config(),
                "codex_ruling_allows": ruling,
            }
        )
        return [
            item.engine
            for item in HandlerLabFillDispatchPlan().handle(request).dispatch
        ]

    assert plan(codex_ok=False, ruling=False) == ["sonnet"]
    assert plan(codex_ok=True, ruling=False) == ["sonnet"]
    assert plan(codex_ok=False, ruling=True) == ["sonnet"]
    assert plan(codex_ok=True, ruling=True) == ["codex"]


def _fill_config() -> dict[str, Any]:
    import json

    cases = json.loads(
        (
            Path(__file__).parents[1]
            / "node_lab_fill_plan_compute"
            / "fixtures"
            / "parity_cases.json"
        ).read_text()
    )["cases"]
    case = next(c for c in cases if c["name"] == "dispatch/parent/engines/codex-host")
    config: dict[str, Any] = case["input"]["config"]
    return config


def test_allowed_models_per_host_lane_model_policy_default_is_not_codex() -> None:
    from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
        resolve_lane_model,
    )

    assert resolve_lane_model(lane_kind="cause-red").model == "sonnet"
    assert (
        resolve_lane_model(lane_kind="cause-red", required_model="codex").model is None
    )
    admitted = resolve_lane_model(
        lane_kind="cause-red", codex_ok=True, codex_ruling_allows=True
    )
    assert admitted.model == "codex"


# ---- AC2: parity dry-run, pin fallback ------------------------------------------------------------------


def test_parity_dry_run_pin_fallback_drops_a_failing_pin() -> None:
    result = admit(
        [host("h051", parity="modified"), host("h052")], pin="h051", lane="fix-1"
    )
    assert result.admitted
    assert result.pin is None
    assert result.pin_dropped is not None
    assert "h051 dropped" in result.pin_dropped
    assert result.eligible_hosts == ("h052",)


def test_parity_dry_run_pin_fallback_keeps_a_passing_pin() -> None:
    result = admit([host("h051"), host("h052")], pin="h051")
    assert result.pin == "h051"
    assert result.pin_dropped is None


def test_parity_dry_run_pin_fallback_unread_dry_run_drops_the_pin() -> None:
    result = admit([host("h051", parity="unread"), host("h052")], pin="h051")
    assert result.pin is None
    assert "was not read" in (result.pin_dropped or "")


def test_parity_dry_run_pin_fallback_never_pins_a_lane_that_repairs_the_host() -> None:
    result = admit(
        [host("h051"), host("h052")],
        pin="h051",
        repairs_parity_of=("h051",),
        lane="repair-h051-venv",
    )
    assert result.pin is None
    assert result.eligible_hosts == ("h052",)
    assert any(
        r.check == "parity_dry_run" and r.subject == "h051" for r in result.refusals
    )


def test_parity_dry_run_pin_fallback_a_required_pin_is_refused_not_dropped() -> None:
    result = admit(
        [host("h051", parity="modified"), host("h052")],
        pin="h051",
        pin_required=True,
    )
    assert not result.admitted
    assert result.pin_dropped is None


# ---- AC3: reconcile lock probe ---------------------------------------------------------------------------


class _Statvfs:
    def __init__(self, flag: int) -> None:
        self.f_flag = flag


def test_reconcile_lock_probe_reads_a_read_only_mount_as_errno_30() -> None:
    probe = HandlerLabReconcileLockProbe(
        statvfs=lambda _p: _Statvfs(os.ST_RDONLY),  # type: ignore[arg-type,return-value]
        access=lambda _p, _m: True,
    )
    reading = probe.handle(ModelLockProbeRequest(lock_dir="/sandbox/registry"))
    assert reading.errno == errno.EROFS == 30


def test_reconcile_lock_probe_never_creates_or_locks_the_lock_file(
    tmp_path: Path,
) -> None:
    reading = HandlerLabReconcileLockProbe().handle(
        ModelLockProbeRequest(lock_dir=str(tmp_path))
    )
    assert reading.errno is None
    assert list(tmp_path.iterdir()) == []


def test_reconcile_lock_probe_reads_a_missing_directory(tmp_path: Path) -> None:
    reading = HandlerLabReconcileLockProbe().handle(
        ModelLockProbeRequest(lock_dir=str(tmp_path / "absent"))
    )
    assert reading.errno == errno.ENOENT


def test_reconcile_lock_probe_unwritable_directory_reads_eacces(tmp_path: Path) -> None:
    probe = HandlerLabReconcileLockProbe(access=lambda _p, _m: False)
    reading = probe.handle(ModelLockProbeRequest(lock_dir=str(tmp_path)))
    assert reading.errno == errno.EACCES


def test_reconcile_lock_probe_refuses_the_host_on_errno_30() -> None:
    result = admit(
        [host("h053", lock_probe_errno=30), host("h054", lock_probe_errno=None)]
    )
    assert result.admitted
    assert result.eligible_hosts == ("h054",)
    refusal = next(r for r in result.refusals if r.check == "reconcile_lock_probe")
    assert refusal.subject == "h053"
    assert "Errno 30" in refusal.reason


def test_reconcile_lock_probe_that_did_not_run_does_not_refuse() -> None:
    result = admit([host("h053", lock_probe_errno=30, lock_probe_read=False)])
    assert result.eligible_hosts == ("h053",)


# ---- AC4: same errno twice -------------------------------------------------------------------------------


def test_same_errno_refusal_third_proof_lane_of_a_class_is_refused() -> None:
    """The replay: proof0-7-1120 and proof0-7-1255 both hit Errno 30; the third placement is refused."""
    rows = [
        friction("2026-11-20T12:30:00Z", "proof0-7-1120", "h101"),
        friction("2026-11-20T12:58:00Z", "proof0-7-1255", "h102"),
    ]
    third = admit([host("h103")], rows=rows, lane="proof0-7-1405")
    assert not third.admitted
    refusal = next(r for r in third.refusals if r.check == "same_errno")
    assert refusal.subject == "proof0-7"
    assert len(refusal.evidence) == 2


def test_same_errno_refusal_second_placement_is_admitted() -> None:
    rows = [friction("2026-11-20T12:30:00Z", "proof0-7-1120", "h101")]
    assert admit([host("h103")], rows=rows, lane="proof0-7-1255").admitted


def test_same_errno_refusal_host_with_two_rows_takes_no_lane() -> None:
    rows = [
        friction("2026-11-20T13:00:00Z", "a-1", "h053"),
        friction("2026-11-20T13:30:00Z", "b-1", "h053"),
    ]
    result = admit([host("h053"), host("h054")], rows=rows, lane="c-1")
    assert result.admitted
    assert result.eligible_hosts == ("h054",)


def test_same_errno_refusal_rows_outside_two_hours_or_other_errnos_do_not_count() -> (
    None
):
    rows = [
        friction("2026-11-20T11:00:00Z", "a-1", "h053"),  # 3h02m before now
        friction("2026-11-20T13:30:00Z", "b-1", "h053"),
        friction("2026-11-20T13:40:00Z", "c-1", "h053", text="Errno 13"),
    ]
    assert admit([host("h053")], rows=rows, lane="d-1").eligible_hosts == ("h053",)


def test_same_errno_refusal_a_newer_merge_row_clears_it() -> None:
    rows = [
        friction("2026-11-20T13:00:00Z", "proof0-7-1120", "h053"),
        friction("2026-11-20T13:30:00Z", "proof0-7-1255", "h053"),
    ]
    merge = "2026-11-20T13:45:00Z | STATUS | lane=landing-controller | merged=OmniNode-ai/omnimarket#6498 | host=h053 | clears=proof0-7 | merged"
    assert not admit([host("h053")], rows=rows, lane="proof0-7-1400").admitted
    cleared = admit([host("h053")], rows=[*rows, merge], lane="proof0-7-1400")
    assert cleared.admitted
    older = "2026-11-20T13:10:00Z | MERGE | host=h053 | before the newest row"
    assert not admit([host("h053")], rows=[*rows, older], lane="proof0-7-1400").admitted


# ---- AC5: the recorded rows -----------------------------------------------------------------------------


def test_errno30_replay_third_placement_on_the_recorded_rows_is_refused() -> None:
    """Replay of the three recorded Errno 30 FRICTION rows (fixture: errno30_1120/friction_rows.ledger).

    unblk394 (13:37Z) and unblk552 (13:51Z) blocked on h053; the placement of unblk695 at 14:01Z, the third,
    is the one the admission refuses on h053, and the lane lands on the host with no such history."""
    rows = (FIXTURES / "friction_rows.ledger").read_text().splitlines()
    assert len(rows) == 3
    before_third = rows[:2]
    request_time = datetime(2026, 11, 20, 14, 1, tzinfo=UTC)
    request = ModelAdmissionRequest(
        lane="unblk695-1315",
        hosts=(host("h053", lock_probe_errno=30), host("h054")),
        pin="h053",
        ledger_rows=tuple(before_third),
        now=request_time,
    )
    result = HandlerLabAdmission().handle(request)
    assert result.pin is None
    assert result.eligible_hosts == ("h054",)
    checks = {r.check for r in result.refusals if r.subject == "h053"}
    assert checks == {"reconcile_lock_probe", "same_errno"}
    # the same rows with only one of them in the window refuse no host on the errno count
    one = ModelAdmissionRequest(
        lane="unblk552-1315",
        hosts=(host("h053"),),
        ledger_rows=tuple(rows[:1]),
        now=datetime(2026, 11, 20, 13, 50, tzinfo=UTC),
    )
    assert HandlerLabAdmission().handle(one).eligible_hosts == ("h053",)
