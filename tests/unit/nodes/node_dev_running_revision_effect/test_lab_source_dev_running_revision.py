# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab source against a recorded build provenance and a fake process boundary, plus the seam to the provenance file.

Seam: the deploy agent's build writes ``/app/build-provenance.json`` into the runtime image. The document below is
spelled from the file read on the dev runtime on 2026-11-12 (keys only the source reads; the shas are the live ones).
A cross-repository check that skips when the other side is absent is refused by the skip ratchet, so the file's
shape is spelled here; when the agent changes the file, this document changes in the same change, and the PR body names the
live read made with the real source. This test goes red when OUR reader stops reading a key the file carries.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_running_revision_effect.adapters import (
    LabRunningRevisionSource,
)
from omnimarket.nodes.node_dev_running_revision_effect.overlay import (
    ModelMergeGitSettings,
    ModelProvenanceSshSettings,
    ModelRunningRevisionSourceSettings,
    ModelSourceRepository,
)
from omnimarket.nodes.node_dev_running_revision_effect.protocols.protocol_running_revision_source import (
    RunningRevisionSourceError,
)

pytestmark = pytest.mark.unit

OBSERVED = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
RUNNING_OMNIMARKET = "24c619c0f67c312e7df250180667b59c0b5fae80"
RUNNING_INFRA = "f3c34e95e9713fcd2f85c39833c690c6f687d7d7"
MERGE_1 = "a" * 40
MERGE_2 = "b" * 40
PROVENANCE = {
    "build_source": "workspace",
    "build_time": "2026-11-12T07:06:34Z",
    "infra_vcs_ref": RUNNING_INFRA,
    "per_repo_vcs_provenance": {
        "siblings": {
            "omnimarket": {
                "vcs_ref": RUNNING_OMNIMARKET,
                "vcs_dirty": False,
                "vcs_branch": "HEAD",
                "dev_merge_tag": "dev-20261112065932-24c619c0f67c",
            }
        },
        "build_context": {
            "omnibase_infra": {
                "vcs_ref": RUNNING_INFRA,
                "vcs_dirty": False,
                "vcs_branch": "dev",
                "dev_merge_tag": "dev-20261112062640-f3c34e95e971",
            }
        },
    },
}
SETTINGS = ModelRunningRevisionSourceSettings(
    ssh=ModelProvenanceSshSettings(
        host_env="TEST_RUNTIME_HOST",
        compose_project="proj",
        service="svc",
        provenance_path="/app/build-provenance.json",
        timeout_seconds=5,
    ),
    git=ModelMergeGitSettings(timeout_seconds=5, lookback_seconds=86400, max_merges=3),
    repositories=(
        ModelSourceRepository(
            repository="omnimarket",
            clone_path_env="TEST_CLONE_OMNIMARKET",
            ref="origin/dev",
            provenance_section="siblings",
        ),
        ModelSourceRepository(
            repository="omnibase_infra",
            clone_path_env="TEST_CLONE_INFRA",
            ref="origin/dev",
            provenance_section="build_context",
        ),
    ),
)
ENVIRON = {
    "TEST_RUNTIME_HOST": "runtime-host",
    "TEST_CLONE_OMNIMARKET": "/clones/omnimarket",
    "TEST_CLONE_INFRA": "/clones/infra",
}


class FakeProcesses:
    """Answers the ssh read and the git reads; records every argv."""

    def __init__(
        self,
        provenance: object = PROVENANCE,
        ssh_exit: int = 0,
        merges: dict[str, list[tuple[str, str]]] | None = None,
        ancestor: bool = True,
    ) -> None:
        self.provenance = provenance
        self.ssh_exit = ssh_exit
        self.merges = merges or {
            "/clones/omnimarket": [
                (MERGE_1, "2026-11-12T02:16:01-05:00"),
                (MERGE_2, "2026-11-12T08:00:00+00:00"),
            ],
            "/clones/infra": [],
        }
        self.ancestor = ancestor
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(argv))
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(
                argv, self.ssh_exit, json.dumps(self.provenance).encode(), b""
            )
        clone, verb = argv[2], argv[3]
        if verb == "merge-base":
            return subprocess.CompletedProcess(
                argv, 0 if self.ancestor else 1, b"", b""
            )
        if verb == "rev-list":
            return subprocess.CompletedProcess(argv, 0, ("c" * 40 + "\n").encode(), b"")
        lines = "".join(f"{sha}\t{stamp}\n" for sha, stamp in self.merges[clone])
        return subprocess.CompletedProcess(argv, 0, lines.encode(), b"")


def _source(fake: FakeProcesses) -> LabRunningRevisionSource:
    return LabRunningRevisionSource(SETTINGS, ENVIRON, fake)


def test_the_provenance_keys_the_reader_needs_are_read() -> None:
    facts = _source(FakeProcesses()).read(uuid4(), OBSERVED)
    (running,) = facts.builds
    shas = {r.repository: r.sha for r in running.manifest.revisions}
    assert shas == {"omnimarket": RUNNING_OMNIMARKET, "omnibase_infra": RUNNING_INFRA}
    assert running.manifest.built_at == datetime(2026, 11, 12, 7, 6, 34, tzinfo=UTC)
    assert running.running_since == running.manifest.built_at
    assert running.manifest.source_hash is None


def test_the_merge_history_starts_at_the_running_sha_and_is_utc() -> None:
    facts = _source(FakeProcesses()).read(uuid4(), OBSERVED)
    omnimarket = next(h for h in facts.histories if h.repository == "omnimarket")
    assert omnimarket.base_sha == RUNNING_OMNIMARKET
    assert [m.sha for m in omnimarket.merges] == [MERGE_1, MERGE_2]
    assert omnimarket.merges[0].merged_at == datetime(
        2026, 11, 12, 7, 16, 1, tzinfo=UTC
    )
    infra = next(h for h in facts.histories if h.repository == "omnibase_infra")
    assert infra.tip_sha == infra.base_sha == RUNNING_INFRA


def test_a_running_sha_off_the_ref_falls_back_to_the_lookback_base() -> None:
    fake = FakeProcesses(ancestor=False)
    facts = _source(fake).read(uuid4(), OBSERVED)
    assert all(h.base_sha == "c" * 40 for h in facts.histories)
    assert any(c[3] == "rev-list" and "--first-parent" in c for c in fake.calls)


def test_a_repository_the_provenance_does_not_name_is_left_out_of_the_manifest() -> (
    None
):
    document = json.loads(json.dumps(PROVENANCE))
    del document["per_repo_vcs_provenance"]["siblings"]["omnimarket"]
    facts = _source(FakeProcesses(provenance=document)).read(uuid4(), OBSERVED)
    (running,) = facts.builds
    assert [r.repository for r in running.manifest.revisions] == ["omnibase_infra"]


@pytest.mark.parametrize(
    "fake",
    [
        FakeProcesses(ssh_exit=255),
        FakeProcesses(provenance=["not", "an", "object"]),
        FakeProcesses(provenance={"build_source": "workspace"}),
        FakeProcesses(
            merges={
                "/clones/omnimarket": [
                    (chr(97 + n) * 40, f"2026-11-12T08:00:0{n}+00:00") for n in range(4)
                ],
                "/clones/infra": [],
            }
        ),
    ],
    ids=["ssh-exit", "not-an-object", "no-build-time", "over-max-merges"],
)
def test_an_unreadable_fact_raises_the_typed_source_error(fake: FakeProcesses) -> None:
    with pytest.raises(RunningRevisionSourceError):
        _source(fake).read(uuid4(), OBSERVED)


def test_a_missing_host_or_clone_variable_raises_the_typed_source_error() -> None:
    with pytest.raises(RunningRevisionSourceError, match="TEST_RUNTIME_HOST"):
        LabRunningRevisionSource(SETTINGS, {}, FakeProcesses()).read(uuid4(), OBSERVED)
    with pytest.raises(RunningRevisionSourceError, match="TEST_CLONE_OMNIMARKET"):
        LabRunningRevisionSource(
            SETTINGS, {"TEST_RUNTIME_HOST": "h"}, FakeProcesses()
        ).read(uuid4(), OBSERVED)
