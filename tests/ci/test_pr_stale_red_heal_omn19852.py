# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour of the release-window and vendor-parity heals (OMN-19888).

Named for the parent stall epic OMN-19852 whose diagnosis produced both heals.
Each acceptance criterion of OMN-19888 has its falsifier here, selected by
``-k``:

* AC1 ``release_window`` -- a PR red on Release Identity Gate at a version dev
  has since moved past gets exactly one branch update, pinned to its head.
* AC2 ``release_window_refuses`` -- dev still in the window, a failed version
  not below dev, a conflict, a draft, a non-dev base, and a gate that is not red
  all leave the PR alone; a PR inside the window is not updated twice.
* AC3 ``vendor_parity`` -- a parity red whose migrations are now vendored
  byte-identically, after the gate failed, re-runs the gate and CI Summary once.
* AC4 ``vendor_parity_refuses`` -- not vendored, vendored with different bytes,
  and a failure that postdates the vendor commit are all left alone.
* AC5 ``workflow`` -- the workflow is scheduled, has no pull_request trigger,
  scopes its writes to its one job and is not a required context.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.pr_stale_red_heal import (  # noqa: E402
    CI_SUMMARY_CHECK,
    MAX_HEAL_RUN_ATTEMPT,
    MAX_UPDATES_PER_PASS,
    RELEASE_IDENTITY_CHECK,
    VENDOR_PARITY_CHECK,
    CiSummarySnapshot,
    DevReleaseState,
    EnumReleaseWindowOutcome,
    EnumVendorParityOutcome,
    EnumVendorState,
    GateSnapshot,
    GhCli,
    MigrationVendorState,
    OpenPr,
    ReleaseWindowInput,
    VendorParityInput,
    _build_parser,
    collect_decisions,
    decide_release_window,
    decide_vendor_parity,
    latest_published_version,
    main,
    newest_check_run,
    parse_release_identity_failure,
    pyproject_version,
    vendor_migrations_named,
    vendored_dest_path,
)

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-stale-red-heal.yml"

pytestmark = pytest.mark.unit

HEAD = "e6967d42a752f18ff2ba84b2c3eae7af1989d579"
#: The refusal line of omnimarket#2905's Release Identity Gate job
#: 108620502879 (2026-09-27T12:35:57Z), verbatim.
LIVE_RIG_LOG = (
    "2026-09-27T12:35:57.8589352Z FAIL: packaged source changed but pyproject "
    "version 0.4.248 is NOT ahead of the latest published version 0.4.248 "
    "(OMN-16344 release-identity gate).\n"
)
SRC = "src/omnimarket/nodes/node_projection_session_content/migrations/0001_create_session_content.sql"
DEST = "docker/migrations/forward/nodes/node_projection_session_content/0001_create_session_content.sql"
#: omnimarket#2905's parity annotation (check-run 108620501988), verbatim head.
LIVE_PARITY_ANNOTATION = (
    f"{SRC} has no vendored counterpart at {DEST} in omnibase_infra@dev. Land the "
    "vendor commit in omnibase_infra FIRST (scripts/sync-node-migrations.sh), "
    "merge it, then this PR will pass."
)


# ---------------------------------------------------------------------------
# Builders: each returns the state that SHOULD heal, so a test names one change.
# ---------------------------------------------------------------------------


def _pr(**overrides: Any) -> OpenPr:
    base: dict[str, Any] = {
        "number": 2905,
        "head_sha": HEAD,
        "is_draft": False,
        "base_ref": "dev",
        "merge_state": "BLOCKED",
    }
    base.update(overrides)
    return OpenPr(**base)


def _red_gate(**overrides: Any) -> GateSnapshot:
    base: dict[str, Any] = {
        "check_run_id": 108620502879,
        "status": "completed",
        "conclusion": "failure",
        "completed_at": "2026-09-27T12:36:02Z",
        "run_id": 36319512751,
    }
    base.update(overrides)
    return GateSnapshot(**base)


def _window(**overrides: Any) -> ReleaseWindowInput:
    base: dict[str, Any] = {
        "pr": _pr(),
        "gate": _red_gate(),
        "failed_versions": ((0, 4, 248), (0, 4, 248)),
        "dev": DevReleaseState(dev_version=(0, 4, 253), latest_published=(0, 4, 252)),
    }
    base.update(overrides)
    return ReleaseWindowInput(**base)


def _migration(**overrides: Any) -> MigrationVendorState:
    base: dict[str, Any] = {
        "src_path": SRC,
        "dest_path": DEST,
        "state": EnumVendorState.IDENTICAL,
        "vendored_at": "2026-09-27T16:01:07Z",
    }
    base.update(overrides)
    return MigrationVendorState(**base)


def _parity(**overrides: Any) -> VendorParityInput:
    base: dict[str, Any] = {
        "pr": _pr(),
        "gate": _red_gate(),
        "run_attempt": 1,
        "migrations": (_migration(),),
        "ci_summary": CiSummarySnapshot(
            job_id=108620502996, run_id=36319513074, red=True, other_red_jobs=0
        ),
    }
    base.update(overrides)
    return VendorParityInput(**base)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_release_window_parses_the_live_refusal_line() -> None:
    assert parse_release_identity_failure(LIVE_RIG_LOG) == ((0, 4, 248), (0, 4, 248))


def test_release_window_refuses_to_parse_an_unrelated_log() -> None:
    assert parse_release_identity_failure("ERROR: no pyproject version\n") is None
    assert parse_release_identity_failure("") is None


def test_release_window_reads_dev_version_and_newest_tag() -> None:
    assert pyproject_version('[project]\nname = "x"\nversion = "0.4.253"\n') == (
        0,
        4,
        253,
    )
    assert pyproject_version('[tool.x]\nversion = "9.9.9"\n') is None
    assert pyproject_version("not toml [") is None
    assert latest_published_version(["v0.4.252", "v0.4.99", "v0.4.251", "junk"]) == (
        0,
        4,
        252,
    )
    assert latest_published_version([]) is None


def test_vendor_parity_parses_the_live_annotation() -> None:
    annotations: list[dict[str, object]] = [
        {"message": "Process completed with exit code 1."},
        {"message": LIVE_PARITY_ANNOTATION},
        {"message": f"{SRC} differs from the vendored copy in omnibase_infra@dev"},
    ]
    assert vendor_migrations_named(annotations) == [SRC]
    assert vendored_dest_path(SRC) == DEST


def test_newest_check_run_picks_the_highest_id_and_reads_the_run() -> None:
    runs: list[dict[str, object]] = [
        {
            "id": 10,
            "name": VENDOR_PARITY_CHECK,
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/o/r/actions/runs/7/job/10",
        },
        {
            "id": 12,
            "name": VENDOR_PARITY_CHECK,
            "status": "in_progress",
            "conclusion": None,
            "details_url": "https://github.com/o/r/actions/runs/7/job/12",
        },
    ]
    newest = newest_check_run(runs, VENDOR_PARITY_CHECK)
    assert newest is not None
    assert newest.check_run_id == 12
    assert newest.run_id == 7
    assert not newest.red
    assert newest_check_run(runs, CI_SUMMARY_CHECK) is None


# ---------------------------------------------------------------------------
# AC1 / AC2: release window
# ---------------------------------------------------------------------------


def test_release_window_updates_a_pr_dev_has_moved_past() -> None:
    decision = decide_release_window(_window())
    assert decision.outcome is EnumReleaseWindowOutcome.UPDATE_REQUIRED
    assert decision.act
    assert decision.head_sha == HEAD


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        ({"gate": None}, EnumReleaseWindowOutcome.GATE_NOT_RED),
        (
            {"gate": _red_gate(conclusion="success")},
            EnumReleaseWindowOutcome.GATE_NOT_RED,
        ),
        (
            {"gate": _red_gate(status="in_progress", conclusion="")},
            EnumReleaseWindowOutcome.GATE_NOT_RED,
        ),
        ({"pr": _pr(is_draft=True)}, EnumReleaseWindowOutcome.DRAFT),
        ({"pr": _pr(base_ref="jonah/stack")}, EnumReleaseWindowOutcome.NOT_DEV_BASE),
        ({"pr": _pr(merge_state="DIRTY")}, EnumReleaseWindowOutcome.CONFLICTED),
        ({"failed_versions": None}, EnumReleaseWindowOutcome.FAILURE_UNPARSED),
        ({"dev": None}, EnumReleaseWindowOutcome.DEV_STATE_UNRESOLVED),
        (
            {
                "dev": DevReleaseState(
                    dev_version=(0, 4, 253), latest_published=(0, 4, 253)
                )
            },
            EnumReleaseWindowOutcome.DEV_IN_WINDOW,
        ),
        (
            {"failed_versions": ((0, 4, 253), (0, 4, 253))},
            EnumReleaseWindowOutcome.VERSION_NOT_BELOW_DEV,
        ),
    ],
)
def test_release_window_refuses(
    overrides: dict[str, Any], outcome: EnumReleaseWindowOutcome
) -> None:
    decision = decide_release_window(_window(**overrides))
    assert decision.outcome is outcome
    assert not decision.act


# ---------------------------------------------------------------------------
# AC3 / AC4: vendor parity
# ---------------------------------------------------------------------------


def test_vendor_parity_reruns_the_gate_and_ci_summary() -> None:
    decision = decide_vendor_parity(_parity())
    assert decision.outcome is EnumVendorParityOutcome.RERUN_REQUIRED
    assert decision.run_ids == (36319512751,)
    assert decision.job_ids == (108620502996,)


def test_vendor_parity_leaves_ci_summary_alone_when_its_run_has_other_reds() -> None:
    summary = CiSummarySnapshot(job_id=1, run_id=2, red=True, other_red_jobs=3)
    decision = decide_vendor_parity(_parity(ci_summary=summary))
    assert decision.outcome is EnumVendorParityOutcome.RERUN_REQUIRED
    assert decision.run_ids == (36319512751,)
    assert decision.job_ids == ()


def test_vendor_parity_skips_ci_summary_that_is_not_red() -> None:
    summary = CiSummarySnapshot(job_id=1, run_id=2, red=False)
    assert decide_vendor_parity(_parity(ci_summary=summary)).job_ids == ()
    assert decide_vendor_parity(_parity(ci_summary=None)).job_ids == ()


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        ({"gate": None}, EnumVendorParityOutcome.GATE_NOT_RED),
        (
            {"gate": _red_gate(conclusion="success")},
            EnumVendorParityOutcome.GATE_NOT_RED,
        ),
        ({"pr": _pr(is_draft=True)}, EnumVendorParityOutcome.DRAFT),
        ({"superseded_by_update": True}, EnumVendorParityOutcome.SUPERSEDED_BY_UPDATE),
        ({"migrations": ()}, EnumVendorParityOutcome.NO_MIGRATION_NAMED),
        (
            {
                "migrations": (
                    _migration(state=EnumVendorState.MISSING, vendored_at=""),
                )
            },
            EnumVendorParityOutcome.NOT_VENDORED,
        ),
        (
            {"migrations": (_migration(state=EnumVendorState.DIFFERS),)},
            EnumVendorParityOutcome.VENDORED_COPY_DIFFERS,
        ),
        (
            {"migrations": (_migration(state=EnumVendorState.UNRESOLVED),)},
            EnumVendorParityOutcome.VENDOR_STATE_UNRESOLVED,
        ),
        (
            # The re-run's own failure, after the vendor landed: a real red.
            {"gate": _red_gate(completed_at="2026-09-27T16:20:00Z")},
            EnumVendorParityOutcome.FAILED_AFTER_VENDOR,
        ),
        (
            {"migrations": (_migration(vendored_at=""),)},
            EnumVendorParityOutcome.FAILED_AFTER_VENDOR,
        ),
        (
            # One of two migrations still missing: the whole PR waits.
            {
                "migrations": (
                    _migration(),
                    _migration(
                        src_path="src/omnimarket/nodes/n/migrations/2.sql",
                        state=EnumVendorState.MISSING,
                    ),
                )
            },
            EnumVendorParityOutcome.NOT_VENDORED,
        ),
        (
            {"gate": _red_gate(run_id=None)},
            EnumVendorParityOutcome.VENDOR_STATE_UNRESOLVED,
        ),
        (
            {"run_attempt": MAX_HEAL_RUN_ATTEMPT},
            EnumVendorParityOutcome.ATTEMPT_CEILING,
        ),
    ],
)
def test_vendor_parity_refuses(
    overrides: dict[str, Any], outcome: EnumVendorParityOutcome
) -> None:
    decision = decide_vendor_parity(_parity(**overrides))
    assert decision.outcome is outcome
    assert not decision.act
    assert decision.run_ids == ()
    assert decision.job_ids == ()


# ---------------------------------------------------------------------------
# Collection and the driver, over a stub GitHub
# ---------------------------------------------------------------------------

INFRA = "OmniNode-ai/omnibase_infra"
MARKET = "OmniNode-ai/omnimarket"


def _check_run(
    cid: int, name: str, conclusion: str, run_id: int, completed_at: str
) -> dict[str, object]:
    return {
        "id": cid,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": completed_at,
        "details_url": f"https://github.com/{MARKET}/actions/runs/{run_id}/job/{cid}",
    }


class StubGh:
    """A GitHub whose PR heads move when a branch is updated."""

    def __init__(self) -> None:
        self.prs: dict[int, OpenPr] = {}
        self.runs_by_head: dict[str, list[dict[str, object]]] = {}
        self.logs: dict[int, str] = {}
        self.annotations_by_check: dict[int, list[dict[str, object]]] = {}
        self.files: dict[tuple[str, str, str], bytes] = {}
        self.commit_dates: dict[tuple[str, str], str] = {}
        self.jobs: dict[int, dict[str, object]] = {}
        self.run_jobs_by_run: dict[int, list[dict[str, object]]] = {}
        self.tags = ["v0.4.251", "v0.4.252"]
        self.dev_pyproject = b'[project]\nname = "omnimarket"\nversion = "0.4.253"\n'
        self.updates: list[tuple[int, str]] = []
        self.run_reruns: list[int] = []
        self.job_reruns: list[int] = []

    # reads
    def open_pull_requests(self, *, repo: str) -> tuple[OpenPr, ...]:
        return tuple(self.prs.values())

    def check_runs(self, *, repo: str, head_sha: str) -> list[dict[str, object]]:
        return list(self.runs_by_head.get(head_sha, []))

    def job_log(self, *, repo: str, job_id: int) -> str:
        return self.logs.get(job_id, "")

    def job(self, *, repo: str, job_id: int) -> dict[str, object]:
        return self.jobs.get(job_id, {"run_attempt": 1})

    def run_jobs(self, *, repo: str, run_id: int) -> list[dict[str, object]]:
        return list(self.run_jobs_by_run.get(run_id, []))

    def annotations(self, *, repo: str, check_run_id: int) -> list[dict[str, object]]:
        return list(self.annotations_by_check.get(check_run_id, []))

    def tag_names(self, *, repo: str) -> list[str]:
        return list(self.tags)

    def file_bytes(self, *, repo: str, path: str, ref: str) -> bytes | None:
        if repo == MARKET and path == "pyproject.toml" and ref == "dev":
            return self.dev_pyproject
        return self.files.get((repo, path, ref))

    def landed_at(self, *, repo: str, path: str, ref: str) -> str:
        return self.commit_dates.get((repo, path), "")

    # writes
    def update_branch(
        self, *, repo: str, pr_number: int, expected_head_sha: str
    ) -> None:
        current = self.prs[pr_number]
        if current.head_sha != expected_head_sha:
            raise RuntimeError("422 expected_head_sha does not match")
        self.updates.append((pr_number, expected_head_sha))
        # GitHub makes a merge commit: a new head with no checks yet.
        self.prs[pr_number] = replace(current, head_sha=f"merged-{len(self.updates)}")

    def rerun_run(self, *, repo: str, run_id: int) -> None:
        self.run_reruns.append(run_id)

    def rerun_job(self, *, repo: str, job_id: int) -> None:
        self.job_reruns.append(job_id)


def _stub_release_window_pr(gh: StubGh, number: int = 2905, head: str = HEAD) -> None:
    gh.prs[number] = _pr(number=number, head_sha=head)
    gh.runs_by_head[head] = [
        _check_run(
            500 + number,
            RELEASE_IDENTITY_CHECK,
            "failure",
            36319513074,
            "2026-09-27T12:35:59Z",
        )
    ]
    gh.logs[500 + number] = LIVE_RIG_LOG


def _stub_vendor_pr(
    gh: StubGh,
    *,
    number: int = 3014,
    head: str = "f767ef3b35c4477cdbe1fb33971f2a2be74dfa16",
    vendored: bytes | None = b"CREATE TABLE x();\n",
    ours: bytes = b"CREATE TABLE x();\n",
    vendored_at: str = "2026-09-27T16:01:07Z",
    failed_at: str = "2026-09-27T12:36:02Z",
    other_ci_reds: int = 0,
) -> None:
    gh.prs[number] = _pr(number=number, head_sha=head)
    parity_id, summary_id, parity_run, ci_run = (
        900 + number,
        950 + number,
        7000 + number,
        8000 + number,
    )
    gh.runs_by_head[head] = [
        _check_run(parity_id, VENDOR_PARITY_CHECK, "failure", parity_run, failed_at),
        _check_run(summary_id, CI_SUMMARY_CHECK, "failure", ci_run, failed_at),
        _check_run(400 + number, RELEASE_IDENTITY_CHECK, "success", ci_run, failed_at),
    ]
    gh.annotations_by_check[parity_id] = [{"message": LIVE_PARITY_ANNOTATION}]
    gh.files[(MARKET, SRC, head)] = ours
    if vendored is not None:
        gh.files[(INFRA, DEST, "dev")] = vendored
        gh.commit_dates[(INFRA, DEST)] = vendored_at
    gh.jobs[parity_id] = {"run_attempt": 1}
    ci_jobs: list[dict[str, object]] = [
        {"name": CI_SUMMARY_CHECK, "conclusion": "failure"}
    ]
    ci_jobs.extend(
        {"name": f"Tests (Split {i}/20)", "conclusion": "failure"}
        for i in range(other_ci_reds)
    )
    gh.run_jobs_by_run[ci_run] = ci_jobs


def test_release_window_heal_updates_once_and_not_again_on_the_next_pass() -> None:
    gh = StubGh()
    _stub_release_window_pr(gh)
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.updates == [(2905, HEAD)]
    # Pass 2: the new head has no checks yet, so nothing is red and nothing moves.
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.updates == [(2905, HEAD)]


def test_release_window_refuses_a_pr_inside_the_window_on_every_pass() -> None:
    gh = StubGh()
    _stub_release_window_pr(gh)
    gh.tags.append("v0.4.253")  # dev 0.4.253 is now published: back in a window
    assert main(["--repo", MARKET], gh=gh) == 0
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.updates == []


def test_release_window_refuses_a_second_update_when_the_new_head_fails_in_a_new_window() -> (
    None
):
    gh = StubGh()
    _stub_release_window_pr(gh)
    assert main(["--repo", MARKET], gh=gh) == 0
    new_head = gh.prs[2905].head_sha
    # The update's CI ran inside the NEXT window: it failed at dev's own version.
    gh.runs_by_head[new_head] = [
        _check_run(777, RELEASE_IDENTITY_CHECK, "failure", 1, "2026-09-27T16:40:00Z")
    ]
    gh.logs[777] = LIVE_RIG_LOG.replace("0.4.248", "0.4.253")
    gh.tags.append("v0.4.253")
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.updates == [(2905, HEAD)]


def test_release_window_refuses_a_conflicted_pr_and_a_real_failure() -> None:
    gh = StubGh()
    _stub_release_window_pr(gh)
    gh.prs[2905] = replace(gh.prs[2905], merge_state="DIRTY")
    _stub_release_window_pr(gh, number=2956, head="b" * 40)
    gh.logs[500 + 2956] = "ERROR: malformed version\n"
    decisions = collect_decisions(gh, repo=MARKET)
    outcomes = {d.pr_number: d.outcome for d in decisions.release_window}
    assert outcomes == {
        2905: EnumReleaseWindowOutcome.CONFLICTED,
        2956: EnumReleaseWindowOutcome.FAILURE_UNPARSED,
    }
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.updates == []


def test_release_window_caps_updates_per_pass() -> None:
    gh = StubGh()
    for n in range(MAX_UPDATES_PER_PASS + 2):
        _stub_release_window_pr(gh, number=3000 + n, head=f"{n:040d}")
    assert main(["--repo", MARKET], gh=gh) == 0
    assert len(gh.updates) == MAX_UPDATES_PER_PASS


def test_release_window_dry_run_writes_nothing() -> None:
    gh = StubGh()
    _stub_release_window_pr(gh)
    _stub_vendor_pr(gh)
    assert main(["--repo", MARKET, "--dry-run"], gh=gh) == 0
    assert gh.updates == []
    assert gh.run_reruns == []
    assert gh.job_reruns == []


def test_vendor_parity_heal_reruns_gate_then_summary_once() -> None:
    gh = StubGh()
    _stub_vendor_pr(gh)
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.run_reruns == [7000 + 3014]
    assert gh.job_reruns == [950 + 3014]
    # Pass 2: the re-run failed again, now AFTER the vendor landed -- a real red.
    head = gh.prs[3014].head_sha
    gh.runs_by_head[head][0] = _check_run(
        9999, VENDOR_PARITY_CHECK, "failure", 7000 + 3014, "2026-09-27T16:30:00Z"
    )
    gh.annotations_by_check[9999] = [{"message": LIVE_PARITY_ANNOTATION}]
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.run_reruns == [7000 + 3014]
    assert gh.job_reruns == [950 + 3014]


@pytest.mark.parametrize(
    ("vendored", "outcome"),
    [
        (None, EnumVendorParityOutcome.NOT_VENDORED),
        (b"CREATE TABLE y();\n", EnumVendorParityOutcome.VENDORED_COPY_DIFFERS),
    ],
)
def test_vendor_parity_refuses_when_infra_does_not_vendor_or_differs(
    vendored: bytes | None, outcome: EnumVendorParityOutcome
) -> None:
    gh = StubGh()
    _stub_vendor_pr(gh, number=2968, head="c" * 40, vendored=vendored)
    decisions = collect_decisions(gh, repo=MARKET)
    assert [d.outcome for d in decisions.vendor_parity] == [outcome]
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.run_reruns == []
    assert gh.job_reruns == []


def test_vendor_parity_rerun_is_superseded_by_a_branch_update() -> None:
    gh = StubGh()
    _stub_vendor_pr(gh, number=2905, head=HEAD)
    gh.runs_by_head[HEAD][2] = _check_run(
        500 + 2905, RELEASE_IDENTITY_CHECK, "failure", 1, "2026-09-27T12:35:59Z"
    )
    gh.logs[500 + 2905] = LIVE_RIG_LOG
    decisions = collect_decisions(gh, repo=MARKET)
    assert (
        decisions.release_window[0].outcome is EnumReleaseWindowOutcome.UPDATE_REQUIRED
    )
    assert (
        decisions.vendor_parity[0].outcome
        is EnumVendorParityOutcome.SUPERSEDED_BY_UPDATE
    )


def test_vendor_parity_leaves_ci_summary_when_the_ci_run_has_real_reds() -> None:
    gh = StubGh()
    _stub_vendor_pr(gh, other_ci_reds=2)
    assert main(["--repo", MARKET], gh=gh) == 0
    assert gh.run_reruns == [7000 + 3014]
    assert gh.job_reruns == []


def test_heal_selector_runs_one_heal_only() -> None:
    gh = StubGh()
    _stub_release_window_pr(gh)
    _stub_vendor_pr(gh)
    decisions = collect_decisions(gh, repo=MARKET, release_window=False)
    assert decisions.release_window == ()
    assert len(decisions.vendor_parity) == 2


def test_an_unreadable_open_pr_list_exits_red() -> None:
    class Broken(StubGh):
        def open_pull_requests(self, *, repo: str) -> tuple[OpenPr, ...]:
            raise RuntimeError("gh pr list exited 1")

    assert main(["--repo", MARKET], gh=Broken()) == 1


def test_every_write_failing_exits_red() -> None:
    class Refusing(StubGh):
        def rerun_run(self, *, repo: str, run_id: int) -> None:
            raise RuntimeError("403")

        def rerun_job(self, *, repo: str, job_id: int) -> None:
            raise RuntimeError("403")

    gh = Refusing()
    _stub_vendor_pr(gh)
    assert main(["--repo", MARKET], gh=gh) == 1


def test_the_cli_has_no_bypass_or_assertion_flags() -> None:
    options = {
        opt for action in _build_parser()._actions for opt in action.option_strings
    }
    for forbidden in (
        "--force",
        "--skip",
        "--dev-version",
        "--latest-tag",
        "--vendored",
    ):
        assert forbidden not in options


# ---------------------------------------------------------------------------
# AC5: the workflow
# ---------------------------------------------------------------------------


def _workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _on(doc: dict[Any, Any]) -> dict[str, Any]:
    on_block = doc.get("on", doc.get(True))
    assert isinstance(on_block, dict)
    return on_block


def test_workflow_is_scheduled_and_never_pull_request_triggered() -> None:
    on_block = _on(_workflow())
    assert "schedule" in on_block
    assert "workflow_dispatch" in on_block
    assert "pull_request" not in on_block
    assert "pull_request_target" not in on_block
    assert on_block["push"]["branches"] == ["dev"]


def test_workflow_scopes_write_grants_to_its_one_job() -> None:
    doc = _workflow()
    assert doc["permissions"] == {"contents": "read"}
    jobs = doc["jobs"]
    assert list(jobs) == ["heal"]
    assert jobs["heal"]["permissions"]["actions"] == "write"
    assert jobs["heal"]["permissions"].get("contents") == "read"


def test_workflow_updates_branches_with_an_app_token_not_the_workflow_token() -> None:
    """A GITHUB_TOKEN-authored update-branch commit starts no CI on the new head."""
    steps = _workflow()["jobs"]["heal"]["steps"]
    mint = [s for s in steps if "create-github-app-token" in str(s.get("uses", ""))]
    assert len(mint) == 1
    assert "continue-on-error" not in mint[0]
    heal = [s for s in steps if "pr_stale_red_heal.py" in str(s.get("run", ""))]
    assert len(heal) == 1
    assert "steps.app-token.outputs.token" in heal[0]["env"]["GH_UPDATE_TOKEN"]


def _keys(node: object) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _keys(v)]
    if isinstance(node, list):
        return [k for v in node for k in _keys(v)]
    return []


def test_workflow_has_no_continue_on_error_and_serialises_passes() -> None:
    doc = _workflow()
    assert "continue-on-error" not in _keys(doc)
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_workflow_app_token_is_scoped_to_this_repository() -> None:
    steps = _workflow()["jobs"]["heal"]["steps"]
    mint = next(s for s in steps if "create-github-app-token" in str(s.get("uses", "")))
    assert "repositories" not in mint["with"]
    assert "owner" not in mint["with"]
    assert mint["with"]["permission-contents"] == "write"


def test_vendor_parity_landing_time_is_the_merge_not_the_queued_commit_date() -> None:
    """omnibase_infra#4206: squash commit dated 15:38:41Z, merged to dev 16:01:07Z.

    A parity gate that failed at 15:53:01Z ran BEFORE the bytes reached dev, so
    it is stale; reading the committer date would call it a real red.
    """
    sha = "cd1d7e8829" + "0" * 30
    replies: list[object] = [
        [{"sha": sha, "commit": {"committer": {"date": "2026-09-27T15:38:41Z"}}}],
        [
            {
                "merge_commit_sha": sha,
                "base": {"ref": "dev"},
                "merged_at": "2026-09-27T16:01:07Z",
            }
        ],
    ]

    class Scripted(GhCli):
        def _json(self, args: list[str]) -> object:
            return replies.pop(0)

    landed = Scripted().landed_at(repo=INFRA, path=DEST, ref="dev")
    assert landed == "2026-09-27T16:01:07Z"
    gate = _red_gate(completed_at="2026-09-27T15:53:01Z")
    decision = decide_vendor_parity(
        _parity(gate=gate, migrations=(_migration(vendored_at=landed),))
    )
    assert decision.outcome is EnumVendorParityOutcome.RERUN_REQUIRED


def test_release_window_update_refuses_without_an_app_token() -> None:
    with pytest.raises(RuntimeError, match="GH_UPDATE_TOKEN"):
        GhCli().update_branch(repo=MARKET, pr_number=1, expected_head_sha=HEAD)
