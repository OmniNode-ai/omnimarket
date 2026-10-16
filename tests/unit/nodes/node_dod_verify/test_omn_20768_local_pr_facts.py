# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20768: dod_verify answers a merged PR's facts from the canonical clone
when the PR watcher no longer holds the PR, and a contract item that is itself a
``gh pr view`` (or a ``gh api .../contents``) command is answered from the same
local reads.

The world is a canonical clone whose ``origin/dev`` carries the PR's squash
commit and a PR watcher state that holds nothing for it: the shape of every PR
merged more than about 36 hours ago. ``gh`` is a recording stub on PATH, so a
test fails when a read the clone holds is sent to GitHub, and a second stub
that refuses the way the lane gh guard does is the positive control that the
stub is on the path the check runs through.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    DodEvidenceLocalSource,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_dod_verify.services.local_gh_reads import answer

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-20768"
_PR = 5001
_TITLE = f"feat({_TICKET}): read the merged PR from the clone"
_FILE_BODY = "def _walk_origin():\n    return 1\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    registry = tmp_path / "registry"
    clone = registry / "omnimarket"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "dev")
    _git(clone, "remote", "add", "origin", f"https://github.com/{_REPO}.git")
    (clone / "keep.txt").write_text("base\n")
    (clone / "old_name.py").write_text("x = 1\n" * 20)
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "chore: base (#4000)")
    parent = _git(clone, "rev-parse", "HEAD")
    (clone / "src.py").write_text(_FILE_BODY)
    (clone / "keep.txt").write_text("base\nchanged\n")
    _git(clone, "mv", "old_name.py", "new_name.py")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", f"{_TITLE} (#{_PR})")
    merge_sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "update-ref", "refs/remotes/origin/dev", merge_sha)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "schema": 1,
        "operator": "op",
        "last_tick": now,
        "last_full_resync": now,
        "repos": {"omnimarket": {"default_branch": "dev"}},
        "prs": {},
        "merges": {},
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    monkeypatch.setenv("OMNI_HOME", str(registry))
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(state_path))
    monkeypatch.setattr(
        hd_mod, "_default_local_source", DodEvidenceLocalSource, raising=False
    )
    monkeypatch.chdir(clone)
    return {"clone": str(clone), "merge_sha": merge_sha, "parent": parent}


def _gh_stub(tmp_path: Path, *, refuse: bool) -> tuple[Path, Path]:
    """A ``gh`` first on PATH that records each spawn; refusing, or silent."""
    bindir = tmp_path / "ghbin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "gh-spawns.log"
    body = f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n' + (
        'echo "REFUSED: gh pr view is a PR-state read" >&2\nexit 1\n'
        if refuse
        else "exit 0\n"
    )
    (bindir / "gh").write_text(body)
    (bindir / "gh").chmod(0o755)
    return bindir, log


def _spawns(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_answer_pr_view_files_for_a_pr_the_watcher_no_longer_holds(
    world: dict[str, str],
) -> None:
    out = answer(
        ["pr", "view", str(_PR), "--repo", _REPO, "--json", "files,state,mergeCommit"]
    )
    assert out is not None
    data = json.loads(out)
    assert data["state"] == "MERGED"
    assert data["mergeCommit"] == {"oid": world["merge_sha"]}
    assert {f["path"] for f in data["files"]} == {"src.py", "keep.txt", "new_name.py"}
    by_path = {f["path"]: f for f in data["files"]}
    assert by_path["src.py"]["additions"] == 2
    assert by_path["src.py"]["deletions"] == 0
    assert by_path["keep.txt"]["additions"] == 1


def test_answer_pr_view_title_is_the_squash_subject_without_the_pr_suffix(
    world: dict[str, str],
) -> None:
    out = answer(["pr", "view", str(_PR), "-R", _REPO, "--json", "title,number,url"])
    assert out is not None
    assert json.loads(out) == {
        "title": _TITLE,
        "number": _PR,
        "url": f"https://github.com/{_REPO}/pull/{_PR}",
    }


def test_answer_applies_a_jq_filter_like_gh(world: dict[str, str]) -> None:
    out = answer(
        [
            "pr",
            "view",
            str(_PR),
            "--repo",
            _REPO,
            "--json",
            "mergeCommit,state",
            "--jq",
            '.state + " " + .mergeCommit.oid',
        ]
    )
    assert out is not None
    assert out.strip() == f"MERGED {world['merge_sha']}"


def test_answer_declines_a_field_the_clone_cannot_hold(world: dict[str, str]) -> None:
    """The head sha of a merged PR is in no commit on the base branch: the
    answer is declined, never guessed."""
    assert (
        answer(["pr", "view", str(_PR), "--repo", _REPO, "--json", "headRefOid"])
        is None
    )


def test_answer_declines_a_pr_the_clone_does_not_hold(world: dict[str, str]) -> None:
    assert answer(["pr", "view", "9999", "--repo", _REPO, "--json", "state"]) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "view", str(_PR), "--repo", _REPO],  # no --json: human text
        ["pr", "merge", str(_PR), "--repo", _REPO, "--squash"],  # a write
        ["pr", "list", "--repo", _REPO, "--json", "number"],
        ["api", "repos/OmniNode-ai/omnimarket/pulls/5001"],
    ],
)
def test_answer_declines_every_other_gh_invocation(
    world: dict[str, str], argv: list[str]
) -> None:
    assert answer(argv) is None


def test_answer_contents_api_returns_the_file_at_the_commit(
    world: dict[str, str],
) -> None:
    out = answer(
        [
            "api",
            f"repos/{_REPO}/contents/src.py?ref={world['merge_sha']}",
            "--jq",
            ".content",
        ]
    )
    assert out is not None
    assert base64.b64decode(out.strip()).decode() == _FILE_BODY


def test_contract_item_that_is_a_gh_pr_view_command_passes_without_gh(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    collector = EvidenceCollector()
    check = {
        "check_type": "command",
        "check_value": (
            f"gh pr view {_PR} --repo {_REPO} --json files "
            "--jq '.files[].path' | grep -c '^src.py$'"
        ),
    }

    ok, message = collector._run_command_check(check, _TICKET)

    assert ok, message
    assert _spawns(log) == [], "the read went to GitHub, not the clone"


def test_contract_item_contents_read_passes_without_gh(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    check = {
        "check_type": "command",
        "check_value": (
            f"gh api repos/{_REPO}/contents/src.py?ref={world['merge_sha']} "
            "--jq '.content' | base64 -d | grep -c 'def _walk_origin'"
        ),
    }

    ok, message = EvidenceCollector()._run_command_check(check, _TICKET)

    assert ok, message
    assert _spawns(log) == []


def test_unanswerable_gh_command_still_reaches_the_real_gh(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the two tests above: the refusing gh really is on
    the path the check runs through, so a zero spawn count there is the local
    answer and not a stub that never ran."""
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    check = {
        "check_type": "command",
        "check_value": f"gh pr view {_PR} --repo {_REPO} --json headRefOid",
    }

    ok, message = EvidenceCollector()._run_command_check(check, _TICKET)

    assert not ok
    assert "REFUSED" in message
    assert _spawns(log) == [f"pr view {_PR} --repo {_REPO} --json headRefOid"]


def _checks_green(pr: int) -> ModelDodEvidenceGithubLookupResultEvent:
    command = ModelDodEvidenceGithubLookupCommand.model_validate(
        {
            "operation": EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            "repo": _REPO,
            "pr_number": pr,
        }
    )
    event = HandlerDodEvidenceGithubEffect().handle(command).events[0]
    assert isinstance(event, ModelDodEvidenceGithubLookupResultEvent)
    return event


def test_merged_pr_beyond_watcher_retention_is_green_on_the_merge_event(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    got = _checks_green(_PR)

    assert got.checks_green is True, got.detail
    assert world["merge_sha"][:12] in (got.detail or "")
    assert _spawns(log) == [], "a merged PR the clone holds needs no GitHub read"


def test_pr_neither_watcher_nor_clone_holds_stays_unresolved(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the green above comes from the squash commit on the
    base branch, not from the handler passing everything it cannot read."""
    bindir, _log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    got = _checks_green(9999)

    assert got.checks_green is False


def test_answer_contents_with_the_raw_media_type_returns_the_file_bytes(
    world: dict[str, str],
) -> None:
    out = answer(
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{_REPO}/contents/src.py?ref={world['merge_sha']}",
        ]
    )
    assert out == _FILE_BODY


def test_answer_contents_without_a_ref_reads_the_landing_branch(
    world: dict[str, str],
) -> None:
    out = answer(["api", f"repos/{_REPO}/contents/src.py", "--jq", ".content"])
    assert out is not None
    assert base64.b64decode(out.strip()).decode() == _FILE_BODY


def test_answer_contents_at_a_commit_the_clone_lacks_is_declined(
    world: dict[str, str],
) -> None:
    assert answer(["api", f"repos/{_REPO}/contents/src.py?ref={'ab' * 20}"]) is None


def test_answer_pull_files_lists_the_squash_commit_files(
    world: dict[str, str],
) -> None:
    out = answer(
        [
            "api",
            f"repos/{_REPO}/pulls/{_PR}/files",
            "--paginate",
            "--jq",
            '.[] | .filename + ":" + .status',
        ]
    )
    assert out is not None
    assert sorted(out.split()) == [
        "keep.txt:modified",
        "new_name.py:renamed",
        "src.py:added",
    ]


def test_contract_item_with_a_raw_media_type_header_passes_without_gh(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    check = {
        "check_type": "command",
        "check_value": (
            "gh api -H 'Accept: application/vnd.github.raw' "
            f'"repos/{_REPO}/contents/src.py?ref={world["merge_sha"]}" '
            "| grep -c 'def _walk_origin'"
        ),
    }

    ok, message = EvidenceCollector()._run_command_check(check, _TICKET)

    assert ok, message
    assert _spawns(log) == []


@pytest.fixture
def scm_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``onex`` whose ``scm pr-view`` answers PR 7777 as closed and
    unmerged, as the gateway does, and records each call."""
    from omnimarket.nodes.node_dod_verify.handlers import dod_evidence_local_source

    monkeypatch.setattr(dod_evidence_local_source, "_SCM_CACHE", {})
    log = tmp_path / "scm-calls.log"
    reply = {
        "verb": "pr-view",
        "outcome": "completed",
        "result": {
            "pr_number": 7777,
            "state": "closed",
            "merged": False,
            "head_ref": "fix/superseded",
            "head_sha": "cd" * 20,
            "base_ref": "dev",
        },
    }
    script = tmp_path / "onex"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$*" in\n'
        f"  *'#7777 '*) cat <<'JSON'\n{json.dumps(reply)}\nJSON\n;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("ONEX_DOD_VERIFY_SCM_BIN", str(script))
    return log


def test_closed_unmerged_pr_is_read_through_the_gateway_not_gh(
    world: dict[str, str],
    scm_gateway: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    check = {
        "check_type": "command",
        "check_value": (
            f"gh pr view 7777 --repo {_REPO} --json state --jq '.state' "
            "| grep -qx CLOSED && "
            f'test "$(gh pr view 7777 --repo {_REPO} --json mergedAt '
            '--jq \'.mergedAt // "null"\')" = null'
        ),
    }

    ok, message = EvidenceCollector()._run_command_check(check, _TICKET)

    assert ok, message
    assert _spawns(log) == []
    # The two reads in the command are two shim processes; the run's shared
    # cache means the gateway is asked once.
    assert scm_gateway.read_text().splitlines() == [
        f"scm pr-view {_REPO}#7777 --source bus --bus-lane dev"
    ]


def test_gateway_answer_for_a_pr_it_does_not_hold_stays_unresolved(
    world: dict[str, str], scm_gateway: Path
) -> None:
    """Positive control: the stand-in refuses every PR but 7777, and the read
    declines instead of inventing a state."""
    assert answer(["pr", "view", "8888", "--repo", _REPO, "--json", "state"]) is None


def test_tarball_read_is_served_from_the_clone(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = _gh_stub(tmp_path, refuse=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    check = {
        "check_type": "command",
        "check_value": (
            "python3 - <<'PY'\n"
            "import subprocess, tarfile, io\n"
            "archive = subprocess.check_output(['gh', 'api', '--cache', '1h', "
            f"'repos/{_REPO}/tarball/{world['merge_sha']}'])\n"
            "names = tarfile.open(fileobj=io.BytesIO(archive)).getnames()\n"
            "assert any(n.endswith('/src.py') for n in names), names\n"
            "PY"
        ),
    }

    ok, message = EvidenceCollector()._run_command_check(check, _TICKET)

    assert ok, message
    assert _spawns(log) == []
