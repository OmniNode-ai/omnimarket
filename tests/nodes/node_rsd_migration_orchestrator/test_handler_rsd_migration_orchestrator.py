# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RSD's migration dispatch reads its task class from the registry and runs one code edit loop."""

from __future__ import annotations

import asyncio
import json
from importlib import metadata
from pathlib import Path

import pytest

from omnimarket.models.model_delegated_code_edit import (
    EnumCodeEditStatus,
    ModelCodeEditResult,
    ModelDelegatedCodeEditRequest,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.handlers.handler_rsd_migration_cli import (
    main,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.handlers.handler_rsd_migration_orchestrator import (
    AUTHORITY_ENTRY,
    CONTRACTS_GROUP,
    RECEIPT_NAME,
    HandlerRsdMigrationOrchestrator,
    RsdDispatchRefusedError,
    build_code_edit_request,
    canonical_shape_problems,
    register_node_entry_point,
    resolve_dispatch_authority,
    run_rsd_migration,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.models.model_rsd_migration import (
    ModelRsdMigrationTask,
)

pytestmark = pytest.mark.unit

NODE = "node_sample_summary_compute"
AUTHORITY_VALUE = "omnimarket.inference.task_class_authority:load_task_class_authority"


def _authority_entry() -> metadata.EntryPoint:
    return metadata.EntryPoint(
        name=AUTHORITY_ENTRY, value=AUTHORITY_VALUE, group=CONTRACTS_GROUP
    )


def _no_entries() -> list[metadata.EntryPoint]:
    return []


def _pyproject(root: Path) -> Path:
    path = root / "pyproject.toml"
    path.write_text(
        '[project]\nname = "x"\n\n[project.entry-points."onex.nodes"]\n'
        'node_a = "omnimarket.nodes.node_a"\n\n[project.entry-points."onex.contracts"]\n'
        'task_class_authority = "m:f"\n',
        encoding="utf-8",
    )
    return path


def _write_node(root: Path, name: str = NODE) -> None:
    node = root / "src/omnimarket/nodes" / name
    (node / "handlers").mkdir(parents=True)
    (node / "__init__.py").write_text("", encoding="utf-8")
    (node / "handlers/__init__.py").write_text("", encoding="utf-8")
    (node / "handlers/handler_summary.py").write_text("", encoding="utf-8")
    (node / "contract.yaml").write_text(
        f"name: {name}\nnode_type: compute\n", encoding="utf-8"
    )
    (node / "metadata.yaml").write_text(
        f"name: {name}\nentry_points:\n  onex.nodes:\n"
        f"    {name}: omnimarket.nodes.{name}\n",
        encoding="utf-8",
    )
    tests = root / "tests/nodes" / name
    tests.mkdir(parents=True)
    (tests / "test_summary.py").write_text("", encoding="utf-8")


def _task(root: Path) -> ModelRsdMigrationTask:
    return ModelRsdMigrationTask(
        source_label="private-data-repo:tla/pr_landing/summarize.py",
        source_text="print('x')\n",
        node_name=NODE,
        worktree=str(root),
        purpose="summarize model checker results",
        ticket="OMN-21373",
    )


class _Recorder:
    def __init__(self, status: EnumCodeEditStatus, root: Path | None = None) -> None:
        self.status = status
        self.root = root
        self.requests: list[ModelDelegatedCodeEditRequest] = []

    def __call__(self, request: ModelDelegatedCodeEditRequest) -> ModelCodeEditResult:
        self.requests.append(request)
        if self.root is not None:
            _write_node(self.root)
        return ModelCodeEditResult(
            loop_run_id=request.correlation_id, status=self.status, turns=1
        )


def test_dispatch_without_the_registry_entry_is_refused() -> None:
    with pytest.raises(RsdDispatchRefusedError, match="task_class_authority"):
        resolve_dispatch_authority(entry_points=_no_entries)


def test_dispatch_authority_comes_from_the_task_class_contract() -> None:
    authority = resolve_dispatch_authority(entry_points=lambda: [_authority_entry()])
    assert authority.task_class == "code_generation"
    assert authority.source == AUTHORITY_VALUE


def test_installed_registry_advertises_the_authority() -> None:
    authority = resolve_dispatch_authority()
    assert authority.task_class == "code_generation"


def test_undeclared_task_class_is_refused() -> None:
    with pytest.raises(RsdDispatchRefusedError, match="not declared"):
        resolve_dispatch_authority(
            "no_such_class", entry_points=lambda: [_authority_entry()]
        )


def test_refused_dispatch_runs_no_model_turn(tmp_path: Path) -> None:
    runner = _Recorder(EnumCodeEditStatus.ACCEPTED)
    receipt = run_rsd_migration(
        _task(tmp_path),
        run_code_edit=runner,
        state_root=tmp_path / "state",
        entry_points=_no_entries,
        correlation_id="00000000-0000-4000-8000-000000000001",
    )
    assert receipt.status == "refused"
    assert "task_class_authority" in receipt.refusal
    assert runner.requests == []
    written = tmp_path / "state/runs" / receipt.correlation_id / RECEIPT_NAME
    assert json.loads(written.read_text())["status"] == "refused"


def test_accepted_loop_registers_the_node_and_reports_migrated(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path)
    runner = _Recorder(EnumCodeEditStatus.ACCEPTED, root=tmp_path)
    receipt = run_rsd_migration(
        _task(tmp_path),
        run_code_edit=runner,
        state_root=tmp_path / "state",
        entry_points=lambda: [_authority_entry()],
    )
    assert receipt.status == "migrated", receipt.shape_problems
    assert receipt.entry_point_registered
    assert receipt.authority is not None
    assert receipt.authority.task_class == "code_generation"
    (request,) = runner.requests
    assert request.task_type == "code_generation"
    assert request.ticket == "OMN-21373"
    assert set(request.writable_globs) == {
        f"src/omnimarket/nodes/{NODE}/**",
        f"tests/nodes/{NODE}/**",
    }
    assert {check.name for check in request.checks} == {"shape", "tests", "lint"}
    assert "print('x')" in request.task
    assert f'{NODE} = "omnimarket.nodes.{NODE}"' in pyproject.read_text()


def test_rejected_loop_is_not_migrated_and_registers_nothing(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path)
    before = pyproject.read_text()
    receipt = run_rsd_migration(
        _task(tmp_path),
        run_code_edit=_Recorder(EnumCodeEditStatus.BUDGET_EXHAUSTED),
        state_root=tmp_path / "state",
        entry_points=lambda: [_authority_entry()],
    )
    assert receipt.status == "not_migrated"
    assert not receipt.entry_point_registered
    assert pyproject.read_text() == before


def test_shape_check_names_each_missing_part(tmp_path: Path) -> None:
    problems = canonical_shape_problems(tmp_path, NODE, require_entry_point=False)
    assert f"missing src/omnimarket/nodes/{NODE}/contract.yaml" in problems
    assert f"no tests/nodes/{NODE}/test_*.py" in problems


def test_shape_check_requires_the_entry_point_when_asked(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    _write_node(tmp_path)
    assert canonical_shape_problems(tmp_path, NODE) == []
    assert canonical_shape_problems(tmp_path, NODE, require_entry_point=True) == [
        f"pyproject.toml registers no onex.nodes entry for {NODE}"
    ]


def test_register_entry_point_lands_in_the_nodes_table_once(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path)
    assert register_node_entry_point(pyproject, NODE)
    assert not register_node_entry_point(pyproject, NODE)
    lines = pyproject.read_text().splitlines()
    added = lines.index(f'{NODE} = "omnimarket.nodes.{NODE}"')
    assert lines[added - 1] == 'node_a = "omnimarket.nodes.node_a"'


def test_check_shape_cli_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check-shape", "--worktree", str(tmp_path), "--node", NODE]) == 1
    assert capsys.readouterr().out.endswith("shape=incomplete\n")
    _write_node(tmp_path)
    assert main(["check-shape", "--worktree", str(tmp_path), "--node", NODE]) == 0
    assert capsys.readouterr().out == "shape=ok\n"


def test_unbound_handler_refuses_to_run(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no code edit runner"):
        HandlerRsdMigrationOrchestrator().run(_task(tmp_path))


def test_handle_dispatches_through_the_bound_runner(tmp_path: Path) -> None:
    runner = _Recorder(EnumCodeEditStatus.CHECKS_FAILED)
    handler = HandlerRsdMigrationOrchestrator(runner, tmp_path / "state")
    receipt = asyncio.run(handler.handle(_task(tmp_path)))
    assert receipt.status == "not_migrated"
    assert len(runner.requests) == 1


def test_node_type_follows_the_node_name_suffix(tmp_path: Path) -> None:
    authority = resolve_dispatch_authority(entry_points=lambda: [_authority_entry()])
    effect = _task(tmp_path).model_copy(update={"node_name": "node_sample_effect"})
    compute = _task(tmp_path)
    assert (
        "`node_type: effect`"
        in build_code_edit_request(
            effect, authority, correlation_id="00000000-0000-4000-8000-000000000002"
        ).task
    )
    assert (
        "`node_type: compute`"
        in build_code_edit_request(
            compute, authority, correlation_id="00000000-0000-4000-8000-000000000003"
        ).task
    )
