# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drift read judges overlays through omnimarket at the revision the runtime says it runs (OMN-22787).

The git backend runs a real repository with two commits whose model differs, so the same overlay
text is accepted at one revision and refused at the other. The in-memory backend answers the same
questions from given trees. The chain event tests publish the command and assert every event, a
golden chain and one error chain per typed failure, and the last tests turn red on a named break.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_overlay_drift_effect.adapters.overlay_home import (
    InMemoryOverlayHome,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_revision import (
    GitRevisionTree,
    InMemoryRevisionTree,
    InMemoryRuntimeRevision,
    ProvenanceRuntimeRevision,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_validator import (
    InterpreterRuntimeValidator,
    RevisionTreeRuntimeValidator,
    Runner,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.handler_overlay_drift import (
    HandlerOverlayDrift,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayDriftCommand,
    ModelOverlayDriftResult,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelRuntimeRevisionSource,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import (
    ProtocolRuntimeValidator,
    RuntimeRevisionError,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_overlay_drift_effect.overlay_drift_support import (
    HOST,
    RUN_AT,
    SRC,
    World,
    _this_checkout,
    overlay,
)
from tests.nodes.node_overlay_drift_effect.test_overlay_drift_chain_events import (
    CORRELATION,
    PAYLOAD,
    _publish,
    assert_failed_chain,
    assert_golden_chain,
)

pytestmark = pytest.mark.unit

OVERLAY_ID = "node_fake_effect"
OVERLAY_TEXT = "alpha: 1\n"
PIN = f"{OVERLAY_ID} 0123456789abcdef omnimarket.fake_models:ModelFake"
#: The model the runtime built at the first revision accepts ``alpha``; the second revision's
#: model renames it to ``beta``, so the same overlay text is refused there.
MODEL_AT_FIRST = "from pydantic import BaseModel, ConfigDict\n\n\nclass ModelFake(BaseModel):\n    model_config = ConfigDict(extra='forbid')\n    alpha: int\n"
MODEL_AT_SECOND = MODEL_AT_FIRST.replace("alpha", "beta")


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(clone),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
    )
    return done.stdout.strip()


def _commit_model(clone: Path, model: str) -> str:
    package = clone / "src/omnimarket"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "fake_models.py").write_text(model, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "model")
    return _git(clone, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with two commits; returns (clone, first revision, second revision)."""
    repo = tmp_path / "omnimarket-clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    first = _commit_model(repo, MODEL_AT_FIRST)
    second = _commit_model(repo, MODEL_AT_SECOND)
    return repo, first, second


def _handler(
    validator: ProtocolRuntimeValidator, ledger: InMemoryLedger | None
) -> HandlerOverlayDrift:
    return HandlerOverlayDrift(
        overlay=overlay(),
        host=HOST,
        home=InMemoryOverlayHome({OVERLAY_ID: OVERLAY_TEXT}, f"# pins\n{PIN}\n"),
        validator=validator,
        appender=ledger,
        clock=lambda: RUN_AT,
    )


def _tree_validator(
    revision: InMemoryRuntimeRevision, tree: GitRevisionTree | InMemoryRevisionTree
) -> RevisionTreeRuntimeValidator:
    return RevisionTreeRuntimeValidator(
        InterpreterRuntimeValidator(Path(sys.executable), 120), revision, tree
    )


def _read(validator: ProtocolRuntimeValidator) -> ModelOverlayDriftResult:
    return _handler(validator, None).handle(
        ModelOverlayDriftCommand(correlation_id="t", write=False)
    )


# --- the revision decides which models judge ---------------------------------------------


def test_git_backend_the_same_overlay_is_accepted_at_one_revision_and_refused_at_the_other(
    clone: tuple[Path, str, str],
) -> None:
    repo, first, second = clone
    tree = GitRevisionTree(repo, 30)
    at_first = _read(_tree_validator(InMemoryRuntimeRevision(first), tree))
    at_second = _read(_tree_validator(InMemoryRuntimeRevision(second), tree))
    assert at_first.verdict.value == "WORKING"
    assert at_first.counts.accepted == 1
    assert at_first.runtime is not None
    assert at_first.runtime.revision == first
    assert at_first.runtime.version == "source-tree"
    assert at_second.verdict.value == "NOT_WORKING"
    assert at_second.counts.rejected == 1
    assert at_second.failing[0].fields == ("alpha", "beta")
    assert at_second.runtime is not None
    assert at_second.runtime.revision == second


def test_memory_backend_answers_the_same_from_given_trees(tmp_path: Path) -> None:
    trees = {}
    for name, model in (("first", MODEL_AT_FIRST), ("second", MODEL_AT_SECOND)):
        package = tmp_path / name / "omnimarket"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "fake_models.py").write_text(model, encoding="utf-8")
        trees[name] = tmp_path / name
    tree = InMemoryRevisionTree(trees)
    assert (
        _read(_tree_validator(InMemoryRuntimeRevision("first"), tree)).counts.accepted
        == 1
    )
    assert (
        _read(_tree_validator(InMemoryRuntimeRevision("second"), tree)).counts.rejected
        == 1
    )


def test_the_installed_interpreter_models_are_not_what_judged(
    clone: tuple[Path, str, str],
) -> None:
    """Without the setting the interpreter's own models judge: this overlay's fake model is missing."""
    plain = _read(InterpreterRuntimeValidator(Path(sys.executable), 120))
    assert plain.counts.model_missing == 1
    assert plain.counts.accepted == 0


# --- the revision cannot be read or exported: UNKNOWN with a typed reason ----------------


def test_unreadable_provenance_gives_unknown_and_never_a_guess(
    clone: tuple[Path, str, str],
) -> None:
    repo, _, _ = clone
    result = _read(
        _tree_validator(InMemoryRuntimeRevision(None), GitRevisionTree(repo, 30))
    )
    assert result.verdict.value == "UNKNOWN"
    assert result.reasons == ("runtime-unreadable-revision-provenance-unreadable",)
    assert result.runtime is None


def test_a_revision_the_clone_lacks_gives_unknown(
    clone: tuple[Path, str, str],
) -> None:
    repo, _, _ = clone
    result = _read(
        _tree_validator(InMemoryRuntimeRevision("f" * 40), GitRevisionTree(repo, 30))
    )
    assert result.verdict.value == "UNKNOWN"
    assert result.reasons == ("runtime-unreadable-revision-revision-not-in-clone",)


def test_a_tree_that_is_not_first_on_the_import_path_stops_the_read(
    tmp_path: Path,
) -> None:
    """A source directory without omnimarket cannot shadow the installed one: the program refuses."""
    empty = tmp_path / "empty-src"
    empty.mkdir()
    result = _read(
        _tree_validator(
            InMemoryRuntimeRevision("0123456789ab"),
            InMemoryRevisionTree({"0123456789ab": empty}),
        )
    )
    assert result.verdict.value == "UNKNOWN"
    assert result.reasons == ("runtime-unreadable-runtime-exit-3",)


def test_git_tree_refuses_a_revision_that_is_not_a_commit_id(
    clone: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, _ = clone
    with pytest.raises(RuntimeRevisionError, match="revision-is-not-a-commit"):
        GitRevisionTree(repo, 30).materialize("--output=/x", tmp_path)
    with pytest.raises(RuntimeRevisionError, match="clone-absent"):
        GitRevisionTree(tmp_path / "none", 30).materialize("a" * 40, tmp_path)


# --- the provenance read -----------------------------------------------------------------

SOURCE = ModelRuntimeRevisionSource.model_validate(
    overlay().runtime.revision_source.model_dump()  # type: ignore[union-attr]
)
REV = "fd073f933fcf0873c9a5e5496d8ead529ec9abae"


class _Run:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[Sequence[str], bytes | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, stdin))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout, b""
        )


def _provenance(**override: object) -> bytes:
    document: dict[str, object] = {
        "per_repo_vcs_provenance": {"siblings": {"omnimarket": {"vcs_ref": REV}}}
    }
    document.update(override)
    return json.dumps(document).encode()


def test_provenance_read_takes_the_revision_at_the_declared_path() -> None:
    run = _Run(_provenance())
    revision = ProvenanceRuntimeRevision(SOURCE, {SOURCE.ssh_host_env: "host"}, run)
    assert revision.read() == REV
    argv, stdin = run.calls[0]
    assert "host" in argv
    assert stdin is not None
    assert SOURCE.compose_project.encode() in stdin
    assert SOURCE.provenance_path.encode() in stdin


@pytest.mark.parametrize(
    ("run", "environ", "reason"),
    [
        (_Run(b"", 255), {SOURCE.ssh_host_env: "h"}, "provenance-read-exit-255"),
        (_Run(b"not json"), {SOURCE.ssh_host_env: "h"}, "provenance-not-json"),
        (_Run(b"[]"), {SOURCE.ssh_host_env: "h"}, "provenance-not-an-object"),
        (
            _Run(_provenance(per_repo_vcs_provenance={})),
            {SOURCE.ssh_host_env: "h"},
            "provenance-has-no-ref-at-path",
        ),
        (
            _Run(
                _provenance(
                    per_repo_vcs_provenance={
                        "siblings": {"omnimarket": {"vcs_ref": "unknown"}}
                    }
                )
            ),
            {SOURCE.ssh_host_env: "h"},
            "provenance-ref-is-not-a-commit",
        ),
        (_Run(_provenance()), {}, "ssh-host-variable-unset"),
    ],
)
def test_provenance_read_names_why_it_could_not_say(
    run: _Run, environ: dict[str, str], reason: str
) -> None:
    with pytest.raises(RuntimeRevisionError, match=reason):
        ProvenanceRuntimeRevision(SOURCE, environ, run).read()


# --- chain events ------------------------------------------------------------------------


class _RevisionWorld(World):
    """The standard world, judged through this checkout's own tree as the runtime's revision."""

    def __init__(
        self, base: Path, revision: InMemoryRuntimeRevision, trees: dict[str, Path]
    ) -> None:
        super().__init__("memory", base)
        self._revision = revision
        self._trees = trees

    def validator(self) -> ProtocolRuntimeValidator:
        return _tree_validator(self._revision, InMemoryRevisionTree(self._trees))


def test_golden_chain_with_the_revision_setting_writes_the_runtime_revision(
    tmp_path: Path,
) -> None:
    world = _RevisionWorld(tmp_path, InMemoryRuntimeRevision(REV), {REV: SRC})
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_golden_chain(events)
    assert events[0][1]["correlation_id"] == CORRELATION
    row = ledger.lines[0]
    assert f"runtime_revision={REV[:12]}" in row
    assert "runtime_version=source-tree" in row
    assert "verdict=WORKING" in row


def test_error_chain_unreadable_provenance_is_a_failure_event_and_an_unknown_row(
    tmp_path: Path,
) -> None:
    world = _RevisionWorld(tmp_path, InMemoryRuntimeRevision(None), {REV: SRC})
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "reason=runtime-unreadable-revision-provenance-unreadable" in ledger.lines[0]
    assert "runtime_revision=unknown" in ledger.lines[0]


def test_error_chain_a_revision_with_no_tree_is_a_failure_event(
    tmp_path: Path,
) -> None:
    world = _RevisionWorld(tmp_path, InMemoryRuntimeRevision(REV), {})
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "reason=runtime-unreadable-revision-revision-not-in-clone" in ledger.lines[0]


def test_named_break_ignoring_the_setting_turns_the_revision_assertion_red(
    tmp_path: Path,
) -> None:
    """The break: the setting is ignored and the interpreter's own models judge. The golden
    chain's runtime_revision assertion must fail, so the chain test is not decorative."""

    class IgnoresTheSetting(_RevisionWorld):
        def validator(self) -> ProtocolRuntimeValidator:
            return InterpreterRuntimeValidator(
                Path(sys.executable), 120, run=_this_checkout_runner()
            )

    world = IgnoresTheSetting(tmp_path, InMemoryRuntimeRevision(REV), {REV: SRC})
    ledger = InMemoryLedger()
    asyncio.run(_publish(world, ledger, PAYLOAD))
    assert f"runtime_revision={REV[:12]}" not in ledger.lines[0]


def _this_checkout_runner() -> Runner:
    return _this_checkout
