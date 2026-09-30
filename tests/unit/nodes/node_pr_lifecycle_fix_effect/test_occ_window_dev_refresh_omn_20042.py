# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An already-bound batch window refreshes its evidence from OCC dev (OMN-20042).

A re-acceptance on dev changes a contract the window binds, but replaying a
bound member used to no-op and leave the binding gate red forever. These tests
drive the real emitter against the bare git window harness and prove that a
refresh preserves dev evidence and carried members, rebinds member receipts,
and respects running CI and an armed green window. A failed binding gate lets
the doomed head rebuild once; unrelated dev changes alone do not trigger it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    append_dod_evidence_items,
    compute_contract_sha256,
    render_companion_contract,
    render_downstream_dod_evidence_item,
)
from tests.unit.nodes.node_pr_lifecycle_fix_effect.test_occ_window_batch_companion_omn_16336 import (
    _OCC,
    _REPO,
    _TICKETS,
    _WINDOW,
    _git,
    _rendered_member_ids,
)
from tests.unit.nodes.node_pr_lifecycle_fix_effect.test_occ_window_in_flight_omn_20042 import (
    _InFlightScenario,
    _run,
)

_CONTRACT = "contracts/OMN-11111.yaml"
_RECEIPT = "drift/dod_receipts/OMN-11111/reaccept-ac2/command.yaml"
_GATE = "Acceptance-Criterion Binding Gate (OMN-18236)"


class _DevRefreshScenario(_InFlightScenario):
    """The in-flight harness with a seeded contract and git-backed comparisons."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.compare_error: GitHubApiError | None = None
        self.compare_pages: list[int] = []
        self.dev_advance(
            {
                _CONTRACT: render_companion_contract(
                    ticket_id="OMN-11111",
                    repo=_REPO,
                    pr_number=99,
                    evidence_id="prior-pr-99",
                )
            }
        )

    def dev_advance(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = self.seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "advance OCC dev")
        _git(self.seed, "push", "origin", "dev")

    def reaccept(self) -> None:
        contract = append_dod_evidence_items(
            (self.seed / _CONTRACT).read_text(encoding="utf-8"),
            [
                render_downstream_dod_evidence_item(
                    evidence_id="reaccept-ac2", repo=_REPO, pr_number=99
                )
            ],
        )
        self.dev_advance(
            {
                _CONTRACT: contract,
                _RECEIPT: (
                    "evidence_item_id: reaccept-ac2\n"
                    f"contract_sha256: sha256:{compute_contract_sha256(contract)}\n"
                    "status: PASS\n"
                ),
            }
        )

    @staticmethod
    def _page(path: str, files: list[str]) -> list[dict[str, str]]:
        query = parse_qs(urlsplit(path).query)
        per_page = int(query.get("per_page", ["100"])[0])
        page = int(query.get("page", ["1"])[0])
        start = (page - 1) * per_page
        return [{"filename": p} for p in files[start : start + per_page]]

    def fake_rest(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        prefix = f"/repos/{_OCC}/compare/"
        if path.startswith(prefix):
            if self.compare_error is not None:
                raise self.compare_error
            comparison = urlsplit(path).path.removeprefix(prefix)
            base, head = comparison.split("...", maxsplit=1)
            merge_base = _git(self.origin, "merge-base", base, head)
            names = _git(
                self.origin, "diff", "--name-only", merge_base, head
            ).splitlines()
            self.compare_pages.append(int(parse_qs(urlsplit(path).query)["page"][0]))
            return {
                "ahead_by": int(
                    _git(self.origin, "rev-list", "--count", f"{base}..{head}")
                ),
                "files": self._page(path, names),
            }
        return super().fake_rest(method, path, body=body, token=token)

    def fake_array(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        token: str | None = None,
    ) -> list[dict[str, object]]:
        prefix = f"/repos/{_OCC}/pulls/"
        if path.startswith(prefix) and "/files" in path:
            number = int(path.removeprefix(prefix).split("/", maxsplit=1)[0])
            branch = self.occ_prs[number]["head"]["ref"]
            names = _git(
                self.origin,
                "diff",
                "--name-only",
                f"refs/heads/dev...refs/heads/{branch}",
            ).splitlines()
            return self._page(path, names)
        return super().fake_array(method, path, body=body, token=token)


@pytest.mark.unit
def test_refresh_preserves_dev_evidence_and_rebinds_every_member(
    tmp_path: Path,
) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)
        before = scenario.branch_head(_WINDOW)
        bodies_before = dict(scenario.bodies)
        scenario.reaccept()
        scenario.check_runs = [_run("completed", "success")]
        action = scenario.emit(emitter, 101)

    assert not action.startswith(("skip:", "no-op:")), action
    assert scenario.branch_head(_WINDOW) != before
    _git(scenario.origin, "merge-base", "--is-ancestor", "dev", _WINDOW)
    assert len(scenario.pull_posts) == 1
    assert scenario.bodies == bodies_before
    assert scenario.show_bytes(_WINDOW, _RECEIPT) == scenario.show_bytes(
        "dev", _RECEIPT
    )
    for member in (101, 102):
        contract = scenario.show_bytes(_WINDOW, f"contracts/{_TICKETS[member]}.yaml")
        ids = {item["id"] for item in yaml.safe_load(contract)["dod_evidence"]}
        assert _rendered_member_ids(member) <= ids
        if member == 101:
            assert "reaccept-ac2" in ids
        receipts = scenario.member_receipts(_WINDOW, member)
        assert receipts
        for text in receipts.values():
            assert yaml.safe_load(text)["contract_sha256"] == (
                f"sha256:{compute_contract_sha256(contract)}"
            )


@pytest.mark.unit
def test_unrelated_dev_change_does_not_refresh(tmp_path: Path) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.dev_advance({"docs/x.md": "unrelated\n"})
        scenario.check_runs = [_run("completed", "success")]
        action = scenario.emit(emitter, 101)

    assert action.startswith("no-op:"), action
    assert scenario.branch_head(_WINDOW) == before


@pytest.mark.unit
@pytest.mark.parametrize("armed", [False, True], ids=["running", "armed-green"])
def test_refresh_waits_for_a_run_that_can_still_succeed(
    tmp_path: Path, armed: bool
) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.reaccept()
        scenario.check_runs = [
            _run("completed", "success") if armed else _run("in_progress")
        ]
        scenario.auto_merge = {"merge_method": "squash"} if armed else None
        leases_before = mocks["acquire_window"].call_count
        action = scenario.emit(emitter, 101)

    assert action.startswith("skip:WINDOW_IN_FLIGHT — "), action
    assert "OMN-20042" in action
    assert "dev is 1 commit(s) ahead" in action
    assert _CONTRACT in action
    assert "first replay after that run settles" in action
    assert ("armed and green" if armed else "still running") in action
    assert scenario.branch_head(_WINDOW) == before
    assert mocks["acquire_window"].call_count == leases_before


@pytest.mark.unit
@pytest.mark.parametrize("bound_change", [True, False], ids=["bound", "unrelated"])
def test_failed_binding_gate_refreshes_once_even_with_running_ci(
    tmp_path: Path, bound_change: bool
) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        if bound_change:
            scenario.reaccept()
        else:
            scenario.dev_advance({"docs/x.md": "unrelated\n"})
        scenario.check_runs = [_run("in_progress"), _run("completed", "failure", _GATE)]
        # Without doomed_ok the shipped debounce still holds this head.
        assert "still running" in (
            emitter._window_in_flight_reason(branch=_WINDOW, token="token") or ""
        )
        action = scenario.emit(emitter, 101)
        refreshed = scenario.branch_head(_WINDOW)
        second_action = scenario.emit(emitter, 101)

    assert not action.startswith(("skip:", "no-op:")), action
    assert refreshed != before
    _git(scenario.origin, "merge-base", "--is-ancestor", "dev", _WINDOW)
    assert second_action.startswith("no-op:"), second_action
    assert scenario.branch_head(_WINDOW) == refreshed
    assert len(scenario.pull_posts) == 1


@pytest.mark.unit
def test_compare_api_error_fails_closed(tmp_path: Path) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.reaccept()
        scenario.compare_error = GitHubApiError("compare unavailable", status_code=502)
        action = scenario.emit(emitter, 101)

    assert action.startswith("no-op:"), action
    assert scenario.branch_head(_WINDOW) == before


@pytest.mark.unit
def test_compare_file_cap_conservatively_refreshes(tmp_path: Path) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.dev_advance({f"docs/{n:03}.md": "unrelated\n" for n in range(300)})
        action = scenario.emit(emitter, 101)

    assert not action.startswith(("skip:", "no-op:")), action
    assert scenario.branch_head(_WINDOW) != before
    assert scenario.compare_pages == [1, 2, 3]


@pytest.mark.unit
def test_receipt_change_on_later_compare_page_refreshes(tmp_path: Path) -> None:
    scenario = _DevRefreshScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.dev_advance(
            {
                **{f"docs/{n:03}.md": "unrelated\n" for n in range(100)},
                _RECEIPT: "dev receipt\n",
            }
        )
        action = scenario.emit(emitter, 101)

    assert not action.startswith(("skip:", "no-op:")), action
    assert scenario.branch_head(_WINDOW) != before
    assert scenario.compare_pages == [1, 2]
    assert scenario.show_bytes(_WINDOW, _RECEIPT) == scenario.show_bytes(
        "dev", _RECEIPT
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "status", "conclusion", "expected"),
    [
        (_GATE, "completed", "failure", _GATE),
        ("AC-BINDING gate", "completed", "timed_out", "AC-BINDING gate"),
        ("ac_binding", "completed", "action_required", "ac_binding"),
        (_GATE, "in_progress", "failure", None),
        (_GATE, "completed", "success", None),
        ("tests", "completed", "failure", None),
    ],
)
def test_binding_gate_failure_markers(
    name: str, status: str, conclusion: str, expected: str | None
) -> None:
    assert (
        OccCompanionEmitter._binding_gate_failed([_run(status, conclusion, name)])
        == expected
    )
