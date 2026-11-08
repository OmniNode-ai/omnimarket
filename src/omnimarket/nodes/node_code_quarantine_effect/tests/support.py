# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rigs for the quarantine suite: one in-memory, one on real files and git, the same chains run on both.

Also the named source mutations that the chain tests must turn red (OMN-23144 AC4).
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
from omnimarket.nodes.node_code_quarantine_effect.handlers import (
    handler_code_quarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_code_quarantine import (
    HandlerCodeQuarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumQuarantineMode,
    ModelCatalogRow,
    ModelFilesBackend,
    ModelGateCommand,
    ModelPolicy,
    ModelQuarantineRequest,
    ModelRemovalBatch,
    ModelRemovalEvidence,
)
from omnimarket.nodes.node_code_quarantine_effect.protocols import QuarantineStepError

REPO = "repo_a"
ENTITY = "unused_pkg"
ENTITY_PATHS = ("src/unused/mod.py", "src/unused/helper.py")
TEST_PATHS = ("tests/unused/test_mod.py",)
KEPT = "src/kept.py"
TAG = "removal-batch-1"
START = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
POLICY = ModelPolicy(quarantine_days=7, min_observation_days=7)
GATES = ("build", "start", "reading")


RigFactory = Callable[[type[HandlerCodeQuarantine]], "Rig"]


@dataclass
class Rig:
    handler: HandlerCodeQuarantine
    clock: SteppedClock
    events: Callable[[], list[object]]
    exists: Callable[[str], bool]
    catalog: Callable[[], list[dict[str, str]]]
    tag_sha: str
    set_gate: Callable[[str, bool], None]
    break_store: Callable[[], None]


def quarantine_request(
    run_id: str = "chain1", observation_days: int = 7, consumers: int = 0
) -> ModelQuarantineRequest:
    return ModelQuarantineRequest(
        run_id=run_id,
        mode=EnumQuarantineMode.QUARANTINE,
        repository=REPO,
        entity=ENTITY,
        entity_paths=ENTITY_PATHS,
        test_paths=TEST_PATHS,
        evidence=ModelRemovalEvidence(
            observation_days=observation_days,
            evidence_ref="reports/code-graph/observation.md",
            public_consumers=consumers,
        ),
    )


def check_request(run_id: str = "chain1") -> ModelQuarantineRequest:
    return ModelQuarantineRequest(
        run_id=run_id, mode=EnumQuarantineMode.CHECK, repository=REPO, entity=ENTITY
    )


def remove_request(
    rig: Rig,
    run_id: str = "chain1",
    tag: str | None = None,
    with_row: bool = True,
    row_sha: str | None = None,
) -> ModelQuarantineRequest:
    tag_name = TAG if tag is None else tag
    rows = (
        (
            ModelCatalogRow(
                entity=ENTITY,
                tag=tag_name,
                sha=rig.tag_sha if row_sha is None else row_sha,
                reason="no reader in a 7-day observation; no public consumer",
            ),
        )
        if with_row
        else ()
    )
    return ModelQuarantineRequest(
        run_id=run_id,
        mode=EnumQuarantineMode.REMOVE,
        repository=REPO,
        batch=ModelRemovalBatch(tag=tag_name, entities=(ENTITY,), catalog_rows=rows),
    )


def memory_rig(handler_cls: type[HandlerCodeQuarantine] = HandlerCodeQuarantine) -> Rig:
    sha = "0123456789abcdef0123456789abcdef01234567"
    store = InMemoryStore()
    repository = InMemoryRepository(
        {REPO: {*ENTITY_PATHS, *TEST_PATHS, KEPT}}, {REPO: {TAG: sha}}
    )
    gates = InMemoryGates(build=True, start=True, reading=True)
    clock = SteppedClock(START)
    reporter = InMemoryReporter()

    def set_gate(name: str, green: bool) -> None:
        setattr(gates, name, green)

    def break_store() -> None:
        def boom(*_args: object) -> None:
            raise QuarantineStepError("store", "store is down")

        store.get = boom  # type: ignore[method-assign,assignment]

    return Rig(
        handler=handler_cls(
            policy=POLICY,
            store=store,
            repository=repository,
            gates=gates,
            clock=clock,
            reporter=reporter,
        ),
        clock=clock,
        events=lambda: list(reporter.events),
        exists=lambda path: repository.path_exists(REPO, path),
        catalog=lambda: [
            {"repository": repo, **json.loads(row.model_dump_json())}
            for repo, row in store.catalog
        ],
        tag_sha=sha,
        set_gate=set_gate,
        break_store=break_store,
    )


def files_rig(
    root: Path, handler_cls: type[HandlerCodeQuarantine] = HandlerCodeQuarantine
) -> Rig:
    repo_root = root / "repo_a"
    for rel in (*ENTITY_PATHS, *TEST_PATHS, KEPT):
        target = repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    ident = ["-c", "user.name=suite", "-c", "user.email=suite@example.invalid"]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *ident, *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    git("tag", TAG)
    sha = git("rev-parse", f"{TAG}^{{commit}}")
    flags = root / "flags"
    flags.mkdir()
    (flags / "reading").write_text("present", encoding="utf-8")
    red_when_exists = (
        sys.executable,
        "-c",
        "import os, sys; sys.exit(1 if os.path.exists(sys.argv[1]) else 0)",
    )
    green_when_exists = (
        sys.executable,
        "-c",
        "import os, sys; sys.exit(0 if os.path.exists(sys.argv[1]) else 1)",
    )
    spec = ModelFilesBackend(
        state_file=str(root / "state" / "quarantine.json"),
        catalog_file=str(root / "state" / "catalog.jsonl"),
        repository_roots={REPO: str(repo_root)},
        git_binary="git",
        git_timeout_seconds=30,
        clean_build=ModelGateCommand(
            argv=(*red_when_exists, str(flags / "red-build")), timeout_seconds=30
        ),
        lab_runtime_start=ModelGateCommand(
            argv=(*red_when_exists, str(flags / "red-start")), timeout_seconds=30
        ),
        reading=ModelGateCommand(
            argv=(*green_when_exists, str(flags / "reading")), timeout_seconds=30
        ),
    )
    clock = SteppedClock(START)
    reporter = InMemoryReporter()

    def set_gate(name: str, green: bool) -> None:
        flag = flags / ("reading" if name == "reading" else f"red-{name}")
        present = (name == "reading") == green
        if present:
            flag.write_text("x", encoding="utf-8")
        elif flag.exists():
            flag.unlink()

    def break_store() -> None:
        Path(spec.state_file).write_text("not json", encoding="utf-8")

    def catalog() -> list[dict[str, str]]:
        path = Path(spec.catalog_file)
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    return Rig(
        handler=handler_cls(
            policy=POLICY,
            store=JsonFileStore(spec),
            repository=GitRepository(spec),
            gates=CommandGates(spec),
            clock=clock,
            reporter=reporter,
        ),
        clock=clock,
        events=lambda: list(reporter.events),
        exists=lambda path: (repo_root / path).exists(),
        catalog=catalog,
        tag_sha=sha,
        set_gate=set_gate,
        break_store=break_store,
    )


#: Named source mutations of the handler. Each ``(old, new)`` must match the handler source exactly once.
MUTATIONS: dict[str, tuple[str, str]] = {
    "quarantine-without-clock": (
        "clock_started_at=self._clock.now(),",
        "clock_started_at=None,",
    ),
    "delete-files-on-quarantine": (
        "self._store.put(started)",
        "self._store.put(started); self._repo.remove_paths(request.repository, paths)",
    ),
    "keep-clock-on-restore": (
        '"restored_reason": gate.value,',
        '"restored_reason": gate.value, "clock_started_at": record.clock_started_at,',
    ),
    "skip-build-gate": (
        "if not self._gates.clean_build(repository, excluded):",
        "if False:",
    ),
    "skip-start-gate": (
        "if not self._gates.lab_runtime_start(repository, excluded):",
        "if False:",
    ),
    "skip-reading-gate": (
        "if not self._gates.reading_present(repository, entity):",
        "if False:",
    ),
    "skip-tag-check": ("if not tagged:", "if False:"),
    "skip-catalog-check": (
        "rows: dict[str, ModelCatalogRow] = {r.entity: r for r in batch.catalog_rows}",
        "rows: dict[str, ModelCatalogRow] = {e: ModelCatalogRow(entity=e, tag=batch.tag, sha=tagged, reason='auto') for e in batch.entities}",
    ),
    "remove-before-days": (
        "if age < timedelta(days=self._policy.quarantine_days):",
        "if False:",
    ),
}


def mutated_handler_class(name: str) -> type[HandlerCodeQuarantine]:
    old, new = MUTATIONS[name]
    source = inspect.getsource(handler_code_quarantine)
    if source.count(old) != 1:
        raise AssertionError(
            f"mutation {name!r} no longer matches the handler source once"
        )
    module = types.ModuleType(f"mutant_{name.replace('-', '_')}")
    exec(compile(source.replace(old, new), f"<mutant {name}>", "exec"), module.__dict__)
    cls: type[HandlerCodeQuarantine] = module.HandlerCodeQuarantine
    return cls
