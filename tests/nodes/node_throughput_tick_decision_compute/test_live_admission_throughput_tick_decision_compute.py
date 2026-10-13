# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20864: the tick reports the live admission headroom per engine, never the cached cap minus lanes.

2026-10-13: the tick printed free slots for a host that placement then refused (h202 read 3 free while
refusing on swap traffic, and lanes sat DETACHED without a host). The caller now reads the runner's
live pool admission once and passes the admitted slots per engine; a refused host is a NOTE with the
rule that refused it, and the Claude and Codex lanes draw from one shared slot budget.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-13T14:00:00Z"
HEAD = "a" * 40


def admission(slots: int = 3, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "placed": 1,
        "lanes": 1,
        "cap": 4,
        "slots": slots,
        "claude_slots": slots,
        "codex_slots": slots,
    }
    return {**base, **overrides}


def refused(reason: str, **overrides: Any) -> dict[str, Any]:
    """Both engines refused by one shared rule: every slot is lost."""
    return admission(
        0,
        claude_slots=0,
        codex_slots=0,
        claude_refusal=reason,
        codex_refusal=reason,
        **overrides,
    )


def host(
    name: str = "h201", adm: dict[str, Any] | None = None, **cells: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "cap": 4,
        "admission": adm if adm is not None else admission(),
        **cells,
    }


def run(*hosts: dict[str, Any], **request: Any) -> Any:
    return HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": NOW,
                "launchctl": "not_loaded",
                "lab_headroom": {"live_admission": True, "hosts": list(hosts)},
                **request,
            }
        )
    )


def lab_missing(result: Any) -> list[str]:
    return [name for name in result.missing if name.startswith("lab-headroom")]


def lab_lines(result: Any) -> list[str]:
    return [line for line in result.lines if "lab-headroom" in line]


def test_free_live_slots_name_both_engines_and_the_one_shared_budget() -> None:
    [line] = lab_lines(run(host()))
    assert line.startswith(
        "MISSING lab-headroom:h201 | running lanes 1, placed units 1 of cap 4, "
        "free Claude slots 3, free Codex slots 3 (shared slots 3) | FIX: "
    )
    fix = line.split(" | FIX: ")[1]
    assert "dispatch up to 3 Claude lanes or up to 3 Codex lanes" in fix
    assert "sharing 3 slots" in fix


def test_shared_slots_are_not_counted_twice_for_the_pr_land_fallback() -> None:
    # Negative control: three shared slots are three lanes, not Claude 3 + Codex 3 = six.
    parked = [
        {"pr": f"repo_a#{n}", "head_sha": HEAD, "landing_state": "PARKED"}
        for n in range(1, 7)
    ]
    result = run(
        host(),
        pr_land={
            "prs": parked,
            "holds": {"source": "ledger", "read": True, "lines": []},
        },
    )
    assert result.pr_land is not None
    assert result.pr_land.idle_slots == 3
    assert len(result.pr_land.dispatch) == 3
    assert "repo_a#3 (parked)" in lab_lines(result)[0]
    assert "repo_a#4" not in lab_lines(result)[0]


@pytest.mark.parametrize("idle", [0.9, 7.3])
def test_a_cpu_refused_host_reports_zero_slots_whatever_the_cached_cap_says(
    idle: float,
) -> None:
    reason = f"CPU-IDLE-UNDER-BAR: {idle}% idle < 25%"
    result = run(host(adm=refused(reason)))
    [line] = lab_lines(result)
    assert line.startswith("NOTE lab-headroom:h201 | ")
    assert "free Claude slots 0, free Codex slots 0 (shared slots 0)" in line
    assert reason in line
    assert "FIX:" not in line
    assert lab_missing(result) == []


@pytest.mark.parametrize(
    "reason", ["swap traffic 41 pages/s", "LOAD-OVER-BAR", "mem_avail 1.0G < 10G"]
)
def test_any_shared_refusal_empties_both_engines(reason: str) -> None:
    [line] = lab_lines(run(host(adm=refused(reason))))
    assert line.startswith("NOTE")
    assert reason in line
    assert "free Claude slots 0, free Codex slots 0" in line


def test_a_claude_limit_refuses_only_claude_and_the_fix_names_codex_alone() -> None:
    adm = admission(
        claude_slots=0, claude_refusal="CLAUDE-LIMITED until 2026-10-13T20:00:00Z"
    )
    [line] = lab_lines(run(host(adm=adm)))
    assert line.startswith("MISSING lab-headroom:h201 |")
    assert "free Claude slots 0, free Codex slots 3 (shared slots 3)" in line
    assert "Claude refused: CLAUDE-LIMITED until 2026-10-13T20:00:00Z" in line
    fix = line.split(" | FIX: ")[1]
    assert "dispatch up to 3 Codex lanes" in fix
    assert "Claude lanes" not in fix
    [codex] = run(host(adm=adm)).codex_headroom
    assert (codex.host, codex.free, codex.cap, codex.running) == ("h201", 3, 4, 1)
    assert codex.claude_blocked == "CLAUDE-LIMITED until 2026-10-13T20:00:00Z"


def test_a_codex_refusal_leaves_claude_slots_and_the_fix_names_claude_alone() -> None:
    adm = admission(codex_slots=0, codex_refusal="NO-CODEX-LOGIN")
    [line] = lab_lines(run(host(adm=adm)))
    assert "free Claude slots 3, free Codex slots 0" in line
    assert "Codex refused: NO-CODEX-LOGIN" in line
    fix = line.split(" | FIX: ")[1]
    assert "dispatch up to 3 Claude lanes" in fix
    assert "Codex lanes" not in fix
    [codex] = run(host(adm=adm)).codex_headroom
    assert codex.free == 0
    assert codex.refusal == "NO-CODEX-LOGIN"


def test_a_full_host_is_a_note_with_no_refusal_to_name() -> None:
    [line] = lab_lines(
        run(host(adm=admission(0, placed=4, lanes=4, claude_slots=0, codex_slots=0)))
    )
    assert line.startswith("NOTE lab-headroom:h201 | ")
    assert "(shared slots 0)" in line
    assert line.endswith("; no dispatch")


def test_an_unreadable_probe_is_unknown_never_free_never_full() -> None:
    adm = admission(0, claude_slots=0, codex_slots=0, probe_error="probe unavailable")
    result = run(host(adm=adm))
    [line] = lab_lines(result)
    assert line.startswith(
        "UNKNOWN lab-headroom:h201 | live admission probe unreadable (probe unavailable)"
    )
    assert "FIX:" in line
    assert lab_missing(result) == []
    assert [u for u in result.unknown if u.startswith("lab-headroom")] == [
        "lab-headroom:h201"
    ]


def test_a_host_with_no_admission_reading_is_unknown() -> None:
    result = run({"name": "h201"})
    assert lab_lines(result)[0].startswith(
        "UNKNOWN lab-headroom:h201 | live admission probe unreadable"
    )


def test_the_local_host_is_judged_like_a_lab_host() -> None:
    [line] = lab_lines(run(host("local", local=True)))
    assert line.startswith("MISSING lab-headroom:local |")
    [noted] = lab_lines(
        run(
            host(
                "local", adm=refused("CPU-IDLE-UNDER-BAR: 7.3% idle < 25%"), local=True
            )
        )
    )
    assert noted.startswith("NOTE lab-headroom:local |")


def test_each_host_has_one_finding_in_host_order() -> None:
    result = run(
        host("h201"), host("h202", adm=refused("swap traffic 41 pages/s")), host("h203")
    )
    assert [line.split(" |")[0] for line in lab_lines(result)] == [
        "MISSING lab-headroom:h201",
        "NOTE lab-headroom:h202",
        "MISSING lab-headroom:h203",
    ]


def test_live_admission_ignores_stale_receipts() -> None:
    result = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": NOW,
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "live_admission": True,
                    "hosts": [host(adm=refused("CPU-IDLE-UNDER-BAR: 0.9% idle < 25%"))],
                    "receipts": [
                        {
                            "started_at": "2026-10-13T13:59:00Z",
                            "host": "h201",
                            "status": "running",
                            "pid_alive": True,
                            "claim_host": "h201",
                            "claim_run_id": "rlane-fix-a-1",
                            "readings": [
                                "h201:cores=32,load1=4.0,free_gb=64.0,placed=1,cap=4,slots=3,login=claude"
                            ],
                        }
                    ],
                },
            }
        )
    )
    assert lab_missing(result) == []
    assert lab_lines(result)[0].startswith("NOTE lab-headroom:h201 |")


def test_the_unavailable_host_note_and_placement_error_still_report_with_live_admission() -> (
    None
):
    result = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": NOW,
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "live_admission": True,
                    "unavailable_hosts": ["h202"],
                    "hosts": [host()],
                },
            }
        )
    )
    assert (
        "NOTE lab-headroom:h202 | unavailable in host table; no dispatch"
        in result.lines
    )
    errored = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": NOW,
                "launchctl": "not_loaded",
                "lab_headroom": {"live_admission": True, "placement_error": "no pool"},
            }
        )
    )
    assert "NOTE lab-headroom: cannot read placement state (no pool)" in errored.lines
