# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Prove immutable pre-push target imports survive caller contamination."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.hooks import install_prepush_hook as installer

pytestmark = pytest.mark.unit


def _run(*args: str, cwd: Path, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, check=False, capture_output=True, text=True, **kwargs
    )


def _git(repo: Path, *args: str) -> str:
    result = _run("git", *args, cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'snapshot-target'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.12'\n"
        "\n"
        "[build-system]\n"
        "requires = []\n"
        "build-backend = 'backend'\n"
        "backend-path = ['build_backend']\n",
        encoding="utf-8",
    )
    backend = repo / "build_backend"
    backend.mkdir()
    (backend / "backend.py").write_text(
        "from pathlib import Path\n"
        "import zipfile\n"
        "NAME = 'snapshot_target'\n"
        "VERSION = '0.1.0'\n"
        "DIST = f'{NAME}-{VERSION}.dist-info'\n"
        "def _metadata(root):\n"
        "    path = root / DIST\n"
        "    path.mkdir(exist_ok=True)\n"
        "    (path / 'METADATA').write_text('Metadata-Version: 2.1\\nName: snapshot-target\\nVersion: 0.1.0\\n')\n"
        "    (path / 'WHEEL').write_text('Wheel-Version: 1.0\\nGenerator: provenance-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        "    return path\n"
        "def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):\n"
        "    _metadata(Path(metadata_directory))\n"
        "    return DIST\n"
        "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
        "    filename = f'{NAME}-{VERSION}-py3-none-any.whl'\n"
        "    root = Path(__file__).parent.parent\n"
        "    with zipfile.ZipFile(Path(wheel_directory) / filename, 'w') as archive:\n"
        "        archive.write(root / 'src' / 'targetpkg' / '__init__.py', 'targetpkg/__init__.py')\n"
        "        archive.writestr(f'{DIST}/METADATA', 'Metadata-Version: 2.1\\nName: snapshot-target\\nVersion: 0.1.0\\n')\n"
        "        archive.writestr(f'{DIST}/WHEEL', 'Wheel-Version: 1.0\\nGenerator: provenance-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        "        archive.writestr(f'{DIST}/RECORD', '')\n"
        "    return filename\n",
        encoding="utf-8",
    )
    package = repo / "src" / "targetpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "PROVENANCE = 'committed-target'\n", encoding="utf-8"
    )
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: provenance\n"
        "        name: provenance\n"
        "        entry: bash scripts/provenance.sh\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n"
        "        stages: [pre-push]\n",
        encoding="utf-8",
    )
    scripts = repo / "scripts"
    scripts.mkdir()
    provenance = scripts / "provenance.sh"
    provenance.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        '"$UV_PROJECT_ENVIRONMENT/bin/python" -c '
        "'import targetpkg; from pathlib import Path; "
        'print(targetpkg.PROVENANCE + "|" + str(Path(targetpkg.__file__).resolve()))\''
        ' > "$PREPUSH_RECORD"'
        "\n",
        encoding="utf-8",
    )
    provenance.chmod(0o755)
    # Generate the lock with offline uv; the fixture has no third-party runtime
    # dependencies, so this must not resolve anything from an index.
    locked = _run(
        "uv", "lock", "--offline", cwd=repo, env={**os.environ, "UV_OFFLINE": "1"}
    )
    assert locked.returncode == 0, locked.stderr
    _commit(repo, "fixture")
    caller = tmp_path / "caller" / "targetpkg"
    caller.mkdir(parents=True)
    (caller / "__init__.py").write_text(
        "PROVENANCE = 'caller-editable'\n", encoding="utf-8"
    )
    return repo, caller


def test_offline_noneditable_snapshot_import_excludes_caller_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, caller = _fixture_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    cache = tmp_path / "cache"
    monkeypatch.setenv("PREPUSH_SNAPSHOT_CACHE_ROOT", str(cache))

    prepared = installer.prepare(cwd=repo, local_sha=sha)
    manifest = json.loads((prepared / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha"] == sha
    assert (prepared / "venv" / "bin" / "python").is_file()

    hook = installer.install(cwd=repo)
    record = tmp_path / "provenance.txt"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "PREPUSH_SNAPSHOT_CACHE_ROOT": str(cache),
        "PREPUSH_RECORD": str(record),
        "PYTHONPATH": str(caller.parent),
        "UV_INDEX_URL": "https://invalid.example.invalid/simple",
        "UV_OFFLINE": "1",
    }
    # The cache was prepared above; the gate must use it without dependency
    # resolution. The local ref pair supplies the exact committed target.
    result = _run(
        str(hook),
        "origin",
        "unused",
        cwd=repo,
        input=f"refs/heads/topic {sha} refs/heads/topic {'0' * 40}\n",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    provenance, imported_path = record.read_text(encoding="utf-8").strip().split("|", 1)
    assert provenance == "committed-target"
    assert str(caller) not in imported_path
    assert imported_path.startswith(str(prepared / "venv"))


def test_prepared_cache_hit_is_manifest_bound_and_does_not_resync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _caller = _fixture_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    cache = tmp_path / "cache"
    monkeypatch.setenv("PREPUSH_SNAPSHOT_CACHE_ROOT", str(cache))
    first = installer.prepare(cwd=repo, local_sha=sha)

    started = time.monotonic()
    second = installer.prepare(cwd=repo, local_sha=sha)
    elapsed = time.monotonic() - started

    assert second == first
    assert elapsed < 5.0
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha"] == sha
    assert manifest["lock_sha256"] == installer._lock_hash(second / "tree" / "uv.lock")
    assert not (cache / "unexpected-network-resolution-marker").exists()
