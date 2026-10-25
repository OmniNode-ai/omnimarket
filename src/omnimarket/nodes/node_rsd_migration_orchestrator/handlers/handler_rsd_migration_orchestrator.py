# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RSD migration orchestrator: move one script into an omnimarket node (OMN-21373).

RSD's earlier generator called a hardcoded ``onex`` environment that carried no
``task_class_authority`` entry point, so every dispatch was refused before any
model ran and the refusal came back as an empty candidate. This module is the
execution path that replaces it:

1. **Dispatch authority.** The task class is read from the ``onex.contracts``
   registry entry ``task_class_authority`` -- the same lookup ``onex delegate``
   makes -- before anything else. A missing entry, an undeclared class or a
   class the contract declares unroutable refuses the run with that reason, and
   no model turn is made.
2. **Execution adapter.** The migration is one delegated code edit loop
   (``node_delegated_code_edit_orchestrator``) over an omnimarket worktree:
   writes are confined to the new node and its tests, and the loop finishes only
   when the declared checks pass.
3. **Canonical shape.** The node directory is checked for ``__init__.py``,
   ``contract.yaml`` naming the node, ``metadata.yaml`` naming its entry point,
   a ``handlers/handler_*.py`` module and a test directory; once the loop is
   accepted the ``onex.nodes`` entry point is registered in ``pyproject.toml``
   and the shape is checked again with the entry point required.

Every run writes ``<state root>/runs/<correlation id>/rsd_migration_receipt.json``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable
from importlib import metadata
from pathlib import Path
from typing import Final, Literal, Protocol

import yaml

from omnimarket.inference.task_class_authority import ModelTaskClassAuthority
from omnimarket.models.model_delegated_code_edit import (
    EnumCodeEditStatus,
    ModelCodeEditResult,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.models.model_rsd_migration import (
    ModelRsdDispatchAuthority,
    ModelRsdMigrationReceipt,
    ModelRsdMigrationTask,
)

#: The registry group and entry ``onex delegate`` reads the task classes from.
CONTRACTS_GROUP = "onex.contracts"
AUTHORITY_ENTRY = "task_class_authority"
#: The code edit loop's only task class.
MIGRATION_TASK_CLASS: Final = "code_generation"
RECEIPT_NAME = "rsd_migration_receipt.json"
NODES_ENTRY_POINT_HEADER = '[project.entry-points."onex.nodes"]'


class RsdDispatchRefusedError(RuntimeError):
    """The dispatch has no task-class authority it may run under."""


class ProtocolCodeEditRunner(Protocol):
    """Runs one delegated code edit loop; the real one is the orchestrator node."""

    def __call__(
        self, request: ModelDelegatedCodeEditRequest
    ) -> ModelCodeEditResult: ...


EntryPointsReader = Callable[[], Iterable[metadata.EntryPoint]]


def _registry_entry_points() -> Iterable[metadata.EntryPoint]:
    return metadata.entry_points(group=CONTRACTS_GROUP)


def resolve_dispatch_authority(
    task_class: str = MIGRATION_TASK_CLASS,
    *,
    entry_points: EntryPointsReader = _registry_entry_points,
) -> ModelRsdDispatchAuthority:
    """The task-class authority for ``task_class``, read from the registry, or refuse."""
    found = [ep for ep in entry_points() if ep.name == AUTHORITY_ENTRY]
    if not found:
        raise RsdDispatchRefusedError(
            f"no installed distribution advertises {AUTHORITY_ENTRY!r} in the "
            f"{CONTRACTS_GROUP!r} entry-point group"
        )
    entry = found[0]
    loaded = entry.load()()
    if not isinstance(loaded, ModelTaskClassAuthority):
        raise RsdDispatchRefusedError(
            f"{entry.value} returned {type(loaded).__name__}, not a task-class authority"
        )
    declared = loaded.task_classes.get(task_class)
    if declared is None:
        raise RsdDispatchRefusedError(
            f"task class {task_class!r} is not declared by the task-class contract"
        )
    refusal = loaded.unroutable_refusal(task_class)
    if refusal is not None:
        raise RsdDispatchRefusedError(refusal)
    budget = loaded.execution_budgets.get(task_class)
    return ModelRsdDispatchAuthority(
        task_class=task_class,
        source=entry.value,
        gateway_exposure=declared.gateway_exposure.value,
        timeout_ceiling_seconds=(
            budget.task_class_timeout_ceiling_seconds if budget else None
        ),
    )


def node_paths(node_name: str) -> tuple[str, str]:
    """The worktree-relative node and test directories of ``node_name``."""
    return f"src/omnimarket/nodes/{node_name}", f"tests/nodes/{node_name}"


def build_code_edit_request(
    task: ModelRsdMigrationTask,
    authority: ModelRsdDispatchAuthority,
    *,
    correlation_id: str,
) -> ModelDelegatedCodeEditRequest:
    """The delegated code edit request that performs the migration."""
    node_dir, test_dir = node_paths(task.node_name)
    # The node-purity gate refuses file, network and environment I/O in a
    # compute node, so a script that does I/O becomes an effect node.
    node_type = "effect" if task.node_name.endswith("_effect") else "compute"
    text = (
        f"Move the script below into a new omnimarket node `{task.node_name}` in "
        f"canonical shape. Purpose: {task.purpose}\n\n"
        f"Create exactly these files:\n"
        f"- {node_dir}/__init__.py (SPDX header, one-line docstring)\n"
        f"- {node_dir}/contract.yaml with `name: {task.node_name}`, `node_type: "
        f"{node_type}`, contract_version and node_version, a description, and a "
        f"handler_routing block naming the handler module\n"
        f"- {node_dir}/metadata.yaml with name, version, description, "
        f"`entry_points: {{onex.nodes: {{{task.node_name}: "
        f"omnimarket.nodes.{task.node_name}}}}}`, capabilities, "
        f"`dependencies: []`, license MIT, pack, display_name, node_role\n"
        f"- {node_dir}/handlers/__init__.py\n"
        f"- {node_dir}/handlers/handler_<name>.py holding the script's logic as "
        f"typed functions (no work at import time) and a `main(argv: list[str] | "
        f"None = None) -> int` CLI\n"
        f"- {node_dir}/__main__.py calling `raise SystemExit(main())`\n"
        f"- {test_dir}/__init__.py and {test_dir}/test_<name>.py with "
        f"`pytestmark = pytest.mark.unit` and tests over tmp_path that pin the "
        f"script's output\n"
        f"Keep the script's behaviour and output format. Every Python file starts "
        f"with the two SPDX lines (`# SPDX-FileCopyrightText: 2026 OmniNode.ai "
        f"Inc.` and `# SPDX-License-Identifier: MIT`) and has full type hints. "
        f"Do not edit any other file. Finish when every check passes.\n\n"
        f"Source ({task.source_label}):\n```python\n{task.source_text}\n```\n"
    )
    shape = ModelDeclaredCheck(
        name="shape",
        argv=(
            "uv",
            "run",
            "python",
            "-m",
            "omnimarket.nodes.node_rsd_migration_orchestrator",
            "check-shape",
            "--worktree",
            ".",
            "--node",
            task.node_name,
        ),
        timeout_seconds=120,
    )
    tests = ModelDeclaredCheck(
        name="tests",
        argv=("uv", "run", "pytest", "-q", "-p", "no:cacheprovider", test_dir),
        targets=(test_dir,),
    )
    lint = ModelDeclaredCheck(
        name="lint",
        argv=("uv", "run", "ruff", "check", node_dir, test_dir),
        timeout_seconds=120,
    )
    return ModelDelegatedCodeEditRequest(
        correlation_id=correlation_id,
        task=text,
        workspace_root=task.worktree,
        writable_globs=(f"{node_dir}/**", f"{test_dir}/**"),
        context_paths=(
            "src/omnimarket/nodes/node_lab_disk_growth_effect/metadata.yaml",
            "src/omnimarket/nodes/node_lab_disk_growth_effect/__main__.py",
        ),
        checks=(shape, tests, lint),
        max_turns=task.max_turns,
        task_type=MIGRATION_TASK_CLASS,
        caller_lane=task.caller_lane,
        ticket=task.ticket,
    )


def canonical_shape_problems(
    worktree: Path, node_name: str, *, require_entry_point: bool = False
) -> list[str]:
    """Every way the node at ``worktree`` misses the canonical shape; empty when it holds."""
    node_rel, test_rel = node_paths(node_name)
    node_dir = worktree / node_rel
    problems: list[str] = []
    for name in (
        "__init__.py",
        "contract.yaml",
        "metadata.yaml",
        "handlers/__init__.py",
    ):
        if not (node_dir / name).is_file():
            problems.append(f"missing {node_rel}/{name}")
    if not any((node_dir / "handlers").glob("handler_*.py")):
        problems.append(f"no {node_rel}/handlers/handler_*.py module")
    if not any((worktree / test_rel).glob("test_*.py")):
        problems.append(f"no {test_rel}/test_*.py")
    contract = _yaml_mapping(node_dir / "contract.yaml", problems)
    if contract is not None:
        if contract.get("name") != node_name:
            problems.append(f"contract.yaml name is {contract.get('name')!r}")
        if not contract.get("node_type"):
            problems.append("contract.yaml declares no node_type")
    meta = _yaml_mapping(node_dir / "metadata.yaml", problems)
    if meta is not None:
        if meta.get("name") != node_name:
            problems.append(f"metadata.yaml name is {meta.get('name')!r}")
        groups = meta.get("entry_points")
        declared = groups.get("onex.nodes") if isinstance(groups, dict) else None
        if not isinstance(declared, dict) or (
            declared.get(node_name) != f"omnimarket.nodes.{node_name}"
        ):
            problems.append("metadata.yaml does not declare its onex.nodes entry point")
    if require_entry_point and not _entry_point_registered(
        worktree / "pyproject.toml", node_name
    ):
        problems.append(f"pyproject.toml registers no onex.nodes entry for {node_name}")
    return problems


def _yaml_mapping(path: Path, problems: list[str]) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        problems.append(f"{path.name} does not parse: {str(exc)[:120]}")
        return None
    if not isinstance(loaded, dict):
        problems.append(f"{path.name} is not a mapping")
        return None
    return loaded


def _entry_point_line(node_name: str) -> str:
    return f'{node_name} = "omnimarket.nodes.{node_name}"'


def _entry_point_registered(pyproject: Path, node_name: str) -> bool:
    if not pyproject.is_file():
        return False
    return (
        _entry_point_line(node_name)
        in pyproject.read_text(encoding="utf-8").splitlines()
    )


def register_node_entry_point(pyproject: Path, node_name: str) -> bool:
    """Append ``node_name`` to the ``onex.nodes`` entry points; False when already there."""
    text = pyproject.read_text(encoding="utf-8")
    line = _entry_point_line(node_name)
    if line in text.splitlines():
        return False
    lines = text.splitlines(keepends=True)
    try:
        start = next(
            i for i, raw in enumerate(lines) if raw.strip() == NODES_ENTRY_POINT_HEADER
        )
    except StopIteration as exc:
        raise ValueError(
            f"{pyproject} has no {NODES_ENTRY_POINT_HEADER} table"
        ) from exc
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, f"{line}\n")
    pyproject.write_text("".join(lines), encoding="utf-8")
    return True


def run_rsd_migration(
    task: ModelRsdMigrationTask,
    *,
    run_code_edit: ProtocolCodeEditRunner,
    state_root: Path,
    entry_points: EntryPointsReader = _registry_entry_points,
    correlation_id: str | None = None,
) -> ModelRsdMigrationReceipt:
    """Run one RSD migration end to end and write its receipt."""
    cid = correlation_id or str(uuid.uuid4())
    try:
        authority = resolve_dispatch_authority(entry_points=entry_points)
    except RsdDispatchRefusedError as exc:
        receipt = ModelRsdMigrationReceipt(
            correlation_id=cid,
            status="refused",
            node_name=task.node_name,
            source_label=task.source_label,
            refusal=str(exc),
        )
        return _write_receipt(state_root, receipt)
    result = run_code_edit(build_code_edit_request(task, authority, correlation_id=cid))
    worktree = Path(task.worktree)
    registered = False
    if result.status == EnumCodeEditStatus.ACCEPTED:
        registered = register_node_entry_point(
            worktree / "pyproject.toml", task.node_name
        )
    problems = canonical_shape_problems(
        worktree, task.node_name, require_entry_point=True
    )
    accepted = result.status == EnumCodeEditStatus.ACCEPTED and not problems
    receipt = ModelRsdMigrationReceipt(
        correlation_id=cid,
        status="migrated" if accepted else "not_migrated",
        node_name=task.node_name,
        source_label=task.source_label,
        authority=authority,
        code_edit=result,
        entry_point_registered=registered,
        shape_problems=tuple(problems),
    )
    return _write_receipt(state_root, receipt)


def _write_receipt(
    state_root: Path, receipt: ModelRsdMigrationReceipt
) -> ModelRsdMigrationReceipt:
    target = state_root / "runs" / receipt.correlation_id / RECEIPT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return receipt


class HandlerRsdMigrationOrchestrator:
    """ORCHESTRATOR: runs one RSD migration over an injected code edit runner."""

    def __init__(
        self,
        run_code_edit: ProtocolCodeEditRunner | None = None,
        state_root: Path | None = None,
    ) -> None:
        # Optional so the handler constructs at boot from defaults alone; a run
        # without a bound runner is refused rather than half-wired.
        self._run_code_edit = run_code_edit
        self._state_root = state_root

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    async def handle(self, task: ModelRsdMigrationTask) -> ModelRsdMigrationReceipt:
        return await asyncio.to_thread(self.run, task)

    def run(self, task: ModelRsdMigrationTask) -> ModelRsdMigrationReceipt:
        if self._run_code_edit is None or self._state_root is None:
            raise RuntimeError(
                "no code edit runner or state root is bound; `python -m "
                "omnimarket.nodes.node_rsd_migration_orchestrator run` binds them"
            )
        return run_rsd_migration(
            task, run_code_edit=self._run_code_edit, state_root=self._state_root
        )


__all__ = [
    "AUTHORITY_ENTRY",
    "CONTRACTS_GROUP",
    "MIGRATION_TASK_CLASS",
    "RECEIPT_NAME",
    "HandlerRsdMigrationOrchestrator",
    "ProtocolCodeEditRunner",
    "RsdDispatchRefusedError",
    "build_code_edit_request",
    "canonical_shape_problems",
    "register_node_entry_point",
    "resolve_dispatch_authority",
    "run_rsd_migration",
]
