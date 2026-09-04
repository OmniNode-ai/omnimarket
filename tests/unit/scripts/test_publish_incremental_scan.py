from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[3] / "scripts" / "publish_incremental_scan.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "publish_incremental_scan", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_workspace_config_path_prefers_cwd(tmp_path, monkeypatch):
    module = _load_script_module()
    repos_root = tmp_path / "workspace"
    repo_root = tmp_path / "omnimarket"
    repos_root.mkdir()
    local_manifest = (
        repo_root
        / "src"
        / "omnimarket"
        / "configs"
        / "adr_canary_rejected_manifest.v1.yaml"
    )
    local_manifest.parent.mkdir(parents=True)
    local_manifest.write_text("rejected_entries: []\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)

    resolved = module._resolve_workspace_config_path(
        "src/omnimarket/configs/adr_canary_rejected_manifest.v1.yaml", repos_root
    )

    assert resolved == local_manifest


def test_git_modified_files_skips_repo_with_no_commits(tmp_path):
    module = _load_script_module()
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)

    assert module._git_modified_files(repo, "2099-01-01T00:00:00+00:00") == []


def test_incremental_command_carries_typed_scope_and_provenance(tmp_path) -> None:
    module = _load_script_module()
    workspace = tmp_path / "workspace"
    source = workspace / "private-repo" / "docs" / "plan.md"
    source.parent.mkdir(parents=True)
    source.write_text("# decision\n", encoding="utf-8")
    args = argparse.Namespace(
        source_repository="OmniNode-ai/omni_home",
        source_visibility="private",
        publication_classification="restricted",
        kb_destination="private",
    )

    command = module._build_canary_command(
        args=args,
        repos_root=workspace,
        manifest_path=workspace / "discovery_manifest.yaml",
        source_files=[source],
    )

    assert command.scoped_files == ("private-repo/docs/plan.md",)
    assert command.source_provenance.source_repository == "OmniNode-ai/omni_home"
    assert command.source_provenance.source_visibility.value == "private"
    assert command.kb_destination.value == "private"
    assert "source" not in command.model_dump(mode="json")
    assert "since_timestamp" not in command.model_dump(mode="json")


def test_incremental_command_rejects_file_outside_workspace(tmp_path) -> None:
    module = _load_script_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# decision\n", encoding="utf-8")
    args = argparse.Namespace(
        source_repository="OmniNode-ai/omni_home",
        source_visibility="private",
        publication_classification="restricted",
        kb_destination="private",
    )

    with pytest.raises(ValueError, match="escapes repos root"):
        module._build_canary_command(
            args=args,
            repos_root=workspace,
            manifest_path=workspace / "discovery_manifest.yaml",
            source_files=[outside],
        )
