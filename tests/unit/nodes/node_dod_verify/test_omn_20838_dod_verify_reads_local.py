# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20838: dod_verify's read layer reads the canonical clone and the PR
watcher's state, not GitHub.

Two tests carry the ticket:

* ``test_read_layer_resolves_with_gh_absent_from_path`` runs every operation of
  ``HandlerDodEvidenceGithubEffect`` with no ``gh`` on PATH, against a fixture
  canonical clone and a fixture watcher state, and every operation resolves.
* ``test_guard_read_layer_spawns_gh_only_for_the_required_context_set`` puts a
  recording ``gh`` first on PATH and fails when the read layer spawns ``gh``
  for any read other than the one with no local source: the base branch's
  required-context set (classic protection plus rulesets), once per base.

Both use the production default source (the conftest's empty source is
replaced by ``DodEvidenceLocalSource`` itself), so they exercise the handler
the way ``EvidenceCollector`` constructs it.
"""

from __future__ import annotations

import json
import os
import shutil
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

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-20838"
_PR = 4242
_REQUIRED = ("verify", "lint")
_KEPT_ENDPOINTS = ("/protection/required_status_checks", "/rules/branches/")


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
    """A canonical clone of omnimarket whose ``origin/dev`` carries the PR's
    squash commit, and a watcher state holding the merged PR and its head read."""
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
    (clone / "src.py").write_text("y = 2\n")
    (clone / "keep.txt").write_text("changed\n")
    _git(clone, "mv", "old_name.py", "new_name.py")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", f"feat({_TICKET}): read locally (#{_PR})")
    merge_sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "update-ref", "refs/remotes/origin/dev", merge_sha)
    head_sha = "ab" * 20

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "schema": 1,
        "operator": "op",
        "last_tick": now,
        "last_full_resync": now,
        "repos": {"omnimarket": {"default_branch": "dev"}},
        "prs": {
            f"omnimarket#{_PR}": {
                "facts": {
                    "repo": "omnimarket",
                    "number": _PR,
                    "state": "MERGED",
                    "merged_at": "2026-10-09T20:00:00Z",
                    "head_ref": f"jonah/{_TICKET.lower()}-local-reads",
                    "head_sha": head_sha,
                    "base": "dev",
                    "title": f"feat({_TICKET}): read locally",
                },
                "ci": {
                    "sha": head_sha,
                    "read_at": now,
                    "runs": [
                        ["verify", "completed", "success", "2026-10-09T19:50:00Z"],
                        ["lint", "completed", "success", "2026-10-09T19:49:00Z"],
                        ["advisory", "in_progress", "", ""],
                    ],
                    "detail": [
                        ["verify", "1001", "2026-10-09T19:40:00Z"],
                        ["lint", "1002", "2026-10-09T19:40:00Z"],
                    ],
                },
            }
        },
        "merges": {},
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))

    monkeypatch.setenv("OMNI_HOME", str(registry))
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(state_path))
    monkeypatch.setattr(
        hd_mod, "_default_local_source", DodEvidenceLocalSource, raising=False
    )
    return {
        "clone": str(clone),
        "merge_sha": merge_sha,
        "parent": parent,
        "head_sha": head_sha,
    }


def _path_without_gh(tmp_path: Path, recording_gh: Path | None) -> str:
    """A PATH holding git (and, when given, a recording gh) and nothing else."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    git = shutil.which("git")
    assert git is not None
    (bindir / "git").symlink_to(git)
    if recording_gh is not None:
        (bindir / "gh").symlink_to(recording_gh)
    return str(bindir)


def _run(
    operation: EnumDodEvidenceGithubOperation, **fields: object
) -> ModelDodEvidenceGithubLookupResultEvent:
    command = ModelDodEvidenceGithubLookupCommand.model_validate(
        {"operation": operation, **fields}
    )
    output = HandlerDodEvidenceGithubEffect().handle(command)
    event = output.events[0]
    assert isinstance(event, ModelDodEvidenceGithubLookupResultEvent)
    return event


def _all_operations(
    world: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, ModelDodEvidenceGithubLookupResultEvent]:
    monkeypatch.chdir(world["clone"])
    op = EnumDodEvidenceGithubOperation
    return {
        "merge_state": _run(op.FETCH_PR_MERGE_STATE, repo=_REPO, pr_number=_PR),
        "diff_facts": _run(op.FETCH_PR_DIFF_FACTS, repo=_REPO, pr_number=_PR),
        "pr_for_ticket": _run(op.LOOKUP_PR_FOR_TICKET, repo=_REPO, ticket_id=_TICKET),
        "repo_for_ticket": _run(op.LOOKUP_REPO_FOR_TICKET, ticket_id=_TICKET),
        "checks_green": _run(op.FETCH_PR_CHECKS_GREEN, repo=_REPO, pr_number=_PR),
    }


def test_read_layer_resolves_with_gh_absent_from_path(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_without_gh(tmp_path, None))
    assert shutil.which("gh") is None
    # The base branch's required-context set has no local source: it is the
    # one GitHub read kept, so this test supplies it the way that read's cache
    # holds it after one successful read.
    monkeypatch.setattr(
        hd_mod,
        "_REQUIRED_CONTEXTS_CACHE",
        {(_REPO, "dev"): ({"contexts": list(_REQUIRED)}, "", [], "")},
        raising=False,
    )

    got = _all_operations(world, monkeypatch)

    assert got["merge_state"].merged is True
    assert got["merge_state"].state == "MERGED"
    facts = got["diff_facts"].diff_facts
    assert got["diff_facts"].resolved is True
    assert facts is not None
    assert facts.merge_commit_sha == world["merge_sha"]
    assert facts.parent_commit_sha == world["parent"]
    assert {(f.path, f.status) for f in facts.changed_files} == {
        ("src.py", "added"),
        ("keep.txt", "modified"),
        ("new_name.py", "renamed"),
    }
    assert got["pr_for_ticket"].text_value == str(_PR)
    assert got["repo_for_ticket"].text_value == _REPO
    assert got["checks_green"].checks_green is True, got["checks_green"].detail


def test_required_context_set_unreadable_with_gh_absent_fails_closed(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the test above: with no gh and no cached set, the
    one kept read fails, and the rollup fails closed rather than passing."""
    monkeypatch.setenv("PATH", _path_without_gh(tmp_path, None))
    got = _all_operations(world, monkeypatch)
    assert got["checks_green"].checks_green is False
    assert got["merge_state"].merged is True


def test_guard_read_layer_spawns_gh_only_for_the_required_context_set(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "gh-spawns.log"
    recorder = tmp_path / "recording-gh"
    recorder.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$*" in\n'
        '  *protection/required_status_checks*) echo \'{"contexts":["verify","lint"]}\' ;;\n'
        "  *rules/branches/*) echo '[]' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    recorder.chmod(0o755)
    monkeypatch.setenv("PATH", _path_without_gh(tmp_path, recorder))

    got = _all_operations(world, monkeypatch)
    # A second rollup over the same base reads the required-context set from
    # the cache, not GitHub.
    _run(
        EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN, repo=_REPO, pr_number=_PR
    )

    spawns = log.read_text().splitlines() if log.exists() else []
    reads_with_a_local_source = [
        s for s in spawns if not any(e in s for e in _KEPT_ENDPOINTS)
    ]
    assert reads_with_a_local_source == [], (
        "dod_verify's read layer spawned gh for a read the canonical clone or "
        f"the PR watcher's state holds: {reads_with_a_local_source}"
    )
    assert sorted(spawns) == [
        f"api repos/{_REPO}/branches/dev/protection/required_status_checks",
        f"api repos/{_REPO}/rules/branches/dev",
    ]
    assert got["checks_green"].checks_green is True, got["checks_green"].detail


def test_watcher_read_missing_a_required_context_falls_back_to_github(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head the watcher read without a completed copy of every required
    context is not settled locally: the check-run listings are read from
    GitHub, so the verdict is the one the GitHub listing gives."""
    monkeypatch.setattr(
        hd_mod,
        "_REQUIRED_CONTEXTS_CACHE",
        {(_REPO, "dev"): ({"contexts": [*_REQUIRED, "advisory"]}, "", [], "")},
        raising=False,
    )
    log = tmp_path / "gh-spawns.log"
    recorder = tmp_path / "recording-gh"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 1\n')
    recorder.chmod(0o755)
    monkeypatch.setenv("PATH", _path_without_gh(tmp_path, recorder))
    monkeypatch.chdir(world["clone"])

    got = _run(
        EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN, repo=_REPO, pr_number=_PR
    )

    spawns = log.read_text().splitlines()
    assert any("/check-suites" in s for s in spawns), spawns
    assert got.checks_green is False


def test_shallow_clone_does_not_answer_parents_or_ticket_search(
    world: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted runner's depth-1 checkout holds the squash commit but not its
    parent or the older history: those facts fall to GitHub, never a guess."""
    registry = tmp_path / "shallow-registry"
    registry.mkdir()
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "dev",
            f"file://{world['clone']}",
            str(registry / "omnimarket"),
        ],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )
    monkeypatch.setenv("OMNI_HOME", str(registry))
    source = DodEvidenceLocalSource()

    assert source.squash_commit(_REPO, _PR, "dev") == world["merge_sha"]
    assert source.commit_parents(_REPO, world["merge_sha"]) is None
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(tmp_path / "absent.json"))
    assert (
        source.merged_pr_candidates(
            _REPO, _TICKET, hd_mod._ticket_token_pattern(_TICKET)
        )
        == []
    )
