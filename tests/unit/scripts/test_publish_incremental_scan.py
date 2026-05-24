from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

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
    local_manifest = repo_root / "docs" / "adr-canary" / "rejected_manifest.yaml"
    local_manifest.parent.mkdir(parents=True)
    local_manifest.write_text("rejected_entries: []\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)

    resolved = module._resolve_workspace_config_path(
        "docs/adr-canary/rejected_manifest.yaml", repos_root
    )

    assert resolved == local_manifest


def test_git_modified_files_skips_repo_with_no_commits(tmp_path):
    module = _load_script_module()
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)

    assert module._git_modified_files(repo, "2099-01-01T00:00:00+00:00") == []
