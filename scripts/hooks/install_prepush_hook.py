#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install and explicitly prepare immutable pre-push snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HOOK_NAME = "pre-push"
LEGACY_HOOK_RELATIVE_PATH = Path("scripts/hooks/prepush_smart_tests.sh")
BOOTSTRAP_MARKER = "OMN-17461 worktree-aware immutable pre-push bootstrap"
ADAPTER_NAME = "prepush_snapshot_adapter.py"
ADAPTER_SHA256 = hashlib.sha256(
    Path(__file__).with_name(ADAPTER_NAME).read_bytes()
).hexdigest()
_CACHE_ENV = "PREPUSH_SNAPSHOT_CACHE_ROOT"

BOOTSTRAP = """#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# {BOOTSTRAP_MARKER}
set -euo pipefail
die() { printf '[prepush-bootstrap] ERROR: %s\\n' "$1" >&2; exit 1; }
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git worktree"
repo_root="$(cd "$repo_root" && pwd -P)"
cd "$repo_root"
hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
adapter="$hook_dir/prepush_snapshot_adapter.py"
[[ -f "$adapter" ]] || die "immutable snapshot adapter is unavailable"
[[ "$(shasum -a 256 "$adapter" | awk '{print $1}')" == "{ADAPTER_SHA256}" ]] || die "immutable snapshot adapter identity does not match installer"
refs_file="$(mktemp)"
snapshot_parent=""
snapshot=""
selected_sha=""
marker_ready=0
precommit_python=""
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "$snapshot" && -d "$snapshot" ]]; then
    if [[ "$marker_ready" -eq 0 && -n "$selected_sha" ]]; then
      "$precommit_python" "$adapter" cleanup --repo-root "$repo_root" --snapshot "$snapshot" --parent "$snapshot_parent" --selected-sha "$selected_sha" || true
    else
      "$precommit_python" "$adapter" cleanup --repo-root "$repo_root" --snapshot "$snapshot" --parent "$snapshot_parent" || true
    fi
  fi
  rm -f "$refs_file"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM
cat > "$refs_file"
precommit_cmd="$(command -v pre-commit || true)"
if [[ -n "$precommit_cmd" ]]; then
  candidate="$(head -n 1 "$precommit_cmd" | sed 's%^#!%%')"
  if [[ -x "$candidate" ]] && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$candidate" -c 'import pre_commit' >/dev/null 2>&1; then precommit_python="$candidate"; fi
fi
if [[ -z "$precommit_python" ]]; then
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$candidate" -c 'import pre_commit' >/dev/null 2>&1; then precommit_python="$(command -v "$candidate")"; break; fi
  done
fi
[[ -n "$precommit_python" ]] || die "pre-commit is unavailable; refusing a vacuous pre-push pass"
[[ $# -eq 2 ]] || die "pre-push requires remote name and URL arguments"
if [[ -e "$repo_root/.onex-prepush-snapshot.json" ]]; then
  if env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$precommit_python" "$adapter" check-marker --snapshot "$repo_root" --repo-root "$repo_root" >/dev/null 2>&1; then
    die "refusing recursive pre-push snapshot invocation"
  fi
  die "refusing untrusted pre-push snapshot marker in caller worktree"
fi
selection="$(env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$precommit_python" "$adapter" select --remote-name "$1" --remote-url "$2" --refs "$refs_file")" || {
  status=$?
  [[ $status -eq 3 ]] && exit 0
  die "pre-commit could not select an outgoing ref; inspect the ref-pair diagnostic"
}
local_sha="$selection"
selected_sha="$local_sha"
git cat-file -e "$local_sha^{commit}" 2>/dev/null || die "selected local SHA is missing or not a commit"
cache_root="$HOME/.cache/onex/prepush-snapshots"
[[ -n "${PREPUSH_SNAPSHOT_CACHE_ROOT:-}" ]] && cache_root="$PREPUSH_SNAPSHOT_CACHE_ROOT"
prepared="$cache_root/$local_sha"
prepared_tree="$prepared/tree"
prepared_env="$prepared/venv"
manifest="$prepared/manifest.json"
[[ -r "$manifest" && -x "$prepared_env/bin/python" && -f "$prepared_tree/uv.lock" ]] || die "prepared target environment unavailable for $local_sha; run: uv run python scripts/hooks/install_prepush_hook.py --prepare $local_sha"
[[ "$(git -C "$prepared_tree" rev-parse HEAD)" == "$local_sha" ]] || die "prepared source snapshot is not bound to selected local SHA"
uv_cmd="$(command -v uv || true)"
[[ -n "$uv_cmd" ]] || die "uv is unavailable; refusing an unbound prepared environment"
uv_version="$(env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$uv_cmd" --version)"
pre_commit_version="$(env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$precommit_python" -c 'from pre_commit.constants import VERSION; print(VERSION)')"
snapshot_parent="$(mktemp -d "${TMPDIR:-/tmp}/onex-prepush-XXXXXX")"
snapshot="$snapshot_parent/tree"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CEILING_DIRECTORIES GIT_PREFIX PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX CONDA_DEFAULT_ENV UV_PROJECT_ENVIRONMENT UV_NO_SYNC
git -C "$repo_root" worktree add --detach "$snapshot" "$local_sha" >/dev/null || die "could not create immutable snapshot"
[[ "$(git -C "$snapshot" rev-parse HEAD)" == "$local_sha" ]] || die "snapshot HEAD does not equal selected local SHA"
[[ -f "$snapshot/.pre-commit-config.yaml" && -f "$snapshot/uv.lock" ]] || die "snapshot is missing pre-commit configuration or uv.lock"
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$precommit_python" "$adapter" write-marker --snapshot "$snapshot" --sha "$local_sha" || die "could not mark owned immutable snapshot"
marker_ready=1
snapshot_lock="$(shasum -a 256 "$snapshot/uv.lock" | awk '{print $1}')"
snapshot_config="$(shasum -a 256 "$snapshot/.pre-commit-config.yaml" | awk '{print $1}')"
prepared_lock="$(shasum -a 256 "$prepared_tree/uv.lock" | awk '{print $1}')"
[[ "$prepared_lock" == "$snapshot_lock" ]] || die "prepared target lock does not match committed target"
identity="$("$prepared_env/bin/python" -c 'import sys, sysconfig; print(f"{sys.implementation.cache_tag}|{sysconfig.get_platform()}")')"
python_abi="${identity%%|*}"
platform="${identity#*|}"
[[ -n "$python_abi" && -n "$platform" && "$python_abi" != "$platform" ]] || die "prepared interpreter identity is unavailable"
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u CONDA_PREFIX -u CONDA_DEFAULT_ENV "$precommit_python" "$adapter" validate-manifest --manifest "$manifest" --sha "$local_sha" --lock-sha256 "$snapshot_lock" --config-sha256 "$snapshot_config" --python-abi "$python_abi" --platform "$platform" --uv-version "$uv_version" --pre-commit-version "$pre_commit_version" || die "prepared environment identity does not match committed target"
(
  cd "$snapshot"
  export UV_PROJECT_ENVIRONMENT="$prepared_env"
  export UV_NO_SYNC=1
  "$precommit_python" -m pre_commit hook-impl --config "$snapshot/.pre-commit-config.yaml" --hook-type pre-push --hook-dir "$(git -C "$snapshot" rev-parse --path-format=absolute --git-common-dir)/hooks" -- "$@"
) < "$refs_file"
"""

BOOTSTRAP = BOOTSTRAP.replace("{BOOTSTRAP_MARKER}", BOOTSTRAP_MARKER).replace(
    "{ADAPTER_SHA256}", ADAPTER_SHA256
)


class HookInstallError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise HookInstallError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def common_hook_path(*, cwd: Path) -> Path:
    common = Path(_git(["rev-parse", "--git-common-dir"], cwd))
    return (
        (common if common.is_absolute() else (cwd / common).resolve())
        / "hooks"
        / HOOK_NAME
    )


def _main_worktree(cwd: Path) -> Path:
    for line in _git(["worktree", "list", "--porcelain"], cwd).splitlines():
        if line.startswith("worktree "):
            return Path(line[9:]).resolve()
    raise HookInstallError("git worktree list returned no primary worktree")


def _replaceable(path: Path, root: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return True
    if path.is_symlink():
        return path.resolve() in {
            (root / LEGACY_HOOK_RELATIVE_PATH).resolve(),
            (_main_worktree(root) / LEGACY_HOOK_RELATIVE_PATH).resolve(),
        }
    return BOOTSTRAP_MARKER in path.read_text(encoding="utf-8")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    temporary.chmod(mode)
    os.replace(temporary, path)


def _adapter_source() -> Path:
    return Path(__file__).with_name(ADAPTER_NAME)


def _replaceable_adapter(path: Path) -> bool:
    source = _adapter_source()
    return not path.exists() or (
        not path.is_symlink() and path.read_bytes() == source.read_bytes()
    )


def install(*, cwd: Path) -> Path:
    root = Path(_git(["rev-parse", "--show-toplevel"], cwd)).resolve()
    path = common_hook_path(cwd=root)
    if not _replaceable(path, root):
        raise HookInstallError(f"refusing to replace unknown common hook {path}")
    adapter_path = path.with_name(ADAPTER_NAME)
    if not _replaceable_adapter(adapter_path):
        raise HookInstallError(
            f"refusing to replace unknown snapshot adapter {adapter_path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(adapter_path, _adapter_source().read_bytes(), 0o755)
    _atomic_write(path, BOOTSTRAP.encode(), 0o755)
    return path


def check_installation(*, cwd: Path) -> tuple[bool, str]:
    root = Path(_git(["rev-parse", "--show-toplevel"], cwd)).resolve()
    path = common_hook_path(cwd=root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    adapter_path = path.with_name(ADAPTER_NAME)
    try:
        adapter_matches = adapter_path.read_bytes() == _adapter_source().read_bytes()
    except OSError as exc:
        return False, str(exc)
    return (
        text == BOOTSTRAP
        and bool(path.stat().st_mode & stat.S_IXUSR)
        and adapter_matches
        and bool(adapter_path.stat().st_mode & stat.S_IXUSR),
        str(path),
    )


def _cache_root() -> Path:
    return Path(
        os.environ.get(_CACHE_ENV, Path.home() / ".cache/onex/prepush-snapshots")
    ).expanduser()


def _lock_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "UV_NO_SYNC",
        "UV_PROJECT_ENVIRONMENT",
    ):
        env.pop(name, None)
    return env


def _output(command: list[str]) -> str:
    result = subprocess.run(
        command, capture_output=True, check=False, text=True, env=_scrubbed_env()
    )
    if result.returncode:
        raise HookInstallError(result.stderr.strip() or f"{' '.join(command)} failed")
    return result.stdout.strip()


def _precommit_python() -> str:
    precommit = shutil.which("pre-commit")
    candidates: list[str | None] = [sys.executable, shutil.which("python3")]
    if precommit:
        first_line = Path(precommit).read_text(encoding="utf-8").splitlines()[0]
        if first_line.startswith("#!"):
            candidates.insert(0, first_line.removeprefix("#!"))
    for candidate in candidates:
        if (
            candidate
            and subprocess.run(
                [candidate, "-c", "import pre_commit"], env=_scrubbed_env(), check=False
            ).returncode
            == 0
        ):
            # A virtualenv launcher is commonly a symlink to a base interpreter.
            # Resolving it discards the virtualenv's site-packages, including the
            # locked pre-commit installation this adapter must use.
            return candidate
    raise HookInstallError("pre-commit interpreter is unavailable for preparation")


def _manifest_data(*, tree: Path, local_sha: str, venv: Path) -> dict[str, str]:
    python = venv / "bin/python"
    identity = _output(
        [
            str(python),
            "-c",
            "import sys,sysconfig; print(f'{sys.implementation.cache_tag}|{sysconfig.get_platform()}')",
        ]
    )
    python_abi, separator, platform_name = identity.partition("|")
    if not separator or not python_abi or not platform_name:
        raise HookInstallError("prepared interpreter identity is unavailable")
    precommit_python = _precommit_python()
    return {
        "config_sha256": _lock_hash(tree / ".pre-commit-config.yaml"),
        "lock_sha256": _lock_hash(tree / "uv.lock"),
        "platform": platform_name,
        "policy": "uv-sync-frozen-offline-no-editable-v1",
        "pre_commit_version": _output(
            [
                precommit_python,
                "-c",
                "from pre_commit.constants import VERSION; print(VERSION)",
            ]
        ),
        "python_abi": python_abi,
        "sha": local_sha,
        "uv_version": _output(
            [str(Path(shutil.which("uv") or "uv").resolve()), "--version"]
        ),
    }


def prepare(*, cwd: Path, local_sha: str) -> Path:
    root = Path(_git(["rev-parse", "--show-toplevel"], cwd)).resolve()
    _git(["cat-file", "-e", f"{local_sha}^{{commit}}"], root)
    entry = _cache_root() / local_sha
    tree, venv, manifest = entry / "tree", entry / "venv", entry / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HookInstallError(
                f"cannot read prepared cache manifest {manifest}: {exc}"
            ) from exc
        if (
            tree.exists()
            and (venv / "bin/python").is_file()
            and data == _manifest_data(tree=tree, local_sha=local_sha, venv=venv)
        ):
            return entry
        raise HookInstallError(f"inconsistent prepared cache {entry}")
    if entry.exists():
        raise HookInstallError(f"incomplete prepared cache {entry}")
    entry.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(["worktree", "add", "--detach", str(tree), local_sha], root)
        env = _scrubbed_env()
        env["UV_PROJECT_ENVIRONMENT"] = str(venv)
        env["UV_OFFLINE"] = "1"
        result = subprocess.run(
            [
                str(Path(shutil.which("uv") or "uv").resolve()),
                "sync",
                "--frozen",
                "--offline",
                "--no-editable",
                "--project",
                str(tree),
            ],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise HookInstallError(
                f"offline target-lock preparation failed: {(result.stderr or result.stdout).strip()[:500]}"
            )
        if not (venv / "bin/python").is_file():
            raise HookInstallError("offline preparation produced no interpreter")
        manifest.write_text(
            json.dumps(
                _manifest_data(tree=tree, local_sha=local_sha, venv=venv),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return entry
    except Exception:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(tree)],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(entry, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--prepare")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.install:
            print(install(cwd=Path.cwd()))
        elif args.check:
            ok, message = check_installation(cwd=Path.cwd())
            print(message)
            return int(not ok)
        else:
            print(prepare(cwd=Path.cwd(), local_sha=args.prepare))
    except HookInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
