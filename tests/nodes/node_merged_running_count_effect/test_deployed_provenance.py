# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence kind deployed from the running build's provenance file (OMN-22549).

Golden: a merge in the history of the ref the build records is named as running, and one outside it is not.
Error: every way the file or the history cannot be read is an unread cause, never evidence and never silence.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.model_runtime_host_reads import (
    ModelRuntimeHostReadOverlay,
    ModelRuntimeRepoRef,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_deployed_provenance import (
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
    ancestry=history,  # type: ignore[no-untyped-def]
    seen: list[tuple[Sequence[str], bytes | None]] | None = None,
) -> SourceDeployedProvenance:
    def run(argv: Sequence[str], stdin: bytes | None, timeout: int):  # type: ignore[no-untyped-def]
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
