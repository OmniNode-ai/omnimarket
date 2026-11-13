# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20655: the closer reads merged PRs in every repository its overlay lists.

The closer's selection took its PR rows from the frozen repository's fixed list of product
repositories, which left out two repositories whose merges carry tickets' work: since 2026-10-01,
96 tickets have their merged PR only there (OMN-18470 is one: its PR is on the default branch,
main, of a documentation repository). The list and each repository's default branch are now overlay
data, and the binding rule accepts a merged ref on a repository's own default branch. Never a
loosening of what counts as proof: a ref on any other branch, and a merge on a branch the overlay
does not name for its repository, still read as not merged.
This repository is public, so the packaged overlay and these cases name no private repository: the
deployed overlay carries the full list, and the cases stand in `docs_main` and `config_main`
(default branch main) for the two repositories the frozen list left out.
Failing control: the overlay as the frozen list had it (without those repositories) reads OMN-18470
as having no merged PR and refuses a check at origin/main.
Golden chain: pr_rows over the in-memory bus. Error chain: a refused request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.events.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)
from omnimarket.events.dod_closeout_sweep import (
    EnumCloseoutDecisionKind,
    ModelBindingCheck,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_binding_rules import (
    refusal_of,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_rules import (
    merged_from_subjects,
    merged_prs,
    open_prs,
    pr_rows,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
    GitCloneMergeSubjects,
    InMemoryMergeSubjects,
    read_watcher_state,
    wait_for_watcher_state,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    OVERLAY_FILE_ENV,
    PACKAGED_OVERLAY,
    load_scope_overlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    bundle_text,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
KB_SUBJECT = "docs(OMN-18470): onboarding guide (#492)"
SHA = "0123456789abcdef0123456789abcdef01234567"


_MAIN_REPOSITORIES = [
    {"name": "docs_main", "default_branch": "main", "location": "beside_registry"},
    {"name": "config_main", "default_branch": "main", "location": "beside_registry"},
    {"name": "extra_main", "default_branch": "main", "location": "under_registry"},
    {"name": "root_main", "default_branch": "main", "location": "registry_root"},
]


def _frozen_list_overlay() -> ModelCloseoutScopeOverlay:
    """The packaged overlay: the repository list the frozen reader carried, no main-default one."""
    return load_scope_overlay(PACKAGED_OVERLAY)


def _overlay() -> ModelCloseoutScopeOverlay:
    """The packaged overlay plus the repositories whose default branch is main, as the deployed
    overlay lists them."""
    packaged = _frozen_list_overlay()
    return ModelCloseoutScopeOverlay.model_validate(
        {
            **packaged.model_dump(mode="json"),
            "pr_repositories": [
                *packaged.model_dump(mode="json")["pr_repositories"],
                *_MAIN_REPOSITORIES,
            ],
        }
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def _clone(path: Path, branch: str, subjects: list[str]) -> None:
    """A clone whose origin/<branch> carries one commit per subject."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    for subject in subjects:
        _git(path, "commit", "-q", "--allow-empty", "-m", subject)
    _git(path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")


def _state(tick: datetime, *prs: tuple[str, int, str, str, str]) -> dict[str, Any]:
    return {
        "schema": 1,
        "repos": {"omnimarket": {}},
        "last_full_resync": tick.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_tick": tick.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prs": {
            f"{repo}#{n}": {
                "facts": {
                    "repo": repo,
                    "number": n,
                    "title": title,
                    "head_ref": ref,
                    "state": st,
                }
            }
            for repo, n, title, ref, st in prs
        },
    }


# --- the repository list and the default branches are overlay data ---------------------------


def test_the_packaged_overlay_names_each_default_branch() -> None:
    branches = _frozen_list_overlay().default_branches()
    assert branches["omnimarket"] == "dev"
    assert "docs_main" not in branches
    assert _overlay().default_branches()["docs_main"] == "main"


def test_the_deployed_overlay_is_found_through_the_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "overlays"
    deployed = root / "node_dod_closeout_sweep_compute" / "overlay.yaml"
    deployed.parent.mkdir(parents=True)
    deployed.write_text(
        yaml.safe_dump(_overlay().model_dump(mode="json")), encoding="utf-8"
    )
    monkeypatch.delenv(OVERLAY_FILE_ENV, raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    assert load_scope_overlay().default_branches()["docs_main"] == "main"
    # failing control: with the roots unset the packaged document answers, with no such repository
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS")
    assert "docs_main" not in load_scope_overlay().default_branches()
    # a pinned file wins, and a pinned file that is absent is refused, never skipped
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(deployed))
    assert load_scope_overlay().default_branches()["docs_main"] == "main"
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(tmp_path / "absent.yaml"))
    with pytest.raises(ValueError, match="is not a file"):
        load_scope_overlay()


def test_an_overlay_that_drops_the_repository_list_is_refused_never_defaulted(
    tmp_path: Path,
) -> None:
    text = (NODE_DIR / "overlay.yaml").read_text(encoding="utf-8")
    cut = text.index("pr_repositories:")
    end = text.index("# The branch a merged-commit ref")
    broken = tmp_path / "overlay.yaml"
    broken.write_text(text[:cut] + text[end:], encoding="utf-8")
    with pytest.raises(ValueError, match="pr_repositories"):
        load_scope_overlay(broken)


# --- failing control built from OMN-18470 -----------------------------------------------------


def test_failing_control_the_frozen_list_reads_omn_18470_as_having_no_merged_pr() -> (
    None
):
    subjects = InMemoryMergeSubjects(
        {"docs_main": [KB_SUBJECT, "chore: other (#1)"]}
    ).subjects()
    merged_old, _ = merged_prs(subjects, _frozen_list_overlay())
    assert "OMN-18470" not in merged_old
    merged_new, _ = merged_prs(subjects, _overlay())
    assert merged_new["OMN-18470"] == ["docs_main#492"]


def test_failing_control_the_ticket_is_waiting_with_the_old_list_and_selected_with_the_new() -> (
    None
):
    subjects = {"docs_main": [KB_SUBJECT]}
    candidate = {
        "id": "OMN-18470",
        "state": "In Review",
        "sources": ["started"],
        "last_closeout_comment_at": "2026-10-01T00:00:00.000Z",
    }

    def bundle(overlay: ModelCloseoutScopeOverlay) -> dict[str, Any]:
        merged, _ = merged_prs(subjects, overlay)
        rows = [dict(r) for r in pr_rows(["OMN-18470"], merged, {})]
        return json.loads(bundle_text([candidate], rows, [], None, 12, overlay))

    old = bundle(_frozen_list_overlay())
    assert [c["id"] for c in old["candidates"]] == []
    assert old["waiting"][0]["reason"] == "no merged PR references it"
    new = bundle(_overlay())
    assert [c["id"] for c in new["candidates"]] == ["OMN-18470"]
    assert new["pr_state"][0]["merged"] == ["docs_main#492"]


def test_failing_control_the_binding_refuses_origin_main_with_the_frozen_rule_and_accepts_it_now() -> (
    None
):
    check = ModelBindingCheck(
        label="guide merged",
        kind="test",
        expect="passes",
        selector="tests/test_guide.py::test_guide",
        repo="docs_main",
        ref="origin/main",
    )
    assert refusal_of(check, _frozen_list_overlay()) == "not-on-merged-dev"
    assert refusal_of(check, _overlay()) is None


# --- the binding rule never loosens what counts as proof ------------------------------------


def _test_check(repo: str, ref: str) -> ModelBindingCheck:
    return ModelBindingCheck(
        label="x",
        kind="test",
        expect="passes",
        selector="tests/test_x.py::test_x",
        repo=repo,
        ref=ref,
    )


@pytest.mark.parametrize(
    ("repo", "ref"),
    [
        ("omnimarket", "origin/dev"),
        ("OmniNode-ai/omnimarket", "origin/dev"),
        ("docs_main", "origin/main"),
        ("config_main", "origin/main"),
        ("extra_main", "origin/main"),
        ("some_unlisted_repo", "origin/dev"),
        ("docs_main", SHA),
    ],
)
def test_a_merged_commit_on_the_repositorys_own_default_branch_is_accepted(
    repo: str, ref: str
) -> None:
    assert refusal_of(_test_check(repo, ref), _overlay()) is None


@pytest.mark.parametrize(
    ("repo", "ref"),
    [
        ("omnimarket", "origin/main"),
        ("docs_main", "origin/dev"),
        ("config_main", "origin/feature"),
        ("some_unlisted_repo", "origin/main"),
        ("omnimarket", "main"),
        ("omnimarket", "HEAD"),
        ("omnimarket", SHA[:12]),
        ("omnimarket", ""),
    ],
)
def test_failing_control_a_ref_off_the_default_branch_is_still_refused(
    repo: str, ref: str
) -> None:
    assert refusal_of(_test_check(repo, ref), _overlay()) == "not-on-merged-dev"


def test_a_command_check_with_no_repo_keeps_the_fallback_branch() -> None:
    check = ModelBindingCheck(
        label="x",
        kind="command",
        expect="ok",
        command="uv run pytest tests/test_x.py",
        cwd="/tmp",
        ref="origin/dev",
    )
    assert refusal_of(check, _overlay()) is None
    assert (
        refusal_of(check.model_copy(update={"ref": "origin/main"}), _overlay())
        == "not-on-merged-dev"
    )


def test_the_refuse_binding_decision_reads_the_overlay_it_is_given() -> None:
    request = ModelDodCloseoutDecisionRequest(
        kind=EnumCloseoutDecisionKind.REFUSE_BINDING,
        date="2026-11-08",
        check=_test_check("docs_main", "origin/main"),
        scope_overlay=_overlay(),
    )
    assert HandlerDodCloseoutSweep().handle(request).refused is False
    # failing control: the packaged document, which does not list the repository, refuses it
    assert (
        HandlerDodCloseoutSweep()
        .handle(request.model_copy(update={"scope_overlay": _frozen_list_overlay()}))
        .refusal_class
        == "not-on-merged-dev"
    )


# --- the PR reader ------------------------------------------------------------------------------


def test_merged_subjects_name_prs_by_ticket_with_no_date_window() -> None:
    got = merged_from_subjects(
        "omnimarket",
        [
            "fix(OMN-21083): served row (#4003)",
            "chore: other (#1)",
            "feat(OMN-1, OMN-2): both (#9)",
            "fix(OMN-3): a direct push, no number",
        ],
        _overlay(),
    )
    assert got == {
        "OMN-21083": ["omnimarket#4003"],
        "OMN-1": ["omnimarket#9"],
        "OMN-2": ["omnimarket#9"],
    }


def test_an_evidence_pr_subject_never_counts_as_a_merged_product_pr() -> None:
    assert (
        merged_from_subjects(
            "omnimarket", ["evidence(OMN-5): bindings (#7)"], _overlay()
        )
        == {}
    )


def test_open_prs_match_title_or_branch_and_skip_closed_and_evidence_prs() -> None:
    state = _state(
        NOW,
        ("config_main", 88, "fix(OMN-20718): split outcomes", "omn-20718-x", "OPEN"),
        ("omnimarket", 9, "chore: tidy", "omn20655-branch", "OPEN"),
        ("omnimarket", 10, "fix(OMN-1): merged", "omn-1", "MERGED"),
        ("onex_change_control", 11, "evidence(OMN-2): bindings", "omn-2", "OPEN"),
        (
            "omnimarket",
            12,
            "evidence(OMN-3): bindings (run-1)",
            "evidence-omn-3",
            "OPEN",
        ),
    )
    assert open_prs(state, _overlay()) == {
        "OMN-20718": ["config_main#88"],
        "OMN-20655": ["omnimarket#9"],
    }


def test_a_repository_the_caller_could_not_read_is_named_never_read_as_empty() -> None:
    merged, unread = merged_prs({"omnimarket": ["fix(OMN-1): x (#2)"]}, _overlay())
    assert merged == {"OMN-1": ["omnimarket#2"]}
    assert "docs_main" in unread
    assert "omnimarket" not in unread


def test_pr_rows_one_per_ticket_input_order_with_the_open_set_unread_an_error() -> None:
    rows = pr_rows(["omn-1", "OMN-1", "bogus"], {"OMN-1": ["omnimarket#1"]}, {})
    assert rows == [
        {"id": "OMN-1", "merged": ["omnimarket#1"], "open": [], "error": ""},
        {"id": "BOGUS", "merged": [], "open": [], "error": "not a ticket id"},
    ]
    why = "PR watcher state unreadable: gone"
    assert pr_rows(["OMN-1"], {"OMN-1": ["omnimarket#1"]}, None, why) == [
        {"id": "OMN-1", "merged": ["omnimarket#1"], "open": [], "error": why}
    ]


# --- the real backends -------------------------------------------------------------------------


def test_the_clone_source_reads_each_repository_on_its_own_default_branch(
    tmp_path: Path,
) -> None:
    home = tmp_path / "root_main"
    _clone(home / "omnimarket", "dev", ["fix(OMN-21083): served row (#4003)"])
    _clone(tmp_path / "docs_main", "main", [KB_SUBJECT])
    _clone(tmp_path / "config_main", "main", ["data(OMN-23033): unit (#57)"])
    _clone(home, "main", ["docs(OMN-19230): register plugin (#576)"])
    subjects = GitCloneMergeSubjects(home, _overlay()).subjects()
    merged, unread = merged_prs(subjects, _overlay())
    assert merged["OMN-18470"] == ["docs_main#492"]
    assert merged["OMN-23033"] == ["config_main#57"]
    assert merged["OMN-21083"] == ["omnimarket#4003"]
    assert merged["OMN-19230"] == ["root_main#576"]
    assert "omnibase_core" in unread


def test_failing_control_a_merge_on_a_branch_the_overlay_does_not_name_is_not_read(
    tmp_path: Path,
) -> None:
    home = tmp_path / "root_main"
    home.mkdir()
    # docs_main here has only origin/dev; the overlay names main for it.
    _clone(tmp_path / "docs_main", "dev", [KB_SUBJECT])
    subjects = GitCloneMergeSubjects(home, _overlay()).subjects()
    assert "docs_main" not in subjects
    merged, unread = merged_prs(subjects, _overlay())
    assert "OMN-18470" not in merged
    assert "docs_main" in unread


def test_stale_or_missing_watcher_state_is_a_reason_never_an_empty_open_set(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    path = tmp_path / "state.json"
    old = NOW - timedelta(seconds=overlay.watcher_tick_max_age_seconds + 60)
    path.write_text(json.dumps(_state(old)))
    state, why = read_watcher_state(path, NOW, overlay)
    assert state is None
    assert "stale" in why
    path.write_text(json.dumps(_state(NOW - timedelta(seconds=60))))
    state, why = read_watcher_state(path, NOW, overlay)
    assert state is not None
    assert why == ""
    _, why = read_watcher_state(tmp_path / "absent.json", NOW, overlay)
    assert why.startswith("PR watcher state unreadable")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", 2),
        ("repos", None),
        ("prs", []),
        ("last_full_resync", "2026-11-08T08:00:00Z"),
    ],
)
def test_failing_control_a_watcher_state_of_another_shape_or_age_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "state.json"
    state = _state(NOW)
    state[field] = value
    path.write_text(json.dumps(state))
    answer, why = read_watcher_state(path, NOW, _overlay())
    assert answer is None
    assert why


def test_the_watcher_wait_stops_at_the_deadline(tmp_path: Path) -> None:
    state, why = wait_for_watcher_state(
        tmp_path / "absent.json",
        _overlay(),
        wait_s=60,
        deadline=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert state is None
    assert why


# --- golden chain and error chain over the bus ---------------------------------------------------


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_pr_rows_reads_a_main_default_merge_over_the_bus(
    tmp_path: Path,
) -> None:
    runtime = await _run(
        tmp_path,
        {
            "kind": "pr_rows",
            "date": "2026-11-08",
            "pr_tickets": ["OMN-18470", "OMN-404"],
            "pr_subjects": {"docs_main": [KB_SUBJECT]},
            "pr_watcher_state": _state(NOW),
            "scope_overlay": _overlay().model_dump(mode="json"),
        },
    )
    result = runtime.handler_result
    assert isinstance(result, ModelDodCloseoutDecisionResult)
    assert result.pr_rows == [
        {
            "id": "OMN-18470",
            "merged": ["docs_main#492"],
            "open": [],
            "error": "",
        },
        {"id": "OMN-404", "merged": [], "open": [], "error": ""},
    ]
    assert "docs_main" not in (result.unread_repositories or [])
    assert "omnibase_core" in (result.unread_repositories or [])


@pytest.mark.asyncio
async def test_error_chain_pr_rows_without_its_subjects_fails_over_the_bus(
    tmp_path: Path,
) -> None:
    runtime = await _run(
        tmp_path, {"kind": "pr_rows", "date": "2026-11-08", "pr_tickets": ["OMN-1"]}
    )
    assert runtime.handler_result is None


def test_error_chain_a_watcher_state_that_was_not_read_puts_the_reason_on_every_row() -> (
    None
):
    request = ModelDodCloseoutDecisionRequest(
        kind=EnumCloseoutDecisionKind.PR_ROWS,
        date="2026-11-08",
        pr_tickets=["OMN-1"],
        pr_subjects={"omnimarket": ["fix(OMN-1): x (#2)"]},
        pr_watcher_error="PR watcher state unreadable: gone",
    )
    rows = HandlerDodCloseoutSweep().handle(request).pr_rows
    assert rows == [
        {
            "id": "OMN-1",
            "merged": ["omnimarket#2"],
            "open": [],
            "error": "PR watcher state unreadable: gone",
        }
    ]


def test_error_chain_pr_rows_refuses_a_field_it_does_not_take() -> None:
    with pytest.raises(ValueError, match="does not take"):
        ModelDodCloseoutDecisionRequest(
            kind=EnumCloseoutDecisionKind.PR_ROWS,
            date="2026-11-08",
            pr_tickets=["OMN-1"],
            pr_subjects={},
            max_candidates=3,
        )


# --- the command -----------------------------------------------------------------------------------


def test_the_prs_command_reads_a_main_default_merge_and_the_watcher(
    tmp_path: Path,
) -> None:
    home = tmp_path / "root_main"
    _clone(home / "omnimarket", "dev", ["fix(OMN-21083): served row (#4003)"])
    _clone(tmp_path / "docs_main", "main", [KB_SUBJECT])
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            _state(
                datetime.now(UTC),
                ("omnimarket", 77, "fix(OMN-21083): follow-up", "omn-21083-b", "OPEN"),
            )
        )
    )
    overlay_file = tmp_path / "deployed_overlay.yaml"
    overlay_file.write_text(
        yaml.safe_dump(_overlay().model_dump(mode="json")), encoding="utf-8"
    )
    run = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep", "prs",
         "--overlay", str(overlay_file), "OMN-18470", "OMN-21083", "OMN-404"],
        capture_output=True, text=True, check=False, timeout=60,
        env={"OMNI_HOME": str(home), "ONEX_PR_WATCHER_STATE": str(state),
             "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    rows = {r["id"]: r for r in json.loads(run.stdout)}
    assert rows["OMN-18470"]["merged"] == ["docs_main#492"]
    assert rows["OMN-21083"]["merged"] == ["omnimarket#4003"]
    assert rows["OMN-21083"]["open"] == ["omnimarket#77"]
    assert rows["OMN-404"] == {"id": "OMN-404", "merged": [], "open": [], "error": ""}
    assert "clones not read" in run.stderr


def test_error_chain_the_prs_command_with_an_unreadable_watcher_marks_every_row(
    tmp_path: Path,
) -> None:
    home = tmp_path / "root_main"
    home.mkdir()
    run = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep", "prs",
         "--wait-s", "0", "OMN-20718"],
        capture_output=True, text=True, check=False, timeout=60,
        env={"OMNI_HOME": str(home), "PATH": "/usr/bin:/bin",
             "ONEX_PR_WATCHER_STATE": str(tmp_path / "unread-state.json")},
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    [row] = json.loads(run.stdout)
    assert row["merged"] == []
    assert row["open"] == []
    assert row["error"].startswith("PR watcher state unreadable")


def test_error_chain_the_prs_command_refuses_without_omni_home() -> None:
    run = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep", "prs",
         "OMN-1"],
        capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert run.returncode == 2
    assert "OMNI_HOME is not set" in run.stderr


def test_the_refusal_names_the_branch_the_repository_merges_to() -> None:
    def reason(repo: str) -> str | None:
        return (
            HandlerDodCloseoutSweep()
            .handle(
                ModelDodCloseoutDecisionRequest(
                    kind=EnumCloseoutDecisionKind.REFUSE_BINDING,
                    date="2026-11-08",
                    check=_test_check(repo, "origin/feature"),
                    scope_overlay=_overlay(),
                )
            )
            .reason
        )

    assert reason("docs_main") == (
        "binding refused (not-on-merged-dev): the check does not run at origin/main "
        "or a full merged sha"
    )
    assert reason("omnimarket") == (
        "binding refused (not-on-merged-dev): the check does not run at origin/dev "
        "or a full merged sha"
    )
