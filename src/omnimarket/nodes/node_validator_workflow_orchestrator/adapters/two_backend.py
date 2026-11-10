# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The composition root on both backends, and the planted-change scenario (OMN-23078).

In memory: the planted change, the trace, the gate and the reducer state are held in memory.
Real: the planted change is committed to a git repository and read through omnibase_core's
GitRepo; the trace is a JSON-lines spool, the gate and the reducer state are JSON files. The
backends are chosen by the two overlays, the paths by the env vars the overlays name.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from omnimarket.nodes.node_validator_verdict_reducer.handlers.handler_validator_verdict_reducer import (
    HandlerValidatorVerdictReducer,
)
from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    ModelVerdictReducerOverlay,
)
from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    example_overlay as reducer_example,
)
from omnimarket.nodes.node_validator_verdict_reducer.protocols import (
    ProtocolVerdictStore,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.change_readers import (
    GitChangeReader,
    InMemoryChangeReader,
    ModelPlantedChange,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.effects import (
    FileCheckGate,
    FileTraceEmitter,
    InMemoryCheckGate,
    InMemoryTraceEmitter,
    PackageContractSource,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.verdict_store import (
    store_from_overlay,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.handlers.handler_validator_workflow import (
    HandlerValidatorWorkflow,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    ModelValidatorWorkflowRequest,
    ModelValidatorWorkflowResult,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    ModelValidatorWorkflowOverlay,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    example_overlay as workflow_example,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.protocols import (
    ProtocolChangeReader,
    ProtocolContractSource,
)

REPO = "example/repo"


@dataclass
class ValidatorWorkflow:
    """A built workflow and the effects a test reads back."""

    handler: HandlerValidatorWorkflow
    emitter: InMemoryTraceEmitter | FileTraceEmitter
    gate: InMemoryCheckGate | FileCheckGate
    reducer: HandlerValidatorVerdictReducer


def _env(environ: Mapping[str, str], name: str) -> Path:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} is not set")
    return Path(value)


def build_workflow(
    overlay: ModelValidatorWorkflowOverlay,
    reducer_overlay: ModelVerdictReducerOverlay,
    environ: Mapping[str, str],
    *,
    planted: dict[str, ModelPlantedChange] | None = None,
    contracts: ProtocolContractSource | None = None,
    reader: ProtocolChangeReader | None = None,
    store: ProtocolVerdictStore | None = None,
) -> ValidatorWorkflow:
    """Wire the orchestrator from its overlays; injected parts replace the overlay's choice."""
    if reader is None:
        if overlay.backends.reader == "git":
            reader = GitChangeReader(
                _env(environ, overlay.git.repo_root_env),
                overlay.ratchet_sources,
                overlay.git.cat_file_timeout_s,
            )
        else:
            reader = InMemoryChangeReader(planted or {}, overlay.ratchet_sources)
    emitter: InMemoryTraceEmitter | FileTraceEmitter = (
        FileTraceEmitter(_env(environ, overlay.files.trace_path_env))
        if overlay.backends.emitter == "file"
        else InMemoryTraceEmitter()
    )
    gate: InMemoryCheckGate | FileCheckGate = (
        FileCheckGate(_env(environ, overlay.files.gate_path_env))
        if overlay.backends.gate == "file"
        else InMemoryCheckGate()
    )
    reducer = HandlerValidatorVerdictReducer(
        overlay=reducer_overlay,
        store=store
        if store is not None
        else store_from_overlay(reducer_overlay, environ),
    )
    handler = HandlerValidatorWorkflow(
        overlay=overlay,
        reader=reader,
        contracts=contracts or PackageContractSource(),
        reducer=reducer,
        emitter=emitter,
        gate=gate,
    )
    return ValidatorWorkflow(
        handler=handler, emitter=emitter, gate=gate, reducer=reducer
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_planted(root: Path, change: ModelPlantedChange) -> tuple[str, str]:
    """Commit the base files, then the head files; return ``(base, head)`` shas."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "planted@example.invalid")
    _git(root, "config", "user.name", "planted")
    _git(root, "config", "commit.gpgsign", "false")

    def write(files: dict[str, str]) -> None:
        for path, text in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    write(change.base_files)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    write(change.head_files)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", change.commit_message)
    return base, _git(root, "rev-parse", "HEAD")


Runner = Callable[[ModelPlantedChange, int], ModelValidatorWorkflowResult]


def _runner(real: bool, workdir: Path) -> Runner:
    """One reducer store kept across calls, so its state carries from one PR to the next."""
    environ = {
        "ONEX_VALIDATOR_WORKFLOW_TRACE_PATH": str(workdir / "trace.jsonl"),
        "ONEX_VALIDATOR_WORKFLOW_GATE_PATH": str(workdir / "gate.json"),
        "ONEX_VALIDATOR_VERDICT_STATE_PATH": str(workdir / "verdict-state.json"),
    }
    overlay = workflow_example()
    reducer_overlay = reducer_example()
    if real:
        overlay = overlay.model_copy(
            update={
                "backends": overlay.backends.model_copy(
                    update={"reader": "git", "emitter": "file", "gate": "file"}
                )
            }
        )
        reducer_overlay = reducer_overlay.model_copy(
            update={
                "store": reducer_overlay.store.model_copy(update={"backend": "file"})
            }
        )
    store = store_from_overlay(reducer_overlay, environ)

    def run(change: ModelPlantedChange, pr: int) -> ModelValidatorWorkflowResult:
        if real:
            root = workdir / f"repo-{pr}"
            base, head = commit_planted(root, change)
            reader: ProtocolChangeReader = GitChangeReader(
                root, overlay.ratchet_sources, overlay.git.cat_file_timeout_s
            )
        else:
            base, head = "base", f"head-{pr}"
            reader = InMemoryChangeReader(
                {f"{REPO}#{pr}": change}, overlay.ratchet_sources
            )
        built = build_workflow(
            overlay, reducer_overlay, environ, reader=reader, store=store
        )
        return built.handler.handle(
            ModelValidatorWorkflowRequest(
                correlation_id=f"planted-{pr}", repo=REPO, pr=pr, head=head, base=base
            )
        )

    return run


def make_in_memory_backend(workdir: Path) -> Runner:
    return _runner(False, workdir)


def make_real_backend(workdir: Path) -> Runner:
    return _runner(True, workdir)


BASELINE = ".onex_ratchets/canonical_file_shape_baseline.txt"


def planted_clean() -> ModelPlantedChange:
    return ModelPlantedChange(
        base_files={"README.md": "base\n"},
        head_files={"README.md": "base\n", "docs/notes.md": "a note\n"},
    )


def planted_noncanonical() -> ModelPlantedChange:
    """A new code file outside every canonical location: Canonical File Shape refuses it."""
    return ModelPlantedChange(
        base_files={"README.md": "base\n"},
        head_files={"README.md": "base\n", "scripts/new_tool.py": "print('tool')\n"},
    )


def scenario_planted_change(factory: Callable[[Path], Runner], workdir: Path) -> object:
    """A clean change passes and a non-canonical file fails, the same on both backends."""
    run = factory(workdir)
    clean = run(planted_clean(), 1)
    bad = run(planted_noncanonical(), 2)
    assert clean.verdict is not None
    assert bad.verdict is not None
    return {
        "clean": clean.verdict.verdict.value,
        "noncanonical": bad.verdict.verdict.value,
        "rules": list(bad.outcomes[0].rule_ids),
    }
