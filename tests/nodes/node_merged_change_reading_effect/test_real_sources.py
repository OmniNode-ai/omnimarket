# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real sources with their boundaries replaced: build facts from the provenance reader, the merge sha from a clone."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_merged_change_reading import (
    ModelMcrChange,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
)
from omnimarket.models.model_merged_running_count import (
    ModelMrcEvidence,
    ModelMrcEvidenceRead,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrRunnerOverlay,
    ModelMcrStoreCheckout,
)
from omnimarket.nodes.node_merged_change_reading_effect.runtime_merged_change_reading import (
    main,
    parse_change,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_merged_change_reading import (
    SourceMergedChangeReading,
)

from .support import labels

WHEN = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


class _Provenance:
    def __init__(self, read: ModelMrcEvidenceRead) -> None:
        self._read = read
        self.asked: list[tuple[str, int, str]] = []

    def read(self, merges: Iterable[tuple[str, int, str]]) -> ModelMrcEvidenceRead:
        self.asked = list(merges)
        return self._read


def _source(provenance: Any, home: Path) -> SourceMergedChangeReading:
    return SourceMergedChangeReading(
        runners=ModelMcrRunnerOverlay(runners=()),
        provenance=provenance,
        provenance_repos=("omnimarket", "omnibase_infra"),
        runtime=None,
        labels=labels(),
        home=home,
    )


def _changes() -> list[ModelMcrChange]:
    return [
        ModelMcrChange(repo="omnimarket", number=1, merge_sha="a" * 40),
        ModelMcrChange(repo="omnimarket", number=2, merge_sha="b" * 40),
        ModelMcrChange(repo="overlays", number=3, merge_sha="c" * 40),
        ModelMcrChange(repo="omnibase_infra", number=4),
    ]


def test_build_facts_in_and_not_in_build_and_unrecorded_repo_unread(
    tmp_path: Path,
) -> None:
    evidence = ModelMrcEvidenceRead(
        evidence=(
            ModelMrcEvidence(
                repo="omnimarket",
                kind="deployed",
                source="p",
                number=1,
                merge_sha="a" * 40,
                observed_at=WHEN,
            ),
        )
    )
    provenance = _Provenance(evidence)
    facts, unread, started = _source(provenance, tmp_path).read_build(_changes())
    assert {(f.repo, f.number): f.in_build for f in facts} == {
        ("omnimarket", 1): True,
        ("omnimarket", 2): False,
    }
    # overlays records no ref in the provenance file; the omnibase_infra change has no sha.
    assert unread == ("omnibase_infra#4", "overlays")
    assert started is None
    assert [m[1] for m in provenance.asked] == [1, 2]


def test_error_provenance_unreadable_marks_every_recorded_repo_unread(
    tmp_path: Path,
) -> None:
    provenance = _Provenance(
        ModelMrcEvidenceRead(unread_cause="build provenance unreadable: ValueError: x")
    )
    facts, unread, _ = _source(provenance, tmp_path).read_build(_changes())
    assert facts == ()
    assert "omnimarket" in unread
    assert "overlays" in unread


def test_error_one_repo_history_unread_keeps_the_other_read(tmp_path: Path) -> None:
    provenance = _Provenance(
        ModelMrcEvidenceRead(
            unread_cause="omnibase_infra: history of the deployed ref unreadable (x)"
        )
    )
    facts, unread, _ = _source(provenance, tmp_path).read_build(_changes())
    assert {f.repo for f in facts} == {"omnimarket"}
    assert "omnibase_infra" in unread
    assert "omnimarket" not in unread


def test_no_provenance_source_reads_every_repo_unread(tmp_path: Path) -> None:
    facts, unread, _ = _source(None, tmp_path).read_build(_changes())
    assert facts == ()
    # the omnibase_infra change without a sha is also named
    assert unread == ("omnibase_infra", "omnibase_infra#4", "omnimarket", "overlays")


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.decode().strip()


def test_merge_sha_resolved_from_a_clone_by_the_contracts_pattern(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "omnimarket"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "OMN-1 the change (#41)")
    expected = _git(clone, "rev-parse", "HEAD")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "OMN-2 another (#410)")
    _git(clone, "update-ref", "refs/remotes/origin/dev", "HEAD")
    resolved = _source(None, tmp_path).resolve_changes(
        [
            ModelMcrChange(repo="omnimarket", number=41),
            ModelMcrChange(repo="omnimarket", number=999),
        ]
    )
    # the contract's pattern anchors the number: #410 is not read as #41
    assert resolved[0].merge_sha == expected
    assert resolved[1].merge_sha == ""


def test_parse_change_forms() -> None:
    assert parse_change("OmniNode-ai/omnimarket#6051") == ModelMcrChange(
        repo="omnimarket", number=6051
    )
    assert parse_change("omnimarket#6051=abc").merge_sha == "abc"
    with pytest.raises(SystemExit):
        parse_change("omnimarket-6051")


def test_main_refuses_when_the_registry_home_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(SystemExit, match="OMNI_HOME"):
        main(["--change", "omnimarket#1"])


class _StoreRuntime:
    """Stands for the runtime host reader: answers the checkout's ancestry question from a table."""

    def __init__(self, answers: dict[str, bool | None], fail: bool = False) -> None:
        self.answers = answers
        self.fail = fail
        self.asked: list[tuple[str, Sequence[str]]] = []

    def checkout_ancestry(
        self, path: str, shas: Sequence[str]
    ) -> dict[str, bool | None]:
        self.asked.append((path, shas))
        if self.fail:
            raise ValueError("host unreachable")
        return {s: self.answers.get(s) for s in shas}

    def read(
        self, runners: Sequence[ModelMcrRunner]
    ) -> tuple[ModelMcrRuntimeFacts, datetime | None]:
        return ModelMcrRuntimeFacts(), None


def _store_source(runtime: _StoreRuntime, tmp_path: Path) -> SourceMergedChangeReading:
    return SourceMergedChangeReading(
        runners=ModelMcrRunnerOverlay(
            runners=(),
            store_checkouts=(
                ModelMcrStoreCheckout(repo="overlays", path="/srv/store/overlays"),
            ),
        ),
        provenance=None,
        provenance_repos=(),
        runtime=runtime,
        labels=labels(),
        home=tmp_path,
    )


def test_store_checkout_ancestry_gives_overlay_build_facts(tmp_path: Path) -> None:
    runtime = _StoreRuntime({"c" * 40: True, "d" * 40: False})
    changes = [
        ModelMcrChange(repo="overlays", number=1, merge_sha="c" * 40),
        ModelMcrChange(repo="overlays", number=2, merge_sha="d" * 40),
        ModelMcrChange(repo="overlays", number=3, merge_sha="e" * 40),
        ModelMcrChange(repo="overlays", number=4),
    ]
    facts, unread, _ = _store_source(runtime, tmp_path).read_build(changes)
    assert {(f.number, f.in_build, f.source) for f in facts} == {
        (1, True, "overlay store checkout"),
        (2, False, "overlay store checkout"),
    }
    # the host could not say for the third; the fourth has no sha; the repository itself was read
    assert unread == ("overlays#3", "overlays#4")
    assert runtime.asked[0][0] == "/srv/store/overlays"


def test_error_store_host_unreachable_reads_the_repository_unread(
    tmp_path: Path,
) -> None:
    runtime = _StoreRuntime({}, fail=True)
    facts, unread, _ = _store_source(runtime, tmp_path).read_build(
        [ModelMcrChange(repo="overlays", number=1, merge_sha="c" * 40)]
    )
    assert facts == ()
    assert unread == ("overlays",)
