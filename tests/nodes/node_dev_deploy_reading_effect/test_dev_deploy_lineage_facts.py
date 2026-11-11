# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The clone's lineage facts: ancestry, commit counts and the runtime-affecting commits between (OMN-23603)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters import (
    LabDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    GitLineageSource,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_reading_run import (
    HandlerDevDeployReadingRun,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployReadingCommand,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
    ModelMergeSettings,
)
from tests.nodes.node_dev_deploy_reading_effect.dev_deploy_effect_support import (
    LANE,
    overlay,
)

pytestmark = pytest.mark.unit

ENV = "TEST_CLONE"


def _git(repo: str, *args: str, **extra_env: str) -> str:
    """One git call in the fixture repository, with the hook's GIT_* location variables scrubbed."""
    done = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(
            {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                **extra_env,
            }
        ),
    )
    return done.stdout.strip()


def _commit(repo: str, path: str, content: str, date: str) -> str:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(content)
    _git(repo, "add", path)
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        path,
        GIT_AUTHOR_DATE=date,
        GIT_COMMITTER_DATE=date,
    )
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Any) -> tuple[str, dict[str, str]]:
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "master")
    base = _commit(repo, "src/base.py", "base", "2026-11-08T08:00:00+00:00")
    runtime = _commit(repo, "src/app.py", "app", "2026-11-08T09:00:00+00:00")
    docs = _commit(repo, "docs/notes.md", "notes", "2026-11-08T10:00:00+00:00")
    head = _commit(repo, "src/tail.py", "tail", "2026-11-08T11:00:00+00:00")
    _git(repo, "checkout", "-q", "-b", "sibling", base)
    sibling = _commit(repo, "src/sib.py", "sib", "2026-11-08T12:00:00+00:00")
    _git(repo, "checkout", "-q", "master")
    return repo, {
        "base": base,
        "runtime": runtime,
        "docs": docs,
        "head": head,
        "sibling": sibling,
    }


def _settings() -> ModelMergeSettings:
    return ModelMergeSettings(
        clone_path_env=ENV,
        ref="master",
        lookback_seconds=86400,
        runtime_path_globs=("src/**", "docker/*.yml"),
        git_timeout_seconds=5,
    )


def _source(tmp_path: Any, repo: str, **environ: Any) -> GitLineageSource:
    env = {ENV: repo, **environ}
    return GitLineageSource(_settings(), env)


def test_pair_descendant_is_ancestor_with_the_commit_count(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    fact = _source(tmp_path, repo).pair(shas["base"], shas["head"])
    assert fact.relation == "ancestor"
    assert fact.commits_between == 3
    assert fact.unread_reason is None


def test_pair_sibling_is_not_ancestor(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    fact = _source(tmp_path, repo).pair(shas["runtime"], shas["sibling"])
    assert fact.relation == "not-ancestor"
    assert fact.commits_between is None


def test_pair_unknown_sha_is_unread_with_a_reason(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    fact = _source(tmp_path, repo).pair(shas["base"], "f" * 40)
    assert fact.relation == "unread"
    assert fact.unread_reason


def test_pair_unset_clone_env_is_unread(tmp_path: Any) -> None:
    fact = GitLineageSource(_settings(), {}).pair("a" * 40, "b" * 40)
    assert fact.relation == "unread"
    assert ENV in fact.unread_reason


def test_pair_runner_that_raises_is_unread(tmp_path: Any) -> None:
    def boom(argv: Any, stdin: Any, timeout: Any) -> Any:
        raise OSError("no git")

    fact = GitLineageSource(_settings(), {ENV: "/tmp"}, run=boom).pair(
        "a" * 40, "b" * 40
    )
    assert fact.relation == "unread"
    assert fact.unread_reason


def test_pair_with_runtime_lists_the_runtime_affecting_commit(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    fact = _source(tmp_path, repo).pair_with_runtime(shas["base"], shas["runtime"])
    assert fact.relation == "ancestor"
    assert fact.runtime_affecting_between == (shas["runtime"],)


def test_pair_with_runtime_is_empty_when_only_docs_changed(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    fact = _source(tmp_path, repo).pair_with_runtime(shas["runtime"], shas["docs"])
    assert fact.relation == "ancestor"
    assert fact.runtime_affecting_between == ()


def _fake_fetch(lane: str, receipt_sha: str, result: str, observed_at: str) -> Any:
    def fetch(url: str, timeout: int) -> str:
        if url.endswith("/health"):
            return json.dumps({"details": {"healthy": True}})
        return json.dumps(
            {
                "rows": [
                    {
                        "lane": lane,
                        "receipt_sha": receipt_sha,
                        "receipt_result": result,
                        "receipt_observed_at": observed_at,
                    }
                ]
            }
        )

    return fetch


def _fake_health() -> Any:
    def fetch(url: str, timeout: int) -> str:
        return json.dumps({"details": {"healthy": True}})

    return fetch


def _fake_run(repo: str, observed_sha: str) -> Any:
    def run(argv: Any, stdin: Any, timeout: Any) -> Any:
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(
                args=list(argv),
                returncode=0,
                stdout=json.dumps({"vcs_ref": observed_sha}).encode(),
                stderr=b"",
            )
        return subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    return run


def _overlay_with_lineage(**lineage: Any) -> ModelDevDeployReadingOverlay:
    return overlay(
        merges={
            "clone_path_env": ENV,
            "ref": "master",
            "lookback_seconds": 86400,
            "runtime_path_globs": ["src/**", "docker/*.yml"],
            "git_timeout_seconds": 5,
        },
        lineage={
            "receipt_covers_descendant": True,
            "probe_accepts_descendant": True,
            "max_commits_between": 50,
            "receipt_cover_window_seconds": 86400,
            **lineage,
        },
    )


def test_read_fills_lineage_for_covered_merge_and_probe(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    ov = _overlay_with_lineage()
    facts = LabDevDeployFacts(
        ov,
        {ENV: repo, "TEST_SSH_HOST": "localhost"},
        run=_fake_run(repo, shas["head"]),
        fetch=_fake_fetch(LANE, shas["head"], "PASS", "2026-11-08T09:30:00+00:00"),
    ).read(datetime(2026, 11, 8, 12, 0, tzinfo=UTC))
    pairs = {(f.base_sha, f.head_sha) for f in facts.lineage}
    assert (shas["runtime"], shas["head"]) in pairs
    assert (shas["base"], shas["head"]) in pairs
    [probe_pair] = [
        f
        for f in facts.lineage
        if (f.base_sha, f.head_sha) == (shas["runtime"], shas["head"])
        and f.runtime_affecting_between
    ]
    assert probe_pair.runtime_affecting_between == (shas["head"],)


def test_read_with_both_switches_off_has_empty_lineage(tmp_path: Any) -> None:
    repo, shas = _repo(tmp_path)
    ov = _overlay_with_lineage(
        receipt_covers_descendant=False, probe_accepts_descendant=False
    )
    facts = LabDevDeployFacts(
        ov,
        {ENV: repo, "TEST_SSH_HOST": "localhost"},
        run=_fake_run(repo, shas["head"]),
        fetch=_fake_fetch(LANE, shas["head"], "PASS", "2026-11-08T09:30:00+00:00"),
    ).read(datetime(2026, 11, 8, 12, 0, tzinfo=UTC))
    assert facts.lineage == ()


def _reading(tmp_path: Any, **lineage: Any) -> Any:
    """Facts from a real clone through the run handler and the compute node: a PASS receipt and the running sha
    are both a later, non-runtime commit (docs) than the newest settled runtime-affecting merge."""
    repo, shas = _repo(tmp_path)
    ov = _overlay_with_lineage(**lineage)
    source = LabDevDeployFacts(
        ov,
        {ENV: repo, "TEST_SSH_HOST": "localhost"},
        run=_fake_run(repo, shas["docs"]),
        fetch=_fake_fetch(LANE, shas["docs"], "PASS", "2026-11-08T10:30:00+00:00"),
    )
    run = HandlerDevDeployReadingRun(
        overlay=ov,
        source=source,
        appender=None,
        clock=lambda: datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
        host="test-host",
        drill=False,
    )
    return run.handle(ModelDevDeployReadingCommand(correlation_id="c-1", write=False))


def test_run_a_later_receipt_and_a_later_running_sha_read_working(
    tmp_path: Any,
) -> None:
    result = _reading(tmp_path)
    assert result.status == "completed", result.failure
    assert result.verdict is V.WORKING, result.reasons


def test_run_with_both_switches_off_the_same_facts_read_not_working(
    tmp_path: Any,
) -> None:
    result = _reading(
        tmp_path, receipt_covers_descendant=False, probe_accepts_descendant=False
    )
    assert result.verdict is V.NOT_WORKING
    assert any(r.startswith("no-pass-receipt:") for r in result.reasons)
    assert any(r.startswith("probe-sha-mismatch:") for r in result.reasons)
