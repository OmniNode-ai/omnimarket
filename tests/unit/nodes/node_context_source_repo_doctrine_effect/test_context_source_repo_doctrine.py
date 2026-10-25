# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository doctrine sources and their contract binding (OMN-21090)."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import os
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.nodes.node_context_pack_builder_compute.parsers.parser_guidance_section import (
    GuidanceSectionParser,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_source_repo_doctrine_effect import (
    HandlerContextSourceRepoDoctrine,
    NodeContextSourceRepoDoctrineEffect,
)
from omnimarket.nodes.node_context_source_repo_doctrine_effect.handlers.handler_context_source_repo_doctrine import (
    load_repo_doctrine_binding,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[4]
NODE = "node_context_source_repo_doctrine_effect"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
F = EnumContextFactor
S = EnumContextSourceStatus
STAMP = "2026-10-19T08:00:00Z"
GUIDE = """# Repository

## Repo Invariants (required)

Keep topics in contracts.
Keep this entire paragraph, too.

## Common Commands

Not doctrine.

## Repository Invariants

Handlers never publish.

## Adding A Node

Declare its handler.

## Development Rules

Use typed models.

## Architecture: Four-Node Pattern

Separate the four roles.

## Declarative Nodes

Keep configuration in contracts.

## Handler No-Publish Constraint

Return intents.

## Pydantic Model Standards

Freeze boundary models.
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "root"
    repo = workspace / "omnimarket"
    repo.mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(GUIDE, encoding="utf-8")
    return workspace


def _read(
    root: Path | None,
    factor: EnumContextFactor,
    *,
    repository: str | None = "omnimarket",
    lane_kind: str | None = None,
    revision: str = STAMP,
    ledger: Path | None = None,
    contract_path: Path | None = None,
) -> ModelContextSourceResult:
    env = {} if root is None else {"OMNI_HOME": str(root)}
    if ledger is not None:
        env["ONEX_LEDGER_PATH"] = str(ledger)
    result = HandlerContextSourceRepoDoctrine(
        env=env, contract_path=contract_path
    ).handle(
        ModelContextSourceRequest(
            factor=factor,
            caller_scope=ModelContextCallerScope(
                repository=repository, lane_kind=lane_kind
            ),
            access=ModelContextAccess(tenant_id="lab"),
            revision=revision,
        )
    )
    for entry in result.entries:
        assert entry.factor is factor
        assert entry.status is S.RESOLVED
        assert entry.span is not None
        assert entry.span.unit is EnumContextSpanUnit.CHARACTER
        assert (entry.span.start, entry.span.end) == (0, len(entry.text))
        assert (
            entry.text_sha256 == hashlib.sha256(entry.text.encode("utf-8")).hexdigest()
        )
    return result


def _sections(*headings: str) -> list[str]:
    return [
        s.content
        for s in GuidanceSectionParser().parse(
            GUIDE, source_file="omnimarket/CLAUDE.md"
        )
        if s.heading_path[-1].startswith(headings)
    ]


def _registry(root: Path) -> Path:
    path = (
        root
        / "omnimarket/src/omnimarket/nodes/node_architectural_invariant_loop/invariants"
    )
    path.mkdir(parents=True)
    return path


def _ledger(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    (tmp_path / "archive").mkdir(exist_ok=True)
    return path


def test_invariants_carry_repo_invariant_sections_whole(root: Path) -> None:
    result = _read(root, F.INVARIANTS)
    assert result.status is S.RESOLVED
    assert [e.text for e in result.entries] == _sections(
        "Repo Invariants", "Repository Invariants"
    )
    assert [e.source_ref for e in result.entries] == [
        "omnimarket/CLAUDE.md#Repo Invariants (required)",
        "omnimarket/CLAUDE.md#Repository Invariants",
    ]
    assert all(e.verbatim_verified and not e.paraphrase for e in result.entries)
    assert {e.as_of for e in result.entries} == {datetime(2026, 10, 19, 8, tzinfo=UTC)}


def test_invariants_carry_the_architectural_registry(root: Path) -> None:
    registry = _registry(root)
    for code in ("ARCH-002", "ARCH-001"):
        (registry / f"{code}.yaml").write_text(
            f"principle_code: {code}\nname: typed_contracts\nseverity: ERROR\ndescription: >\n  Keep contracts typed\n  across boundaries.\n",
            encoding="utf-8",
        )
    (registry / "ARCH-003.yaml").write_text(
        "description: missing code", encoding="utf-8"
    )
    (registry / "ARCH-004.yaml").write_text("[broken: yaml", encoding="utf-8")
    (registry / "ARCH-005.yaml").write_text(
        "principle_code: ARCH-005", encoding="utf-8"
    )
    result = _read(root, F.INVARIANTS)
    assert [e.text for e in result.entries[2:]] == [
        f"{code} typed_contracts (ERROR): Keep contracts typed across boundaries."
        for code in ("ARCH-001", "ARCH-002")
    ]
    assert result.entry_counts["registry_skipped"] == 3
    for entry, code in zip(result.entries[2:], ("ARCH-001", "ARCH-002"), strict=True):
        assert (
            entry.source_ref
            == f"omnimarket/src/omnimarket/nodes/node_architectural_invariant_loop/invariants/{code}.yaml#{code}"
        )
        assert entry.paraphrase
        assert not entry.verbatim_verified


def test_invariants_carry_rulings_that_name_the_repository(
    root: Path, tmp_path: Path
) -> None:
    ledger = _ledger(
        tmp_path,
        "2026-10-13T00:00:00Z | RULING | omnimarket: first",
        "2026-10-14T00:00:00Z | RULING | omnibase_infra: other repo",
        "2026-10-15T00:00:00Z | RULING | omnimarketx is a different token",
        "2026-10-15T01:00:00Z | RULING | x_omnimarket is different",
        "2026-10-15T02:00:00Z | RULING | omnimarket_suffix is different",
        "2026-10-15T03:00:00Z | RULING | x-omnimarket is different",
        "2026-10-16T00:00:00Z | RULING | (omnimarket): second",
        "2026-10-17T00:00:00Z | RULING | omnimarket: superseded tie",
        "2026-10-17T00:00:00Z | RULING | omnimarket: later tie",
    )
    archive = tmp_path / "archive/newest.md"
    archive.write_text(
        "2026-10-18T00:00:00Z | RULING | omnimarket: archive must not be read\n",
        encoding="utf-8",
    )
    result = _read(root, F.INVARIANTS, ledger=ledger)
    rulings = result.entries[2:]
    assert [e.text.rsplit(": ", 1)[-1] for e in rulings] == [
        "later tie",
        "superseded tie",
        "second",
    ]
    assert [e.source_ref for e in rulings] == [
        f"ledger#{e.text[:20]}@{hashlib.sha256(e.text.encode('utf-8')).hexdigest()[:12]}"
        for e in rulings
    ]
    assert len({e.source_ref for e in rulings}) == 3
    assert [e.source_ref.split("@")[0] for e in rulings] == [
        "ledger#2026-10-17T00:00:00Z",
        "ledger#2026-10-17T00:00:00Z",
        "ledger#2026-10-16T00:00:00Z",
    ]
    assert all(e.verbatim_verified and not e.paraphrase for e in rulings)
    assert rulings[0].as_of == datetime(2026, 10, 17, tzinfo=UTC)
    # Once live has fewer than the cap, archives are scanned in reverse name order.
    ledger.write_text(
        "2026-10-13T00:00:00Z | RULING | omnimarket: live\n", encoding="utf-8"
    )
    archive.write_text(
        "2026-10-16T00:00:00Z | RULING | omnimarket: archive first\n2026-10-15T00:00:00Z | RULING | omnimarket: archive second\n",
        encoding="utf-8",
    )
    (tmp_path / "archive/earlier.md").write_text(
        "2026-10-18T00:00:00Z | RULING | omnimarket: unread earlier archive\n",
        encoding="utf-8",
    )
    result = _read(root, F.INVARIANTS, ledger=ledger)
    assert [e.text.rsplit(": ", 1)[-1] for e in result.entries[2:]] == [
        "archive first",
        "archive second",
        "live",
    ]


def test_rulings_after_the_revision_stamp_are_ignored(
    root: Path, tmp_path: Path
) -> None:
    ledger = _ledger(
        tmp_path,
        "2026-10-18T00:00:00Z | RULING | omnimarket: before",
        "2026-10-19T08:00:00Z | RULING | omnimarket: equal",
        "2026-10-19T08:00:01Z | RULING | omnimarket: future",
    )
    result = _read(root, F.INVARIANTS, ledger=ledger)
    assert [e.text.rsplit(": ", 1)[-1] for e in result.entries[2:]] == [
        "equal",
        "before",
    ]


def test_canonical_shapes_are_the_named_shape_sections(root: Path) -> None:
    result = _read(root, F.CANONICAL_SHAPES)
    assert result.status is S.RESOLVED
    assert [e.text for e in result.entries] == _sections(
        "Adding A Node",
        "Development Rules",
        "Architecture: Four-Node Pattern",
        "Declarative Nodes",
        "Handler No-Publish Constraint",
        "Pydantic Model Standards",
    )
    assert all(e.verbatim_verified and not e.paraphrase for e in result.entries)


def _git(repo: Path, *args: str, stamp: str = "2026-10-15T12:00:00Z") -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        env={
            **scrub_git_location_env(os.environ),
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_root(root: Path) -> Path:
    repo = root / "omnimarket"
    _git(repo, "init")
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "CLAUDE.md", "x.py")
    _git(repo, "commit", "-m", "feat(OMN-1): add x (#12)")
    (repo / "x.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(
        repo, "commit", "-m", "fix(OMN-2): repair y (#13)", stamp="2026-10-16T12:00:00Z"
    )
    _git(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        "chore: no pr",
        stamp="2026-10-17T12:00:00Z",
    )
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return root


@pytest.mark.parametrize(
    ("lane_kind", "pr", "subject", "stamp", "files"),
    [
        (
            "design-build",
            12,
            "feat(OMN-1): add x (#12)",
            "2026-10-15T12:00:00+00:00",
            ["A\tCLAUDE.md", "A\tx.py"],
        ),
        (
            "red-test-fix",
            13,
            "fix(OMN-2): repair y (#13)",
            "2026-10-16T12:00:00+00:00",
            ["M\tx.py"],
        ),
    ],
)
def test_exemplar_is_the_newest_merged_pr_of_the_lane_kind(
    git_root: Path, lane_kind: str, pr: int, subject: str, stamp: str, files: list[str]
) -> None:
    sha = _git(git_root / "omnimarket", "rev-parse", "HEAD~2" if pr == 12 else "HEAD~1")
    result = _read(git_root, F.EXEMPLAR, lane_kind=lane_kind)
    assert result.status is S.RESOLVED
    (entry,) = result.entries
    assert (
        entry.text
        == f"{subject}\nmerged {stamp} as {sha[:12]} on origin/dev\nfiles ({len(files)}):\n"
        + "\n".join(files)
    )
    assert entry.source_ref == f"omnimarket#{pr}@{sha[:12]}"
    assert entry.paraphrase
    assert not entry.verbatim_verified
    assert entry.as_of == datetime(2026, 10, 19, 8, tzinfo=UTC)


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE"])
def test_exemplar_ignores_inherited_git_location_env(
    git_root: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, str(git_root / "bogus-git-location"))
    sha = _git(git_root / "omnimarket", "rev-parse", "HEAD~2")
    result = _read(git_root, F.EXEMPLAR, lane_kind="design-build")
    assert result.status is S.RESOLVED
    (entry,) = result.entries
    assert entry.source_ref == f"omnimarket#12@{sha[:12]}"
    assert "feat(OMN-1): add x (#12)" in entry.text


@pytest.mark.parametrize("lane_kind", [None, "unmapped"])
def test_exemplar_without_a_mapped_lane_kind_is_unresolved(
    root: Path, lane_kind: str | None
) -> None:
    assert _read(root, F.EXEMPLAR, lane_kind=lane_kind).status is S.UNRESOLVED


def test_exemplar_falls_back_to_main_ref(git_root: Path) -> None:
    repo = git_root / "omnimarket"
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/dev")
    result = _read(git_root, F.EXEMPLAR, lane_kind="design-build", revision="head")
    assert result.status is S.RESOLVED
    assert "on origin/main" in result.entries[0].text
    assert result.entries[0].as_of == datetime(2026, 10, 15, 12, tzinfo=UTC)


def test_exemplar_respects_revision_and_file_cap(
    git_root: Path, tmp_path: Path
) -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    contract["context_source_repo_doctrine"]["exemplar"]["max_files"] = 1
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    result = _read(git_root, F.EXEMPLAR, lane_kind="design-build", contract_path=path)
    assert "files (2):\nA\tCLAUDE.md\n... and 1 more" in result.entries[0].text
    result = _read(
        git_root, F.EXEMPLAR, lane_kind="red-test-fix", revision="2026-10-15T23:00:00Z"
    )
    assert result.status is S.UNRESOLVED


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_git_failures_are_unavailable(
    root: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        if failure == "missing":
            raise FileNotFoundError("git")
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(subprocess, "run", fail)
    assert _read(root, F.EXEMPLAR, lane_kind="design-build").status is S.UNAVAILABLE


def test_exemplar_outside_a_git_repo_is_unavailable(root: Path) -> None:
    assert _read(root, F.EXEMPLAR, lane_kind="design-build").status is S.UNAVAILABLE


@pytest.mark.parametrize("root_kind", ["unset", "missing", "file"])
def test_missing_root_is_unavailable(tmp_path: Path, root_kind: str) -> None:
    path = tmp_path / "missing"
    if root_kind == "file":
        path.write_text("not a directory", encoding="utf-8")
    assert (
        _read(None if root_kind == "unset" else path, F.INVARIANTS).status
        is S.UNAVAILABLE
    )


def test_unserved_factor_is_unresolved(root: Path) -> None:
    assert _read(root, F.OPEN_WORK).status is S.UNRESOLVED


@pytest.mark.parametrize(
    "repository", [None, "../outside", "omni/../..", "omnimarket\n"]
)
@pytest.mark.parametrize("factor", [F.INVARIANTS, F.CANONICAL_SHAPES, F.EXEMPLAR])
def test_repository_name_outside_the_pattern_is_skipped(
    root: Path, tmp_path: Path, repository: str | None, factor: EnumContextFactor
) -> None:
    ledger = _ledger(tmp_path, "2026-10-18T00:00:00Z | RULING | ../outside: no")
    result = _read(
        root, factor, repository=repository, lane_kind="design-build", ledger=ledger
    )
    assert result.status is S.UNRESOLVED
    assert result.entries == ()
    assert result.entry_counts


def test_symlink_outside_root_is_skipped(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(GUIDE, encoding="utf-8")
    guide = root / "omnimarket/CLAUDE.md"
    guide.unlink()
    guide.symlink_to(outside)
    assert _read(root, F.CANONICAL_SHAPES).status is S.UNRESOLVED
    result = _read(root, F.INVARIANTS)
    assert result.status is S.UNRESOLVED
    assert result.entry_counts["rulings_unavailable"] == 1


def test_file_mtime_is_used_without_a_revision_stamp(root: Path) -> None:
    result = _read(root, F.INVARIANTS, revision="head")
    assert {e.as_of for e in result.entries} == {
        datetime.fromtimestamp((root / "omnimarket/CLAUDE.md").stat().st_mtime, tz=UTC)
    }


def test_binding_is_frozen_and_rejects_unknown_fields() -> None:
    binding = load_repo_doctrine_binding(NODE_DIR / "contract.yaml")
    with pytest.raises(ValidationError):
        binding.__setattr__("root_env", "OTHER")
    with pytest.raises(ValidationError):
        type(binding).model_validate({**binding.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        type(binding.exemplar).model_validate(
            {**binding.exemplar.model_dump(), "unknown": True}
        )


def test_contract_topics_and_entry_point_are_declared() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    command = "onex.cmd.omnimarket.context-source-repo-doctrine-requested.v1"
    event = "onex.evt.omnimarket.context-source-repo-doctrine-completed.v1"
    assert contract["runtime_dispatch"] == {
        "command_topic": command,
        "terminal_events": {"success": event},
    }
    assert contract["event_bus"] == {
        "subscribe_topics": [command],
        "publish_topics": [event],
        "dlq_topics": ["onex.dlq.omnimarket.context-source-repo-doctrine-malformed.v1"],
    }
    assert (
        contract["terminal_event"]
        == HandlerContextSourceRepoDoctrine().terminal_event
        == event
    )
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert cls is HandlerContextSourceRepoDoctrine
    assert issubclass(NodeContextSourceRepoDoctrineEffect, cls)
    assert inspect.get_annotations(cls.handle, eval_str=True) == {
        "request": ModelContextSourceRequest,
        "return": ModelContextSourceResult,
    }
    assert cls.synchronous_execution is True
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][NODE]
        == f"omnimarket.nodes.{NODE}"
    )
    for path in (NODE_DIR / "handlers").glob("*.py"):
        assert "onex.cmd." not in path.read_text(encoding="utf-8")
        assert "onex.evt." not in path.read_text(encoding="utf-8")


def test_handler_satisfies_protocol_context_tier_source() -> None:
    assert isinstance(HandlerContextSourceRepoDoctrine(), ProtocolContextTierSource)


def test_a_ruling_in_overlapping_archives_is_carried_once(
    root: Path, tmp_path: Path
) -> None:
    row = "2026-10-23T00:00:00Z | RULING | omnimarket: overlap"
    ledger = _ledger(tmp_path, "2026-10-24T00:00:00Z | STATUS | not a ruling")
    (tmp_path / "archive/2026-10-24-split.md").write_text(row + "\n", encoding="utf-8")
    (tmp_path / "archive/2026-10-23-split.md").write_text(
        row + "\n2026-10-22T00:00:00Z | RULING | omnimarket: older\n",
        encoding="utf-8",
    )
    result = _read(root, F.INVARIANTS, ledger=ledger, revision="2026-10-24T12:00:00Z")
    rulings = [e for e in result.entries if (e.source_ref or "").startswith("ledger#")]
    assert [e.text for e in rulings] == [
        row,
        "2026-10-22T00:00:00Z | RULING | omnimarket: older",
    ]
