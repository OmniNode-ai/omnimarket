# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repository with open PRs and zero merges in the window gets a status check (OMN-19998).

Operator RULING 2026-10-15T00:42:51Z lane=orchestrator-bb1a: a repository with outstanding PRs that
merged nothing gets its status checked every tick. Given `repo_status` facts, the node checks each such
repository over the watcher state, the controller's state and tick lines it already holds and returns one
typed verdict per repository; a verdict event is due only when no unit is open for the repository or its
verdict changed. An unowned shared red or an ejecting merge queue is a MISSING line whose FIX names one
cause lane or the controller; every other verdict is a NOTE.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import omnimarket.nodes.node_throughput_tick_decision_compute as node_package
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
    ModelThroughputTickResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NODE_DIR = Path(node_package.__file__).parent
NOW = "2026-10-15T01:00:00Z"


def _pr(
    repo: str,
    n: int,
    *,
    cls: str = "green-unarmed",
    red: list[str] | None = None,
    pending: list[str] | None = None,
    started: str = "2026-10-15T00:40:00Z",
    draft: bool = False,
    queued: bool = False,
    reason: str = "every check-run green",
) -> tuple[str, dict[str, Any]]:
    pending = pending or []
    return f"{repo}#{n}", {
        "cls": cls,
        "queued": queued,
        "reason": reason,
        "ci": {
            "red": red or [],
            "pending": pending,
            "detail": [[name, "1", started] for name in pending],
        },
        "facts": {
            "repo": repo,
            "number": n,
            "state": "OPEN",
            "draft": draft,
            "author": "op",
            "base": "main",
        },
    }


def _merged(repo: str, n: int, at: str) -> tuple[str, dict[str, Any]]:
    return f"{repo}#{n}", {
        "facts": {"repo": repo, "number": n, "state": "MERGED", "merged_at": at}
    }


def _watcher(
    *prs: tuple[str, dict[str, Any]],
    base_rows: dict[str, list[list[str]]] | None = None,
    base_runs: dict[str, list[list[str]]] | None = None,
) -> dict[str, Any]:
    repos = {key.split("#")[0] for key, _ in prs}
    return {
        "last_tick": "2026-10-15T00:58:00Z",
        "operator": "op",
        "repos": {repo: {"default_branch": "main"} for repo in repos},
        "branches": {
            f"{repo}@main": {
                "checks": {"rows": rows},
                **(
                    {"runs": {"rows": base_runs[repo]}}
                    if base_runs is not None and repo in base_runs
                    else {}
                ),
            }
            for repo, rows in (base_rows or {}).items()
        },
        "prs": dict(prs),
    }


def _request(watcher: dict[str, Any], **overrides: Any) -> ModelThroughputTickRequest:
    payload: dict[str, Any] = {
        "now": NOW,
        "launchctl": "not_loaded",
        "watcher_path": "/w.json",
        "watcher_state": watcher,
        "repo_status": {"window_minutes": 60, "open_units": []},
        **overrides,
    }
    return ModelThroughputTickRequest.model_validate(payload)


def _by_repo(result: ModelThroughputTickResult) -> dict[str, Any]:
    return {v.repo: v for v in result.repo_status}


def _line(result: ModelThroughputTickResult, repo: str) -> str:
    return next(line for line in result.lines if f"repo-status:{repo} " in line)


def test_a_repo_with_open_prs_and_zero_merges_gets_one_verdict_and_a_merging_repo_none() -> (
    None
):
    watcher = _watcher(
        _pr("alpha", 1),
        _pr("alpha", 2),
        _pr("beta", 3),
        _merged("beta", 4, "2026-10-15T00:30:00Z"),
        _pr("gamma", 5, draft=True),
    )

    result = HANDLER.handle(_request(watcher))

    verdicts = _by_repo(result)
    assert sorted(verdicts) == ["alpha"]
    assert verdicts["alpha"].open_prs == 2
    assert verdicts["alpha"].merges_in_window == 0
    assert verdicts["alpha"].event is True
    assert "repo-status" in result.checked


def test_without_repo_status_facts_the_check_is_skipped_and_absent_from_the_dump() -> (
    None
):
    watcher = _watcher(_pr("alpha", 1))

    result = HANDLER.handle(_request(watcher, repo_status=None))

    assert result.repo_status == []
    assert "repo_status" not in result.model_dump()
    assert "repo-status" not in result.checked


def test_a_check_red_on_two_open_prs_with_no_cause_owner_is_a_missing_fix_line() -> (
    None
):
    watcher = _watcher(
        _pr("alpha", 1, cls="red", red=["pytest"]),
        _pr("alpha", 2, cls="red", red=["pytest", "lint"]),
    )

    result = HANDLER.handle(_request(watcher))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "shared_red_unowned"
    assert verdict.check == "pytest"
    assert verdict.prs == ["alpha#1", "alpha#2"]
    line = _line(result, "alpha")
    assert line.startswith("MISSING repo-status:alpha |")
    assert "dispatch ONE cause lane for check pytest in alpha" in line
    assert "repo-status:alpha" in result.missing
    assert result.exit_code == 1


def test_a_roll_up_red_beside_a_real_red_names_the_real_check() -> None:
    watcher = _watcher(
        _pr("alpha", 1, cls="red", red=["CI Summary", "repo-evidence / dod-verify"]),
        _pr("alpha", 2, cls="red", red=["CI Summary", "repo-evidence / dod-verify"]),
    )

    result = HANDLER.handle(_request(watcher))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "shared_red_unowned"
    assert verdict.check == "repo-evidence / dod-verify"
    assert verdict.prs == ["alpha#1", "alpha#2"]
    line = _line(result, "alpha")
    assert (
        "dispatch ONE cause lane for check repo-evidence / dod-verify in alpha" in line
    )
    assert "check CI Summary" not in line


def test_a_roll_up_red_alone_is_still_the_shared_red() -> None:
    watcher = _watcher(
        _pr("alpha", 1, cls="red", red=["CI Summary"]),
        _pr("alpha", 2, cls="red", red=["CI Summary"]),
    )

    result = HANDLER.handle(_request(watcher))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "shared_red_unowned"
    assert verdict.check == "CI Summary"


def test_a_roll_up_shared_only_through_prs_with_other_reds_is_not_shared() -> None:
    watcher = _watcher(
        _pr("alpha", 1, cls="red", red=["CI Summary", "lint"]),
        _pr("alpha", 2, cls="red", red=["CI Summary", "pytest"]),
    )

    result = HANDLER.handle(_request(watcher))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict != "shared_red_unowned"


def test_a_shared_red_a_live_cause_lease_owns_is_not_unowned() -> None:
    watcher = _watcher(
        _pr("alpha", 1, cls="red", red=["pytest"]),
        _pr("alpha", 2, cls="red", red=["pytest"]),
    )
    state = {
        "causes": {
            "cause:o/alpha:ab": {
                "repo": "o/alpha",
                "members": [{"pr": "o/alpha#1"}, {"pr": "o/alpha#2"}],
            }
        },
        "leases": [{"pr": "cause:o/alpha:ab", "lease_id": "L42"}],
    }

    result = HANDLER.handle(_request(watcher, state_json=state))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "parked_or_escalated"
    assert "owned by landing-L42" in verdict.reason
    assert _line(result, "alpha").startswith("NOTE repo-status:alpha |")


def test_a_merge_queue_ejection_for_a_base_red_is_a_missing_line_naming_the_cause() -> (
    None
):
    watcher = _watcher(_pr("alpha", 1), _pr("alpha", 2))
    tick = {
        "tick": 9,
        "ts": "2026-10-15T00:55:00Z",
        "base_blocked": {
            "count": 2,
            "checks": ["validate"],
            "prs": ["alpha#1", "alpha#2"],
            "lifted": [],
        },
    }

    result = HANDLER.handle(_request(watcher, ticks=[tick]))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "merge_queue_ejecting"
    assert verdict.check == "validate"
    assert verdict.prs == ["alpha#1", "alpha#2"]
    line = _line(result, "alpha")
    assert line.startswith("MISSING repo-status:alpha |")
    assert "dispatch ONE cause lane for validate on the alpha base branch" in line


def test_a_red_base_branch_is_a_note_naming_the_newest_failing_check() -> None:
    rows = [
        [
            "validate",
            "completed",
            "failure",
            "2026-10-15T00:50:00Z",
            "2026-10-15T00:55:00Z",
        ],
        [
            "validate",
            "completed",
            "success",
            "2026-10-15T00:10:00Z",
            "2026-10-15T00:20:00Z",
        ],
        [
            "lint",
            "completed",
            "failure",
            "2026-10-15T00:10:00Z",
            "2026-10-15T00:20:00Z",
        ],
        [
            "lint",
            "completed",
            "success",
            "2026-10-15T00:50:00Z",
            "2026-10-15T00:52:00Z",
        ],
    ]
    watcher = _watcher(_pr("alpha", 1), base_rows={"alpha": rows})

    result = HANDLER.handle(_request(watcher))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "base_branch_red"
    assert verdict.check == "validate"
    assert _line(result, "alpha").startswith("NOTE repo-status:alpha |")


def test_a_red_from_another_branchs_run_on_the_base_sha_is_not_a_base_red() -> None:
    rows = [
        [
            "CI Summary",
            "completed",
            "success",
            "2026-10-15T00:20:00Z",
            "2026-10-15T00:21:00Z",
            "1",
            "10",
            "100",
        ],
        [
            "CI Summary",
            "completed",
            "failure",
            "2026-10-15T00:50:00Z",
            "2026-10-15T00:51:00Z",
            "2",
            "20",
            "200",
        ],
        [
            "Tests (Split 1/20)",
            "completed",
            "failure",
            "2026-10-15T00:45:00Z",
            "2026-10-15T00:46:00Z",
            "3",
            "20",
            "200",
        ],
    ]
    runs = [
        [
            "100",
            "10",
            ".github/workflows/ci.yml",
            "CI",
            "1",
            "push",
            "completed",
            "success",
            "main",
            "2026-10-15T00:10:00Z",
        ],
        [
            "200",
            "20",
            ".github/workflows/ci.yml",
            "CI",
            "1",
            "push",
            "completed",
            "failure",
            "release",
            "2026-10-15T00:40:00Z",
        ],
    ]
    watcher = _watcher(
        _pr("alpha", 1), base_rows={"alpha": rows}, base_runs={"alpha": runs}
    )

    result = HANDLER.handle(_request(watcher))

    assert _by_repo(result)["alpha"].verdict != "base_branch_red"


def test_a_red_from_an_unlisted_run_still_counts() -> None:
    rows = [
        [
            "CI Summary",
            "completed",
            "failure",
            "2026-10-15T00:50:00Z",
            "2026-10-15T00:51:00Z",
            "2",
            "30",
            "300",
        ],
        [
            "Tests (Split 1/20)",
            "completed",
            "failure",
            "2026-10-15T00:45:00Z",
            "2026-10-15T00:46:00Z",
            "3",
            "30",
            "300",
        ],
    ]
    runs = [
        [
            "100",
            "10",
            ".github/workflows/ci.yml",
            "CI",
            "1",
            "push",
            "completed",
            "success",
            "main",
            "2026-10-15T00:10:00Z",
        ],
    ]
    watcher = _watcher(
        _pr("alpha", 1), base_rows={"alpha": rows}, base_runs={"alpha": runs}
    )

    result = HANDLER.handle(_request(watcher))

    assert _by_repo(result)["alpha"].verdict == "base_branch_red"


def test_a_base_red_roll_up_beside_a_real_red_names_the_real_check() -> None:
    rows = [
        [
            "CI Summary",
            "completed",
            "failure",
            "2026-10-15T00:50:00Z",
            "2026-10-15T00:51:00Z",
            "1",
            "10",
            "100",
        ],
        [
            "Tests (Split 1/20)",
            "completed",
            "failure",
            "2026-10-15T00:45:00Z",
            "2026-10-15T00:46:00Z",
            "2",
            "10",
            "100",
        ],
    ]
    runs = [
        [
            "100",
            "10",
            ".github/workflows/ci.yml",
            "CI",
            "1",
            "push",
            "completed",
            "failure",
            "main",
            "2026-10-15T00:40:00Z",
        ],
    ]
    watcher = _watcher(
        _pr("alpha", 1), base_rows={"alpha": rows}, base_runs={"alpha": runs}
    )

    result = HANDLER.handle(_request(watcher))

    verdict = _by_repo(result)["alpha"]
    assert verdict.verdict == "base_branch_red"
    assert verdict.check == "Tests (Split 1/20)"


def test_a_base_red_roll_up_alone_is_still_named() -> None:
    rows = [
        [
            "CI Summary",
            "completed",
            "failure",
            "2026-10-15T00:50:00Z",
            "2026-10-15T00:51:00Z",
            "1",
            "10",
            "100",
        ],
    ]
    runs = [
        [
            "100",
            "10",
            ".github/workflows/ci.yml",
            "CI",
            "1",
            "push",
            "completed",
            "failure",
            "main",
            "2026-10-15T00:40:00Z",
        ],
    ]
    watcher = _watcher(
        _pr("alpha", 1), base_rows={"alpha": rows}, base_runs={"alpha": runs}
    )

    result = HANDLER.handle(_request(watcher))

    assert _by_repo(result)["alpha"].check == "CI Summary"


def test_pending_ci_is_waiting_on_ci_with_the_oldest_wait() -> None:
    watcher = _watcher(
        _pr(
            "alpha",
            1,
            cls="pending",
            pending=["pytest"],
            started="2026-10-15T00:30:00Z",
        ),
        _pr(
            "alpha", 2, cls="pending", pending=["lint"], started="2026-10-15T00:45:00Z"
        ),
    )

    verdict = _by_repo(HANDLER.handle(_request(watcher)))["alpha"]

    assert verdict.verdict == "waiting_on_ci"
    assert verdict.queue_wait_minutes == 30.0


def test_a_parked_repo_is_parked_or_escalated() -> None:
    watcher = _watcher(_pr("alpha", 1, cls="held", reason="repo alpha is parked"))

    verdict = _by_repo(HANDLER.handle(_request(watcher)))["alpha"]

    assert verdict.verdict == "parked_or_escalated"


def test_green_prs_in_the_merge_queue_are_merging_normally() -> None:
    watcher = _watcher(_pr("alpha", 1, cls="green-armed", queued=True))

    verdict = _by_repo(HANDLER.handle(_request(watcher)))["alpha"]

    assert verdict.verdict == "merging_normally"


def test_an_open_unit_with_the_same_verdict_dedupes_the_event_and_a_changed_one_does_not() -> (
    None
):
    watcher = _watcher(
        _pr("alpha", 1, cls="green-armed", queued=True),
        _pr("beta", 2, cls="red", red=["x"]),
        _pr("beta", 3, cls="red", red=["x"]),
    )
    units = [
        {
            "repo": "alpha",
            "opened_at": "2026-10-15T00:50:00Z",
            "verdict": "merging_normally",
        },
        {
            "repo": "beta",
            "opened_at": "2026-10-15T00:50:00Z",
            "verdict": "waiting_on_ci",
        },
    ]

    result = HANDLER.handle(_request(watcher, repo_status={"open_units": units}))

    verdicts = _by_repo(result)
    assert verdicts["alpha"].event is False
    assert verdicts["alpha"].opened_at == "2026-10-15T00:50:00Z"
    assert verdicts["beta"].event is True
    assert verdicts["beta"].opened_at == NOW
    # The FIX line repeats every tick while the cause stands, whether or not an event is due.
    assert "repo-status:beta" in result.missing


def test_a_stale_watcher_reads_no_verdict_and_says_so() -> None:
    watcher = _watcher(_pr("alpha", 1))
    watcher["last_tick"] = "2026-10-15T00:00:00Z"

    result = HANDLER.handle(_request(watcher))

    assert result.repo_status == []
    assert any(line.startswith("UNKNOWN repo-status |") for line in result.lines)


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_carries_the_verdicts(tmp_path: Path) -> None:
    watcher = _watcher(
        _pr("alpha", 1, cls="red", red=["pytest"]),
        _pr("alpha", 2, cls="red", red=["pytest"]),
    )
    payload = _request(watcher).model_dump(mode="json", exclude_none=True)
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()

    result = runtime.handler_result
    assert isinstance(result, ModelThroughputTickResult)
    assert [(v.repo, v.verdict, v.check) for v in result.repo_status] == [
        ("alpha", "shared_red_unowned", "pytest")
    ]
    assert (
        ModelThroughputTickResult.model_validate_json(result.model_dump_json())
        == result
    )
