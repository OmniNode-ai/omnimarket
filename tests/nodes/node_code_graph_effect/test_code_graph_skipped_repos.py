# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A listed repository the host holds no clone of is skipped and named; an unreadable one still fails.

A host reads the repositories it holds. The overlay lists every registry repository, so a host
without a clone of one reads the others and says which it skipped, as a ``repo-skipped`` reason.
Only the absent clone is skipped: a clone that cannot be read (a bad ref) fails the run, a run that
reads no repository fails, and a skipped repository that the frozen sample names is never a quiet pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_code_graph import ModelCodeGraphCommand
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
    events_of,
    load_topics,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    ModelCodeGraphOverlay,
    ModelGraphControl,
    ModelGraphSample,
    ModelGraphSampleEntry,
)
from omnimarket.nodes.node_code_graph_effect.protocols import ProtocolRepoSource
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    HOST,
    RUN_AT,
    overlay,
    source,
)

pytestmark = pytest.mark.unit

GONE = "example_gone"


def _repos(*names: str, ref: str = "origin/main") -> list[dict[str, str]]:
    return [{"name": name, "ref": ref} for name in names]


def _handler(
    git: ProtocolRepoSource,
    ledger: InMemoryLedger | None,
    document: ModelCodeGraphOverlay,
) -> CodeGraphRun:
    return CodeGraphRun(
        overlay=document,
        host=HOST,
        source=git,
        appender=ledger,
        clock=lambda: RUN_AT,
    )


def _core_only_sample(*, with_gone_entry: bool) -> dict[str, object]:
    """A sample that names ``example_core`` only, and a ``GONE`` entry when asked."""
    entries = [
        ModelGraphSampleEntry(
            repo="example_core",
            path="src/example_core/models.py",
            expected="used",
            basis="example",
        ),
        ModelGraphSampleEntry(
            repo="example_core",
            path="src/example_core/unused.py",
            expected="unused",
            basis="example",
        ),
    ]
    if with_gone_entry:
        entries.append(
            ModelGraphSampleEntry(
                repo=GONE, path="src/gone/a.py", expected="used", basis="example"
            )
        )
    frozen = ModelGraphSample.freeze(
        entries=tuple(entries),
        min_agree=2,
        controls=(
            ModelGraphControl(
                repo="example_core",
                path="src/example_core/models.py",
                why="the example script's import",
            ),
        ),
    )
    return frozen.model_dump(mode="json")


def test_a_listed_repository_with_no_clone_is_skipped_and_named(
    tmp_path: Path,
) -> None:
    git = source("git", tmp_path)
    ledger = InMemoryLedger()
    document = overlay(
        repos=_repos("example_core", "example_app", GONE),
    )
    result = _handler(git, ledger, document).handle(
        ModelCodeGraphCommand(correlation_id="skip")
    )
    assert result.status == "completed", result.failure
    assert result.verdict is EnumReadingVerdict.WORKING, result.reasons
    assert [repo.repo for repo in result.repos] == ["example_core", "example_app"]
    assert result.reasons == ("ok", f"repo-skipped:{GONE}:no-clone")
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["repos"] == "2"
    assert row.cells["skipped"] == GONE
    assert f"repo-skipped:{GONE}:no-clone" in row.cells["reason"]
    topics = load_topics()
    events = dict(events_of(result, topics))
    completed = events[topics["code_graph_run_completed"]]
    assert completed["reasons"] == ["ok", f"repo-skipped:{GONE}:no-clone"]


def test_a_run_that_skips_nothing_says_so(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    result = _handler(source("git", tmp_path), ledger, overlay()).handle(
        ModelCodeGraphCommand(correlation_id="none")
    )
    assert result.reasons == ("ok",)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["skipped"] == "none"


def test_a_clone_with_a_bad_ref_still_fails_the_run(tmp_path: Path) -> None:
    git = source("git", tmp_path)
    document = overlay(
        repos=[
            {"name": "example_core", "ref": "origin/main"},
            {"name": "example_app", "ref": "origin/no-such-branch"},
            {"name": GONE, "ref": "origin/main"},
        ],
    )
    result = _handler(git, None, document).handle(
        ModelCodeGraphCommand(correlation_id="bad-ref", write=False)
    )
    assert result.status == "failed"
    assert result.outcome.value == "source-unreadable"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.failure is not None
    assert result.failure.startswith("repo-unreadable:example-app-git-rev-parse")


def test_a_run_that_reads_no_repository_fails(tmp_path: Path) -> None:
    git = source("git", tmp_path)
    only_absent = ModelCodeGraphOverlay.model_validate(
        {
            **overlay().model_dump(mode="json"),
            "repos": _repos("absent_one", "absent_two"),
            "sample": ModelGraphSample.freeze(
                entries=(
                    ModelGraphSampleEntry(
                        repo="absent_one",
                        path="src/a.py",
                        expected="used",
                        basis="example",
                    ),
                ),
                min_agree=1,
                controls=(
                    ModelGraphControl(
                        repo="absent_one", path="src/a.py", why="example"
                    ),
                ),
            ).model_dump(mode="json"),
            "allowlist": [],
            "shadow": None,
        }
    )
    ledger = InMemoryLedger()
    result = _handler(git, ledger, only_absent).handle(
        ModelCodeGraphCommand(correlation_id="none-read")
    )
    assert result.status == "failed"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome.value == "source-unreadable"
    assert result.failure == "repo-unreadable:no-listed-repository-has-a-clone"
    assert result.repos == ()
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["skipped"] == "absent_one,absent_two"


def test_a_skipped_repository_the_sample_names_is_not_a_quiet_pass(
    tmp_path: Path,
) -> None:
    git = source("git", tmp_path)
    document = overlay(
        repos=_repos("example_core", GONE),
        sample=_core_only_sample(with_gone_entry=True),
        allowlist=[],
        shadow=None,
    )
    result = _handler(git, None, document).handle(
        ModelCodeGraphCommand(correlation_id="sample", write=False)
    )
    assert result.status == "completed", result.failure
    assert result.sample is not None
    # the bar and the control are met by the repository that was read: only the typed reason says no
    assert result.sample.agree >= document.sample.min_agree
    assert result.sample.controls_reached == result.sample.controls_total
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert f"sample-repo-skipped:{GONE}:1-entries" in result.reasons
    assert f"repo-skipped:{GONE}:no-clone" in result.reasons
    assert "ok" not in result.reasons


def test_a_skipped_repository_the_sample_does_not_name_leaves_the_verdict(
    tmp_path: Path,
) -> None:
    git = source("git", tmp_path)
    document = overlay(
        repos=_repos("example_core", "example_app", GONE),
    )
    result = _handler(git, None, document).handle(
        ModelCodeGraphCommand(correlation_id="unnamed", write=False)
    )
    assert result.verdict is EnumReadingVerdict.WORKING
    assert not any(
        reason.startswith("sample-repo-skipped") for reason in result.reasons
    )


def test_an_in_memory_repository_that_is_missing_stays_unreadable() -> None:
    """The in-memory backend has no clones: a missing repository is an error, not an absent clone."""
    repos = InMemoryRepoSource({})
    result = _handler(repos, None, overlay()).handle(
        ModelCodeGraphCommand(correlation_id="mem", write=False)
    )
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.startswith("repo-unreadable:example-core-no-repository")
