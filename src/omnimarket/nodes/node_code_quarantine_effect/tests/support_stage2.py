# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two-repository rigs for stage 2 (OMN-23147): one in memory, one on real files and git, the same reads on both."""

from __future__ import annotations

import inspect
import subprocess
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_memory import (
    InMemoryGates,
    InMemoryReporter,
    InMemoryRepository,
    InMemoryStore,
    SteppedClock,
)
from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_real import (
    CommandGates,
    GitRepository,
    JsonFileStore,
)
from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_stage2 import (
    InMemoryLoadedCopies,
    InMemoryStage2Reporter,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers import (
    handler_stage2_duplicates,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_code_quarantine import (
    HandlerCodeQuarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_stage2_duplicates import (
    HandlerStage2Duplicates,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumStage2Mode,
    ModelDuplicateCopy,
    ModelDuplicateGroup,
    ModelFilesBackend,
    ModelGateCommand,
    ModelRemovalEvidence,
    ModelRepositoryTag,
    ModelStage2Event,
    ModelStage2Request,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support import POLICY, START

HOME = "repo_home"
SPARE = "repo_spare"
NAME = "node_dup_example"
TAG = "removal-batch-stage-2"
NODE_FILES = (
    "src/pkg/nodes/node_dup_example/contract.yaml",
    "src/pkg/nodes/node_dup_example/handler.py",
)
NODE_TESTS = ("tests/nodes/test_node_dup_example.py",)
KEPT = "src/pkg/other.py"


@dataclass
class Stage2Rig:
    handler: HandlerStage2Duplicates
    clock: SteppedClock
    events: Callable[[], list[ModelStage2Event]]
    exists: Callable[[str, str], bool]
    tag_shas: dict[str, str]
    loaded: InMemoryLoadedCopies


def copy_of(
    repository: str, observation_days: int = 7, consumers: int = 0
) -> ModelDuplicateCopy:
    return ModelDuplicateCopy(
        repository=repository,
        entity_paths=NODE_FILES,
        test_paths=NODE_TESTS,
        evidence=ModelRemovalEvidence(
            observation_days=observation_days,
            evidence_ref="reports/code-graph/observation.md",
            public_consumers=consumers,
        ),
    )


def group_of(name: str = NAME, **copy_kwargs: int) -> ModelDuplicateGroup:
    return ModelDuplicateGroup(
        contract_name=name,
        copies=(copy_of(HOME), copy_of(SPARE, **copy_kwargs)),
    )


def stage2_request(
    mode: EnumStage2Mode,
    groups: tuple[ModelDuplicateGroup, ...],
    run_id: str = "s2",
    tag: str = TAG,
) -> ModelStage2Request:
    return ModelStage2Request(
        run_id=run_id,
        mode=mode,
        groups=groups,
        tags=(ModelRepositoryTag(repository=SPARE, tag=tag),)
        if mode is EnumStage2Mode.REMOVE
        else (),
    )


def _build(
    quarantine: HandlerCodeQuarantine,
    clock: SteppedClock,
    repository: InMemoryRepository | GitRepository,
    loaded: Mapping[str, str | None],
    exists: Callable[[str, str], bool],
    tag_shas: dict[str, str],
    handler_cls: type[HandlerStage2Duplicates],
) -> Stage2Rig:
    reporter = InMemoryStage2Reporter()
    reading = InMemoryLoadedCopies(loaded)
    return Stage2Rig(
        handler=handler_cls(
            quarantine=quarantine,
            loaded=reading,
            repository=repository,
            reporter=reporter,
        ),
        clock=clock,
        events=lambda: list(reporter.events),
        exists=exists,
        tag_shas=tag_shas,
        loaded=reading,
    )


def memory_stage2_rig(
    loaded: Mapping[str, str | None],
    handler_cls: type[HandlerStage2Duplicates] = HandlerStage2Duplicates,
) -> Stage2Rig:
    sha = "0123456789abcdef0123456789abcdef01234567"
    paths = {*NODE_FILES, *NODE_TESTS, KEPT}
    repository = InMemoryRepository(
        {HOME: set(paths), SPARE: set(paths)}, {HOME: {TAG: sha}, SPARE: {TAG: sha}}
    )
    clock = SteppedClock(START)
    quarantine = HandlerCodeQuarantine(
        policy=POLICY,
        store=InMemoryStore(),
        repository=repository,
        gates=InMemoryGates(build=True, start=True, reading=True),
        clock=clock,
        reporter=InMemoryReporter(),
    )
    return _build(
        quarantine,
        clock,
        repository,
        loaded,
        lambda repo, path: repository.path_exists(repo, path),
        {HOME: sha, SPARE: sha},
        handler_cls,
    )


def files_stage2_rig(
    root: Path,
    loaded: Mapping[str, str | None],
    handler_cls: type[HandlerStage2Duplicates] = HandlerStage2Duplicates,
) -> Stage2Rig:
    roots = {HOME: root / HOME, SPARE: root / SPARE}
    ident = ["-c", "user.name=suite", "-c", "user.email=suite@example.invalid"]
    shas: dict[str, str] = {}
    for repo, repo_root in roots.items():
        for rel in (*NODE_FILES, *NODE_TESTS, KEPT):
            target = repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x = 1\n", encoding="utf-8")

        def git(*args: str, cwd: Path = repo_root) -> str:
            return subprocess.run(
                ["git", *ident, *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "seed")
        git("tag", TAG)
        shas[repo] = git("rev-parse", f"{TAG}^{{commit}}")
    always_green = (sys.executable, "-c", "import sys; sys.exit(0)")
    spec = ModelFilesBackend(
        state_file=str(root / "state" / "quarantine.json"),
        catalog_file=str(root / "state" / "catalog.jsonl"),
        repository_roots={repo: str(path) for repo, path in roots.items()},
        git_binary="git",
        git_timeout_seconds=30,
        clean_build=ModelGateCommand(argv=always_green, timeout_seconds=30),
        lab_runtime_start=ModelGateCommand(argv=always_green, timeout_seconds=30),
        reading=ModelGateCommand(argv=always_green, timeout_seconds=30),
    )
    clock = SteppedClock(START)
    repository = GitRepository(spec)
    quarantine = HandlerCodeQuarantine(
        policy=POLICY,
        store=JsonFileStore(spec),
        repository=repository,
        gates=CommandGates(spec),
        clock=clock,
        reporter=InMemoryReporter(),
    )
    return _build(
        quarantine,
        clock,
        repository,
        loaded,
        lambda repo, path: (roots[repo] / path).exists(),
        shas,
        handler_cls,
    )


#: Named source mutations of the stage 2 handler. Each ``(old, new)`` must match the handler source exactly once.
MUTATIONS: dict[str, tuple[str, str]] = {
    "quarantine-the-loaded-copy": (
        "keep = next((c for c in group.copies if c.repository == loaded), None)",
        "keep = next((c for c in group.copies if c.repository != loaded), None)",
    ),
    "skip-the-loaded-copy-reading": (
        "loaded = self._loaded.loaded_repository(group.contract_name)",
        "loaded = group.copies[0].repository",
    ),
}


def mutated_stage2_class(name: str) -> type[HandlerStage2Duplicates]:
    old, new = MUTATIONS[name]
    source = inspect.getsource(handler_stage2_duplicates)
    if source.count(old) != 1:
        raise AssertionError(
            f"mutation {name!r} no longer matches the handler source once"
        )
    module = types.ModuleType(f"mutant_s2_{name.replace('-', '_')}")
    exec(compile(source.replace(old, new), f"<mutant {name}>", "exec"), module.__dict__)
    cls: type[HandlerStage2Duplicates] = module.HandlerStage2Duplicates
    return cls
