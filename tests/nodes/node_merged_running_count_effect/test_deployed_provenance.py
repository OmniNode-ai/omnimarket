# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence kind deployed from the running build's provenance file (OMN-22549).

Golden: a merge in the history of the ref the build records is named as running, and one outside it is not.
Error: every way the file or the history cannot be read is an unread cause, never evidence and never silence.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.model_runtime_host_reads import (
    ModelRuntimeHostReadOverlay,
    ModelRuntimeRepoRef,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_deployed_provenance import (
    Ancestry,
    ModelProvenanceLabels,
    SourceDeployedProvenance,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)
INFRA_REF = "a" * 40
MARKET_REF = "b" * 40
IN_INFRA = "1" * 40
OUT_INFRA = "2" * 40
IN_MARKET = "3" * 40
PROVENANCE = {
    "build_time": "2026-11-05T21:18:46Z",
    "infra_vcs_ref": INFRA_REF,
    "per_repo_vcs_provenance": {"siblings": {"omnimarket": {"vcs_ref": MARKET_REF}}},
}
MERGES = [
    ("OmniNode-ai/omnibase_infra", 10, IN_INFRA),
    ("OmniNode-ai/omnibase_infra", 11, OUT_INFRA),
    ("OmniNode-ai/omnimarket", 20, IN_MARKET),
]
OVERLAY = ModelRuntimeHostReadOverlay(
    ssh_host_env="RUNTIME_SSH",
    compose_project="proj",
    fleet_service="fleet-svc",
    provenance_service="prov-svc",
    provenance_path="/app/build-provenance.json",
    repo_refs=(
        ModelRuntimeRepoRef(
            repo="omnibase_infra",
            clone_relpath="omnibase_infra",
            ref_path=("infra_vcs_ref",),
        ),
        ModelRuntimeRepoRef(
            repo="omnimarket",
            clone_relpath="omnimarket",
            ref_path=("per_repo_vcs_provenance", "siblings", "omnimarket", "vcs_ref"),
        ),
    ),
)
LABELS = ModelProvenanceLabels(
    kind="deployed",
    source_template="build provenance {repo}@{ref}",
    git_timeout_seconds=5,
)


def history(clone: Path, ref: str, timeout: int) -> frozenset[str]:
    if ref == INFRA_REF:
        return frozenset({IN_INFRA})
    if ref == MARKET_REF:
        return frozenset({IN_MARKET})
    raise ValueError("unknown ref")


def source(
    document: object = PROVENANCE,
    *,
    code: int = 0,
    environ: dict[str, str] | None = None,
    ancestry: Ancestry = history,
    seen: list[tuple[Sequence[str], bytes | None]] | None = None,
) -> SourceDeployedProvenance:
    def run(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if seen is not None:
            seen.append((argv, stdin))
        return subprocess.CompletedProcess(
            argv, code, json.dumps(document).encode(), b""
        )

    return SourceDeployedProvenance(
        overlay=OVERLAY,
        labels=LABELS,
        environ={"RUNTIME_SSH": "runtime-host"} if environ is None else environ,
        home=Path("/registry"),
        now=NOW,
        run=run,
        ancestry=ancestry,
    )


def test_golden_a_merge_in_the_deployed_history_is_running_and_one_outside_is_not() -> (
    None
):
    seen: list[tuple[Sequence[str], bytes | None]] = []
    read = source(seen=seen).read(MERGES)
    assert read.unread_cause == ""
    assert {(e.repo, e.number) for e in read.evidence} == {
        ("omnibase_infra", 10),
        ("omnimarket", 20),
    }
    first = read.evidence[0]
    assert first.kind == "deployed"
    assert first.source == f"build provenance omnibase_infra@{INFRA_REF[:12]}"
    assert first.observed_at == datetime(2026, 11, 5, 21, 18, 46, tzinfo=UTC)
    argv, stdin = seen[0]
    assert "runtime-host" in argv
    assert stdin is not None
    assert b"label=com.docker.compose.service=prov-svc" in stdin
    assert b"cat /app/build-provenance.json" in stdin


def test_error_the_host_variable_unset_is_an_unread_cause_and_no_evidence() -> None:
    read = source(environ={}).read(MERGES)
    assert read.evidence == ()
    assert "RUNTIME_SSH is not set" in read.unread_cause


def test_error_a_failed_ssh_read_is_an_unread_cause_and_no_evidence() -> None:
    read = source(code=255).read(MERGES)
    assert read.evidence == ()
    assert "exited 255" in read.unread_cause


def test_error_a_file_that_is_not_an_object_is_an_unread_cause() -> None:
    read = source(document=["not", "an", "object"]).read(MERGES)
    assert read.evidence == ()
    assert "not an object" in read.unread_cause


def test_error_a_repository_with_no_ref_in_the_file_names_it_and_keeps_the_other() -> (
    None
):
    read = source(document={"infra_vcs_ref": INFRA_REF}).read(MERGES)
    assert [(e.repo, e.number) for e in read.evidence] == [("omnibase_infra", 10)]
    assert "omnimarket: no deployed ref at per_repo_vcs_provenance" in read.unread_cause


def test_error_a_history_the_clone_cannot_give_names_the_repository() -> None:
    def broken(clone: Path, ref: str, timeout: int) -> frozenset[str]:
        raise ValueError("git rev-list exited 128")

    read = source(ancestry=broken).read(MERGES)
    assert read.evidence == ()
    assert "omnibase_infra: history of the deployed ref unreadable" in read.unread_cause
    assert "omnimarket: history of the deployed ref unreadable" in read.unread_cause


def test_control_a_source_that_trusts_every_merge_is_caught() -> None:
    """The break the golden test names: a reader that returned every merge would name the one outside the history."""
    read = source().read(MERGES)
    assert ("omnibase_infra", 11) not in {(e.repo, e.number) for e in read.evidence}


def test_error_an_absent_overlay_is_a_named_cause_and_no_source(tmp_path: Path) -> None:
    from omnimarket.nodes.node_merged_running_count_effect.runtime_merged_running_count import (
        build_provenance_source,
    )

    declared = {
        "overlay_id": "runtime_host_reads",
        "overlay_file_env": "RUNTIME_HOST_READS_OVERLAY_PATH",
        "kind": "deployed",
        "source_template": "build provenance {repo}@{ref}",
        "git_timeout_seconds": 5,
    }
    built, cause = build_provenance_source(declared, {}, tmp_path, NOW)
    assert built is None
    assert "runtime_host_reads" in cause


def test_golden_the_shipped_overlay_shape_builds_a_source(tmp_path: Path) -> None:
    from omnimarket.nodes.node_merged_running_count_effect.runtime_merged_running_count import (
        build_provenance_source,
    )

    root = tmp_path / "overlays" / "runtime_host_reads"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        "ssh_host_env: ONEX_RUNTIME_SSH_HOST\ncompose_project: proj\nfleet_service: svc\n"
        "provenance_service: svc\nprovenance_path: /app/build-provenance.json\n"
        "repo_refs:\n  - {repo: omnimarket, clone_relpath: omnimarket,"
        " ref_path: [per_repo_vcs_provenance, siblings, omnimarket, vcs_ref]}\n"
    )
    declared = {
        "overlay_id": "runtime_host_reads",
        "overlay_file_env": "RUNTIME_HOST_READS_OVERLAY_PATH",
        "kind": "deployed",
        "source_template": "build provenance {repo}@{ref}",
        "git_timeout_seconds": 5,
    }
    built, cause = build_provenance_source(
        declared,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
        tmp_path,
        NOW,
    )
    assert cause == ""
    assert built is not None


# --- A running commit the reader's own clone never fetched (OMN-23031) -------------------------------------------------
#
# The pair that was read as "merged, not running" for 2773 minutes: both merges were ancestors of the running revision, but
# that revision was installed from a branch, so the floor host's clone had never fetched it and `git rev-list` exited 128.
# Replayed here with real git: the reader's clone holds the two merges and not the running commit; the runtime host's clone
# holds the running commit on top of them.


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


class Pair:
    """Two clones of one history: the reader's stops at the second merge, the runtime host's runs on past it."""

    def __init__(self, root: Path) -> None:
        self.reader_home = root / "reader"
        self.host_home = root / "host"
        reader = self.reader_home / "omnibase_infra"
        reader.mkdir(parents=True)
        _git(reader, "init", "-q", "-b", "dev")
        self.merge_a = self._commit(reader, "first merge")
        self.merge_b = self._commit(reader, "second merge")
        host = self.host_home / "omnibase_infra"
        self.host_home.mkdir()
        subprocess.run(
            ["git", "clone", "-q", str(reader), str(host)],
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )
        _git(host, "checkout", "-q", "-b", "branch-install")
        self.running = self._commit(host, "branch commit, never merged")
        self.unmerged = self._commit(reader, "a later merge the build lacks")

    @staticmethod
    def _commit(repo: Path, message: str) -> str:
        _git(repo, "commit", "-q", "--allow-empty", "-m", message)
        return _git(repo, "rev-parse", "HEAD")


def _pair_source(
    pair: Pair,
    *,
    runtime_clone_home: str | None,
    ref: str | None = None,
    seen: list[str] | None = None,
) -> SourceDeployedProvenance:
    document = {"infra_vcs_ref": pair.running if ref is None else ref}

    def run(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        script = (stdin or b"").decode()
        if seen is not None:
            seen.append(script)
        if "rev-list" not in script:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(document).encode(), b""
            )
        return subprocess.run(
            ["bash", "-s"],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=scrub_git_location_env(os.environ),
        )

    return SourceDeployedProvenance(
        overlay=OVERLAY.model_copy(
            update={
                "runtime_clone_home": runtime_clone_home,
                "repo_refs": OVERLAY.repo_refs[:1],
            }
        ),
        labels=LABELS,
        environ={"RUNTIME_SSH": "runtime-host"},
        home=pair.reader_home,
        now=NOW,
        run=run,
    )


def _merges(pair: Pair) -> list[tuple[str, int, str]]:
    return [
        ("OmniNode-ai/omnibase_infra", 5663, pair.merge_a),
        ("OmniNode-ai/omnibase_infra", 5674, pair.merge_b),
        ("OmniNode-ai/omnibase_infra", 5708, pair.unmerged),
    ]


def test_golden_an_ancestor_of_a_running_commit_the_reader_never_fetched_is_running(
    tmp_path: Path,
) -> None:
    pair = Pair(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        _git(pair.reader_home / "omnibase_infra", "cat-file", "-e", pair.running)
    read = _pair_source(pair, runtime_clone_home=str(pair.host_home)).read(
        _merges(pair)
    )
    assert read.unread_cause == ""
    assert {e.number for e in read.evidence} == {5663, 5674}
    assert {e.merge_sha for e in read.evidence} == {pair.merge_a, pair.merge_b}


def test_control_without_the_runtime_clone_home_the_pair_is_not_placed(
    tmp_path: Path,
) -> None:
    """The break the golden test names: the reader as it was, with only its own clone, cannot place either merge."""
    pair = Pair(tmp_path)
    read = _pair_source(pair, runtime_clone_home=None).read(_merges(pair))
    assert read.evidence == ()
    assert "omnibase_infra: history of the deployed ref unreadable" in read.unread_cause


def test_error_a_commit_neither_clone_holds_names_both_causes(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    read = _pair_source(
        pair, runtime_clone_home=str(pair.host_home), ref="f" * 40
    ).read(_merges(pair))
    assert read.evidence == ()
    assert "omnibase_infra: history of the deployed ref unreadable" in read.unread_cause
    assert "on the runtime host" in read.unread_cause


def test_error_a_ref_that_is_not_a_commit_id_never_reaches_the_runtime_host(
    tmp_path: Path,
) -> None:
    pair = Pair(tmp_path)
    seen: list[str] = []
    read = _pair_source(
        pair, runtime_clone_home=str(pair.host_home), ref="dev; touch /tmp/x", seen=seen
    ).read(_merges(pair))
    assert read.evidence == ()
    assert "not a commit id" in read.unread_cause
    assert not any("rev-list" in script for script in seen)
