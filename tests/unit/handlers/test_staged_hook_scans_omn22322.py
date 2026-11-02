# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Falsifiers of the four staged-diff commit hooks (OMN-22322).

For each hook: a violation planted in a changed file is refused with the exit code and
the message the whole-tree scan gives; a violation in a file the commit does not touch
is not judged by the staged entry (the rule is local to one file) and is caught by the
whole-tree mode CI and ``pre-commit run --all-files`` run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers import staged_hook_scans as staged
from omnimarket.nodes.node_deployment_fact_gate_effect.__main__ import _render
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    PACKAGED_CONFIG_DIR,
    PACKAGED_ROUTING_CONFIGS,
    HandlerDeploymentFactGate,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.models.model_deployment_fact_gate import (
    ModelDeploymentFactGateRequest,
)
from omnimarket.nodes.node_harness_invocation_check_effect.handlers import (
    handler_harness_invocation_check as harness,
)
from omnimarket.nodes.node_harness_invocation_check_effect.models.model_harness_invocation_check import (
    EnumHarnessInvocationKind,
    ModelHarnessInvocationBaselineEntry,
    ModelHarnessInvocationCheckRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
# Spelled in pieces so this file is not itself a retired-model reference.
RETIRED = "gpt" + "-oss-120b"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={**scrub_git_location_env(os.environ), **GIT_ENV},
    )


def _repo(root: Path, files: dict[str, str]) -> Path:
    _git(root, "init", "-q")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


# retired-model-references -------------------------------------------------------------


def test_retired_reference_in_a_changed_file_is_refused_like_the_whole_tree_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"old.md": "clean\n", "new.md": f"model {RETIRED}\n"})
    assert staged.retired_model_references(tmp_path, ["new.md"]) == 1
    staged_out = capsys.readouterr().out
    assert staged_out == "new.md:1: retired-model reference\n"
    assert staged.retired_model_references(tmp_path, []) == 1
    assert capsys.readouterr().out == staged_out


def test_retired_reference_in_an_unchanged_file_is_caught_by_the_whole_tree_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"old.md": f"model {RETIRED}\n", "new.md": "clean\n"})
    assert staged.retired_model_references(tmp_path, ["new.md"]) == 0
    assert capsys.readouterr().out == ""
    assert staged.retired_model_references(tmp_path, []) == 1
    assert capsys.readouterr().out == "old.md:1: retired-model reference\n"


def test_a_retired_name_in_a_filename_is_refused_in_the_staged_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = f"{RETIRED}.txt"
    _repo(tmp_path, {name: "clean\n"})
    assert staged.retired_model_references(tmp_path, [name]) == 1
    assert capsys.readouterr().out == f"{name}: retired-model reference in filename\n"


# harness-invocation-check -------------------------------------------------------------

PLANTED = "import subprocess\n\nsubprocess.run(['codex', 'exec', 'x'])\n"


@pytest.fixture
def empty_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = harness.load_policy().model_copy(update={"baseline": ()})
    monkeypatch.setattr(staged, "load_policy", lambda: policy)
    monkeypatch.setattr(harness, "load_policy", lambda: policy)


def _full_harness(root: Path) -> tuple[int, str]:
    result = harness.HandlerHarnessInvocationCheck().handle(
        ModelHarnessInvocationCheckRequest(repo_root=str(root))
    )
    return result.exit_code, "".join(line + "\n" for line in result.output_lines)


def test_a_harness_invocation_in_a_changed_file_is_refused_like_the_whole_tree_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], empty_baseline: None
) -> None:
    _repo(tmp_path, {"src/pkg/new.py": PLANTED, "src/pkg/old.py": "x = 1\n"})
    assert staged.harness_invocation_check(tmp_path, ["src/pkg/new.py"]) == 1
    staged_out = capsys.readouterr().out
    assert "src/pkg/new.py:3: cli_exec codex" in staged_out
    assert _full_harness(tmp_path) == (1, staged_out)


def test_a_harness_invocation_in_an_unchanged_file_is_caught_by_the_whole_tree_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], empty_baseline: None
) -> None:
    _repo(tmp_path, {"src/pkg/old.py": PLANTED, "src/pkg/new.py": "x = 1\n"})
    assert staged.harness_invocation_check(tmp_path, ["src/pkg/new.py"]) == 0
    capsys.readouterr()
    assert staged.harness_invocation_check(tmp_path, []) == 1
    assert "src/pkg/old.py:3: cli_exec codex" in capsys.readouterr().out


def test_a_staged_file_in_an_excluded_directory_is_not_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], empty_baseline: None
) -> None:
    _repo(tmp_path, {"docs/example.py": PLANTED})
    assert staged.harness_invocation_check(tmp_path, ["docs/example.py"]) == 0
    assert _full_harness(tmp_path)[0] == 0


def test_a_stale_baseline_entry_is_refused_in_the_staged_mode_like_the_whole_tree_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = ModelHarnessInvocationBaselineEntry(
        path="src/pkg/gone.py",
        kind=EnumHarnessInvocationKind.CLI_EXEC,
        target="codex",
        count=1,
    )
    policy = harness.load_policy().model_copy(update={"baseline": (entry,)})
    monkeypatch.setattr(staged, "load_policy", lambda: policy)
    monkeypatch.setattr(harness, "load_policy", lambda: policy)
    _repo(tmp_path, {"src/pkg/other.py": "x = 1\n"})
    assert staged.harness_invocation_check(tmp_path, ["src/pkg/other.py"]) == 1
    staged_out = capsys.readouterr().out
    assert "baseline entr(y/ies) now cover fewer sites" in staged_out
    assert _full_harness(tmp_path) == (1, staged_out)


def test_a_baselined_site_in_an_unstaged_file_is_not_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = ModelHarnessInvocationBaselineEntry(
        path="src/pkg/held.py",
        kind=EnumHarnessInvocationKind.CLI_EXEC,
        target="codex",
        count=1,
    )
    policy = harness.load_policy().model_copy(update={"baseline": (entry,)})
    monkeypatch.setattr(staged, "load_policy", lambda: policy)
    monkeypatch.setattr(harness, "load_policy", lambda: policy)
    _repo(tmp_path, {"src/pkg/held.py": PLANTED, "src/pkg/other.py": "x = 1\n"})
    assert staged.harness_invocation_check(tmp_path, ["src/pkg/other.py"]) == 0
    capsys.readouterr()
    assert _full_harness(tmp_path)[0] == 0


# rls-read-tenant-seam -----------------------------------------------------------------

UNSEAMED = "import asyncpg\n\n\nasync def read(conn):\n    return await conn.fetch('SELECT * FROM widget_rows')\n"
CLEAN = "x = 1\n"
MIGRATION = "ALTER TABLE widget_rows ENABLE ROW LEVEL SECURITY;\n"


def _rls_tree(root: Path, files: dict[str, str]) -> None:
    script = root / "scripts/ci/check_rls_read_tenant_seam.py"
    script.parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts/ci/check_rls_read_tenant_seam.py", script)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def _rls_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: list[str],
) -> tuple[int, str]:
    monkeypatch.chdir(root)
    code = staged.rls_read_tenant_seam(root, paths)
    return code, capsys.readouterr().err


def test_an_unseamed_rls_statement_in_a_changed_module_is_refused_like_the_whole_tree_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _rls_tree(
        tmp_path,
        {
            "src/omnimarket/migrations/0001_rls.sql": MIGRATION,
            "src/omnimarket/bad.py": UNSEAMED,
            "src/omnimarket/ok.py": CLEAN,
        },
    )
    code, err = _rls_run(tmp_path, monkeypatch, capsys, ["src/omnimarket/bad.py"])
    assert code == 1
    assert "bad.py:" in err
    assert _rls_run(tmp_path, monkeypatch, capsys, []) == (1, err)


def test_an_unseamed_rls_statement_in_an_unchanged_module_is_caught_by_the_whole_tree_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _rls_tree(
        tmp_path,
        {
            "src/omnimarket/migrations/0001_rls.sql": MIGRATION,
            "src/omnimarket/bad.py": UNSEAMED,
            "src/omnimarket/ok.py": CLEAN,
        },
    )
    assert _rls_run(tmp_path, monkeypatch, capsys, ["src/omnimarket/ok.py"]) == (0, "")
    code, err = _rls_run(tmp_path, monkeypatch, capsys, [])
    assert code == 1
    assert "bad.py:" in err


def test_a_staged_migration_judges_the_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _rls_tree(
        tmp_path,
        {
            "src/omnimarket/migrations/0001_rls.sql": MIGRATION,
            "src/omnimarket/bad.py": UNSEAMED,
        },
    )
    code, err = _rls_run(
        tmp_path, monkeypatch, capsys, ["src/omnimarket/migrations/0001_rls.sql"]
    )
    assert code == 1
    assert "bad.py:" in err


def test_no_rls_migration_still_fails_closed_in_the_staged_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _rls_tree(tmp_path, {"src/omnimarket/ok.py": CLEAN})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        staged.rls_read_tenant_seam(tmp_path, ["src/omnimarket/ok.py"])
    assert exit_info.value.code == 2


# deployment-fact-gate -----------------------------------------------------------------

BRANCHING = "def pick(task_class):\n    return task_class == 'code'\n"


def _fact_repo(root: Path, files: dict[str, str]) -> Path:
    config_dir = root / PACKAGED_CONFIG_DIR
    config_dir.mkdir(parents=True)
    for name in PACKAGED_ROUTING_CONFIGS:
        shutil.copy(REPO_ROOT / PACKAGED_CONFIG_DIR / name, config_dir / name)
    return _repo(
        root,
        files
        | {
            f"{PACKAGED_CONFIG_DIR}/{name}": (config_dir / name).read_text()
            for name in PACKAGED_ROUTING_CONFIGS
        },
    )


def _full_fact_gate(root: Path) -> tuple[int, str]:
    result = HandlerDeploymentFactGate().handle(
        ModelDeploymentFactGateRequest(repo_root=str(root), base_ref="HEAD")
    )
    report = _render(result, False)
    return (0 if result.passed else 1), report + "\n" if report else ""


def test_a_new_routing_comparison_in_a_changed_file_is_refused_like_the_whole_tree_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fact_repo(tmp_path, {"src/omnimarket/mod.py": "x = 1\n"})
    (tmp_path / "src/omnimarket/mod.py").write_text(BRANCHING)
    assert staged.deployment_fact_gate(tmp_path) == 1
    err = capsys.readouterr().err
    assert "new route-selecting comparison(s) against base HEAD: 1" in err
    assert "src/omnimarket/mod.py::pick::task_class == 'code' (0 -> 1)" in err
    assert _full_fact_gate(tmp_path) == (1, err)


def test_a_new_routing_comparison_in_an_untracked_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fact_repo(tmp_path, {"src/omnimarket/mod.py": "x = 1\n"})
    (tmp_path / "src/omnimarket/fresh.py").write_text(BRANCHING)
    assert staged.deployment_fact_gate(tmp_path) == 1
    assert "src/omnimarket/fresh.py::pick::" in capsys.readouterr().err


def test_a_routing_comparison_already_in_the_base_revision_is_not_new(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fact_repo(
        tmp_path,
        {"src/omnimarket/old.py": BRANCHING, "src/omnimarket/mod.py": "x = 1\n"},
    )
    (tmp_path / "src/omnimarket/mod.py").write_text("x = 2\n")
    assert staged.deployment_fact_gate(tmp_path) == 0
    assert _full_fact_gate(tmp_path) == (0, "")
    assert staged.changed_scanned_files(tmp_path, "HEAD") == ["src/omnimarket/mod.py"]


def test_only_files_that_differ_from_the_base_revision_are_parsed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fact_repo(
        tmp_path, {"src/omnimarket/a.py": "x = 1\n", "src/omnimarket/b.py": "y = 1\n"}
    )
    (tmp_path / "src/omnimarket/a.py").write_text("x = 2\n")
    (tmp_path / "src/omnimarket/b.py").write_text("y = 1\n")
    assert staged.changed_scanned_files(tmp_path, "HEAD") == ["src/omnimarket/a.py"]


# handler-dispatch-entrypoint ----------------------------------------------------------


def test_the_dispatch_entrypoint_gate_judges_a_planted_handler_like_the_gate_itself(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.validators import handler_dispatch_entrypoint as gate

    (tmp_path / "scan/node_x").mkdir(parents=True)
    (tmp_path / "scan/node_x/contract.yaml").write_text(
        "name: node_x\nhandler_routing:\n  handlers:\n"
        "    - handler:\n        name: HandlerNoEntrypoint\n"
        "        module: omn22322_planted_handler\n"
    )
    (tmp_path / "omn22322_planted_handler.py").write_text(
        "class HandlerNoEntrypoint:\n    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(gate, "MIN_EXPECTED_DECLARED_HANDLERS", 1)
    argv = [str(tmp_path / "scan"), "--baseline", str(tmp_path / "no_baseline.yaml")]
    direct = gate.main(argv)
    direct_err = capsys.readouterr().err
    assert direct == 1
    assert "node_x: HandlerNoEntrypoint" in direct_err
    assert staged.handler_dispatch_entrypoint(argv) == 1
    assert capsys.readouterr().err == direct_err


def test_the_libyaml_parser_is_restored_after_the_block() -> None:
    import yaml

    before = yaml.safe_load
    with staged.libyaml_parser():
        assert yaml.safe_load is not before or not hasattr(yaml, "CSafeLoader")
        assert yaml.safe_load("a: [1, 2]") == {"a": [1, 2]}
    assert yaml.safe_load is before
