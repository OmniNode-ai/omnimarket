# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The must-fail control for a test-run evidence item (OMN-20032, task GC.9).

A green ``test_passes`` check is a statement about the head. It becomes a
statement about the change only when the same tests are shown to fail on the
code as it was before the change. This module runs that comparison for the
diff-derived behaviour proof (OMN-17943): it takes the test files the PR
changed, re-runs each one at the pre-change commit with the PR's own test-side
files laid over it, digests each run, and grades the whole with the same compute
the delegated test loop grades its generated tests with (OMN-19361).

Reuse, not a second implementation:

* the run digest is ``digest_pytest_run`` (``node_pytest_failure_digest_compute``);
* the grade is ``grade_control`` (``node_delegated_test_control_compute``), so
  an assertion-level failure is headline, a collection-only failure is
  accepted-weak, and a pass at both is ``control_did_not_fail``.

Nothing here decides a verdict. :func:`evaluate_must_fail_control` returns a
record; the collector decides what the record does to the item. The record can
only take a green away (vacuous, or a control that could not run). It never
makes an item pass.

Impossible cases are named, not implied. A docs-only change, a change with no
test file, a change that touched nothing but tests, a root commit, an item that
declares itself a pure refactor, and an item that is not derived from a PR diff
each return ``IMPOSSIBLE`` with a reason a reader can act on.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.delegated_test_loop.must_fail_models import (
    MAX_REASON_CHARS,
    EnumMustFailControlOutcome,
    EnumMustFailImpossibleReason,
    ModelMustFailControl,
    ModelMustFailTestRun,
    ModelPrChangedFile,
    ModelPrDiffFacts,
)
from omnimarket.nodes.node_delegated_test_control_compute import (
    EnumControlStatus,
    ModelControlGradeRequest,
    grade_control,
)
from omnimarket.nodes.node_pytest_failure_digest_compute import (
    ModelPytestRunReport,
    digest_pytest_run,
)

#: The id prefix every diff-derived behaviour-proof item carries (OMN-16434,
#: PR-scoped by OMN-18856). Pinned against the producer's constant in a test.
DIFF_DERIVED_ID_PREFIX = "dod-occ-diff-derived-behavior-proof"

_BINDING_RE = re.compile(
    r"^PR #(?P<pr>\d+) on (?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    r"— diff-derived behavior proof"
)
_WAIVER_RE = re.compile(
    r"^must-fail-control: impossible \((?P<kind>pure-refactor|docs|test-only)\): "
    r"(?P<reason>\S.{11,})$",
    re.MULTILINE,
)
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_DOC_DIRS = ("docs/", "doc/")
_DOC_BASENAMES = frozenset({"LICENSE", "NOTICE", "CODEOWNERS", "CHANGELOG"})
_TEST_DIRS = ("tests/", "test/")
_TEST_BASENAMES = frozenset({"conftest.py"})
_PRESENT_STATUSES = frozenset({"added", "modified", "renamed", "copied", "changed"})
_LOSSY_STATUSES = frozenset({"removed"})

#: The rank of a run outcome as evidence the change is what made the tests pass.
_STRONGEST_FIRST = (
    "failed_call",
    "failed_collection",
    "error_setup",
    "infra_error",
    "no_tests",
    "passed",
)


class ModelMustFailRunRequest(BaseModel):
    """One test file to run at the pre-change commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_dir: Path
    pre_change_sha: str
    change_sha: str
    test_path: str
    overlay_paths: tuple[str, ...] = Field(
        default=(),
        description="Test-side files the PR added or changed, laid over the "
        "pre-change tree from the change commit so the tests can run there.",
    )
    timeout_seconds: int = Field(..., ge=1)


class ModelMustFailRunResult(BaseModel):
    """What one run produced. ``exit_code`` is None when pytest never ran."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    junit_xml: str = ""
    exit_code: int | None = None
    detail: str = Field(default="", max_length=MAX_REASON_CHARS)


class ProtocolMustFailTreeRunner(Protocol):
    """Runs one test file against the tree as it was before the change."""

    def handle(self, request: ModelMustFailRunRequest) -> ModelMustFailRunResult: ...


def diff_derived_binding(item: Mapping[str, Any]) -> tuple[str, int] | None:
    """``(repo, pr_number)`` of a diff-derived behaviour-proof item, else None.

    The item's id names its kind and its description, which the producer
    renders from one template, names the PR. An item with the id and no
    parseable description is not bound, and is treated as not diff-derived.
    """
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id.startswith(DIFF_DERIVED_ID_PREFIX):
        return None
    description = item.get("description")
    if not isinstance(description, str):
        return None
    match = _BINDING_RE.match(description.strip())
    if match is None:
        return None
    return match.group("repo"), int(match.group("pr"))


def pytest_targets(command: str) -> tuple[str, ...] | None:
    """The test files of a ``uv run pytest a.py b.py -q`` command, else None.

    Only the shape the producer mints is accepted: an optional ``uv run``, the
    word ``pytest``, ``-q`` and ``.py`` paths. Anything else has no list of
    files to re-run against the earlier code, and returns None.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if not tokens or tokens[0] != "pytest":
        return None
    paths: list[str] = []
    for token in tokens[1:]:
        if token == "-q":
            continue
        if token.startswith("-") or not token.endswith(".py") or "::" in token:
            return None
        if token.startswith("/") or ".." in Path(token).parts:
            return None
        paths.append(token)
    return tuple(paths) if paths else None


def declared_impossible(description: str) -> tuple[str, str] | None:
    """``(kind, reason)`` when the item declares its control impossible.

    The declaration is a line of the item's description::

        must-fail-control: impossible (pure-refactor): <reason of 12+ characters>

    The reason is required and travels to the receipt. A declaration with no
    reason does not match, so it declares nothing.
    """
    match = _WAIVER_RE.search(description)
    if match is None:
        return None
    return match.group("kind"), match.group("reason").strip()


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_doc_path(path: str) -> bool:
    return (
        path.endswith(_DOC_SUFFIXES)
        or path.startswith(_DOC_DIRS)
        or _basename(path) in _DOC_BASENAMES
    )


def is_test_side_path(path: str) -> bool:
    """A test, a conftest, or a fixture under a test directory."""
    name = _basename(path)
    return (
        path.startswith(_TEST_DIRS)
        or "/tests/" in path
        or name in _TEST_BASENAMES
        or (
            name.endswith(".py")
            and (name.startswith("test_") or name.endswith("_test.py"))
        )
    )


def aggregate_outcome(outcomes: Sequence[str]) -> str:
    """The strongest evidence across several test files' outcomes.

    One file failing at assertion level is a discriminating test whatever the
    others did, so it outranks everything. Then a collection or setup failure,
    then an infrastructure fault (a run that never spoke is not a pass), then a
    pass.
    """
    for candidate in _STRONGEST_FIRST:
        if candidate in outcomes:
            return candidate
    return "infra_error"


def _impossible(
    reason: EnumMustFailImpossibleReason,
    text: str,
    *,
    route: Literal["control", "shell"] = "control",
    facts: ModelPrDiffFacts | None = None,
) -> ModelMustFailControl:
    return ModelMustFailControl(
        route=route,
        outcome=EnumMustFailControlOutcome.IMPOSSIBLE,
        impossible_reason=reason,
        reason=text[:MAX_REASON_CHARS],
        repo=facts.repo if facts else "",
        pr_number=facts.pr_number if facts else None,
        pre_change_sha=facts.parent_commit_sha if facts else "",
        change_sha=facts.merge_commit_sha if facts else "",
    )


def shell_route_record(reason: str) -> ModelMustFailControl:
    """The record of an item that ran by the shell route only.

    It says so, and it is never labelled controlled: the outcome is
    ``IMPOSSIBLE`` and the route is ``shell``.
    """
    return _impossible(
        EnumMustFailImpossibleReason.NOT_DIFF_DERIVED, reason, route="shell"
    )


def _unavailable(text: str, facts: ModelPrDiffFacts | None) -> ModelMustFailControl:
    return ModelMustFailControl(
        route="control",
        outcome=EnumMustFailControlOutcome.UNAVAILABLE,
        reason=text[:MAX_REASON_CHARS],
        repo=facts.repo if facts else "",
        pr_number=facts.pr_number if facts else None,
        pre_change_sha=facts.parent_commit_sha if facts else "",
        change_sha=facts.merge_commit_sha if facts else "",
    )


def evaluate_must_fail_control(
    *,
    item: Mapping[str, Any],
    command: str,
    facts: ModelPrDiffFacts | None,
    runner: ProtocolMustFailTreeRunner | None,
    repo_dir: Path | None,
    timeout_seconds: int,
) -> ModelMustFailControl:
    """Run the control for one diff-derived item whose check passed at the head.

    Call this only for a check that already passed at the head: "passes at the
    head" is the collector's own result, and a control on a red check is noise.
    """
    description = item.get("description")
    if isinstance(description, str):
        declared = declared_impossible(description)
        if declared is not None:
            kind, why = declared
            return _impossible(
                EnumMustFailImpossibleReason.DECLARED_BY_ITEM,
                f"the item declares its control impossible ({kind}): {why}",
                facts=facts,
            )

    targets = pytest_targets(command)
    if targets is None:
        return _impossible(
            EnumMustFailImpossibleReason.COMMAND_NOT_A_TEST_FILE_LIST,
            "the check is not `pytest <files>.py -q`, so there is no list of "
            "changed tests to re-run against the earlier code",
            facts=facts,
        )
    if facts is None:
        return _unavailable(
            "the PR's changed files and merge commit could not be read", None
        )
    if not facts.merge_commit_sha:
        return _unavailable(
            "the PR is not merged, so it has no pre-change commit to compare with",
            facts,
        )
    files = facts.changed_files
    if not files:
        return _unavailable("GitHub returned no changed files for the PR", facts)
    if all(is_doc_path(f.path) for f in files):
        return _impossible(
            EnumMustFailImpossibleReason.DOCS_ONLY_DIFF,
            "the PR changed documentation only, so no test can fail before it",
            facts=facts,
        )
    present = {f.path for f in files if f.status in _PRESENT_STATUSES}
    changed_targets = tuple(t for t in targets if t in present)
    if not changed_targets:
        return _impossible(
            EnumMustFailImpossibleReason.NO_TEST_FILE_IN_DIFF,
            "none of the check's test files was added or changed by the PR, so "
            "they exercised the earlier code as they exercise this",
            facts=facts,
        )
    source_changed = [
        f
        for f in files
        if f.status not in _LOSSY_STATUSES
        and not is_test_side_path(f.path)
        and not is_doc_path(f.path)
    ]
    removed_source = [
        f
        for f in files
        if f.status in _LOSSY_STATUSES
        and not is_test_side_path(f.path)
        and not is_doc_path(f.path)
    ]
    if not source_changed and not removed_source:
        return _impossible(
            EnumMustFailImpossibleReason.TEST_ONLY_DIFF,
            "the PR changed tests and nothing else, so there is no earlier "
            "behaviour for them to fail against",
            facts=facts,
        )
    if not facts.parent_commit_sha:
        return _impossible(
            EnumMustFailImpossibleReason.NO_PRE_CHANGE_COMMIT,
            "the merge commit has no parent, so there is no earlier code",
            facts=facts,
        )
    if runner is None or repo_dir is None:
        return _unavailable(
            "no runner or product clone is available to run the earlier code", facts
        )

    overlay = tuple(
        sorted(
            f.path
            for f in files
            if f.status in _PRESENT_STATUSES and is_test_side_path(f.path)
        )
    )
    runs: list[ModelMustFailTestRun] = []
    for test_path in changed_targets:
        result = runner.handle(
            ModelMustFailRunRequest(
                repo_dir=repo_dir,
                pre_change_sha=facts.parent_commit_sha,
                change_sha=facts.merge_commit_sha,
                test_path=test_path,
                overlay_paths=overlay,
                timeout_seconds=timeout_seconds,
            )
        )
        digest = digest_pytest_run(
            ModelPytestRunReport(junit_xml=result.junit_xml, exit_code=result.exit_code)
        )
        runs.append(ModelMustFailTestRun(path=test_path, outcome=digest.outcome.value))

    outcome = aggregate_outcome([r.outcome for r in runs])
    grade = grade_control(
        ModelControlGradeRequest(
            fixed_outcome="passed",
            prefix_outcome=outcome,
            mutation_outcome=None,
            mutation_requested=False,
            prefix_ref_equals_fixed_ref=facts.parent_commit_sha
            == facts.merge_commit_sha,
        )
    )
    mapped: dict[EnumControlStatus, EnumMustFailControlOutcome] = {
        EnumControlStatus.ACCEPTED_CALL: EnumMustFailControlOutcome.CONTROLLED,
        EnumControlStatus.ACCEPTED_MUTATION: EnumMustFailControlOutcome.CONTROLLED,
        EnumControlStatus.ACCEPTED_COLLECTION: EnumMustFailControlOutcome.CONTROLLED_WEAK,
        EnumControlStatus.CONTROL_DID_NOT_FAIL: EnumMustFailControlOutcome.VACUOUS,
        EnumControlStatus.NEEDS_MUTATION_CONTROL: EnumMustFailControlOutcome.UNAVAILABLE,
        EnumControlStatus.INFRA_ERROR: EnumMustFailControlOutcome.UNAVAILABLE,
    }
    return ModelMustFailControl(
        route="control",
        outcome=mapped[grade.status],
        headline=grade.headline,
        grade_status=grade.status.value,
        reason=grade.reason[:MAX_REASON_CHARS],
        repo=facts.repo,
        pr_number=facts.pr_number,
        pre_change_sha=facts.parent_commit_sha,
        change_sha=facts.merge_commit_sha,
        runs=tuple(runs),
    )


#: Provided by the collector: the interpreter of the check's own lock-exact
#: environment for a project root, or the reason there is none.
PythonProvider = Callable[[Path], tuple[Path | None, str | None]]


__all__ = [
    "DIFF_DERIVED_ID_PREFIX",
    "ModelMustFailRunRequest",
    "ModelMustFailRunResult",
    "ModelPrChangedFile",
    "ProtocolMustFailTreeRunner",
    "PythonProvider",
    "aggregate_outcome",
    "declared_impossible",
    "diff_derived_binding",
    "evaluate_must_fail_control",
    "is_doc_path",
    "is_test_side_path",
    "pytest_targets",
    "shell_route_record",
]
