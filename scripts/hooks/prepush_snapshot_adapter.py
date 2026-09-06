#!/usr/bin/env python3
"""Mechanical helper installed beside the shared immutable pre-push hook."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

POLICY = "uv-sync-frozen-offline-no-editable-v1"
MARKER_NAME = ".onex-prepush-snapshot.json"


def _load_object(path: Path, noun: str) -> dict[str, object]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {noun}: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{noun} must be a JSON object")
    return result


def validate(manifest_path: Path, expected: dict[str, str]) -> str | None:
    try:
        manifest = _load_object(manifest_path, "prepared manifest")
    except ValueError as exc:
        return str(exc)
    for key, value in {**expected, "policy": POLICY}.items():
        if manifest.get(key) != value:
            return f"prepared manifest {key!r} does not match committed target"
    return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marker_path(snapshot: Path) -> Path:
    return snapshot / MARKER_NAME


def _marker(snapshot: Path, sha: str) -> dict[str, object]:
    snapshot = snapshot.resolve()
    marker = _load_object(_marker_path(snapshot), "snapshot marker")
    if marker.get("version") != 1:
        raise ValueError("snapshot marker has an unsupported version")
    if marker.get("sha") != sha:
        raise ValueError("snapshot marker SHA does not match selected target")
    if marker.get("snapshot") != str(snapshot):
        raise ValueError("snapshot marker path does not match its worktree")
    if marker.get("adapter_sha256") != _sha256(Path(__file__).resolve()):
        raise ValueError("snapshot marker adapter identity does not match")
    return marker


def write_marker(snapshot: Path, sha: str) -> None:
    snapshot = snapshot.resolve()
    data = {
        "adapter_sha256": _sha256(Path(__file__).resolve()),
        "nonce": os.urandom(16).hex(),
        "sha": sha,
        "snapshot": str(snapshot),
        "version": 1,
    }
    marker = _marker_path(snapshot)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, marker)


def check_marker(snapshot: Path, repo_root: Path) -> int:
    try:
        snapshot = snapshot.resolve()
        repo_root = repo_root.resolve()
        if snapshot.parent.name.startswith("onex-prepush-") is False:
            raise ValueError("snapshot marker is outside an owned snapshot parent")
        head = subprocess.run(
            ["git", "-C", str(snapshot), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _marker(snapshot, head)
        listed = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if f"worktree {snapshot}\n" not in listed:
            raise ValueError("snapshot marker is not a registered worktree")
        common = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        snapshot_common = subprocess.run(
            ["git", "-C", str(snapshot), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        common_path = Path(common)
        snapshot_common_path = Path(snapshot_common)
        if not common_path.is_absolute():
            common_path = repo_root / common_path
        if not snapshot_common_path.is_absolute():
            snapshot_common_path = snapshot / snapshot_common_path
        if common_path.resolve() != snapshot_common_path.resolve():
            raise ValueError("snapshot marker has different common Git metadata")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"invalid pre-push snapshot marker: {exc}", file=sys.stderr)
        return 1
    print("verified pre-push snapshot marker", file=sys.stderr)
    return 0


def _registered_with_common_git(repo_root: Path, snapshot: Path) -> None:
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if f"worktree {snapshot}\n" not in listed:
        raise ValueError("snapshot marker is not a registered worktree")
    common = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot_common = subprocess.run(
        ["git", "-C", str(snapshot), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common_path = Path(common)
    snapshot_common_path = Path(snapshot_common)
    if not common_path.is_absolute():
        common_path = repo_root / common_path
    if not snapshot_common_path.is_absolute():
        snapshot_common_path = snapshot / snapshot_common_path
    if common_path.resolve() != snapshot_common_path.resolve():
        raise ValueError("snapshot marker has different common Git metadata")


def cleanup(
    repo_root: Path,
    snapshot: Path,
    parent: Path,
    selected_sha: str | None = None,
) -> int:
    """Remove only a marker-owned snapshot and its direct mktemp parent."""
    try:
        snapshot = snapshot.resolve()
        parent = parent.resolve()
        if snapshot.parent != parent or not parent.name.startswith("onex-prepush-"):
            raise ValueError("refusing to clean an unowned snapshot path")
        head = subprocess.run(
            ["git", "-C", str(snapshot), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if _marker_path(snapshot).exists():
            _marker(snapshot, head)
        elif selected_sha and head == selected_sha:
            _registered_with_common_git(repo_root, snapshot)
        else:
            raise ValueError("refusing to clean an unmarked snapshot")
        removed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "remove",
                "--force",
                str(snapshot),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if removed.returncode:
            raise ValueError(removed.stderr.strip() or "git worktree remove failed")
        shutil.rmtree(parent)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(
            f"WARNING: failed to clean owned pre-push snapshot: {exc}", file=sys.stderr
        )
        return 1
    return 0


def select(remote_name: str, remote_url: str, refs: Path) -> int:
    """Delegate pre-push record semantics to the installed pre-commit parser."""
    try:
        from pre_commit.commands.hook_impl import Z40, _pre_push_ns

        stdin = refs.read_bytes()
        namespace = _pre_push_ns(False, (remote_name, remote_url), stdin)
        if namespace is None:
            return 3
        if namespace.to_ref is not None:
            print(namespace.to_ref)
            return 0
        for line in stdin.decode().splitlines():
            local_ref, local_sha, remote_ref, _remote_sha = line.rsplit(maxsplit=3)
            if (
                local_sha != Z40
                and local_ref == namespace.local_branch
                and remote_ref == namespace.remote_branch
            ):
                print(local_sha)
                return 0
        raise ValueError("selected root range is absent from pre-push stdin")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"pre-commit selector failed: {exc}", file=sys.stderr)
        return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("validate-manifest")
    manifest.add_argument("--manifest", type=Path, required=True)
    for name in (
        "sha",
        "lock-sha256",
        "config-sha256",
        "python-abi",
        "platform",
        "uv-version",
        "pre-commit-version",
    ):
        manifest.add_argument(f"--{name}", required=True)
    selection = commands.add_parser("select")
    selection.add_argument("--remote-name", required=True)
    selection.add_argument("--remote-url", required=True)
    selection.add_argument("--refs", type=Path, required=True)
    marker = commands.add_parser("write-marker")
    marker.add_argument("--snapshot", type=Path, required=True)
    marker.add_argument("--sha", required=True)
    check = commands.add_parser("check-marker")
    check.add_argument("--snapshot", type=Path, required=True)
    check.add_argument("--repo-root", type=Path, required=True)
    cleanup_parser = commands.add_parser("cleanup")
    cleanup_parser.add_argument("--repo-root", type=Path, required=True)
    cleanup_parser.add_argument("--snapshot", type=Path, required=True)
    cleanup_parser.add_argument("--parent", type=Path, required=True)
    cleanup_parser.add_argument("--selected-sha")
    args = parser.parse_args(argv)
    if args.command == "select":
        return select(args.remote_name, args.remote_url, args.refs)
    if args.command == "write-marker":
        write_marker(args.snapshot, args.sha)
        return 0
    if args.command == "check-marker":
        return check_marker(args.snapshot, args.repo_root)
    if args.command == "cleanup":
        return cleanup(args.repo_root, args.snapshot, args.parent, args.selected_sha)
    expected = {
        "sha": args.sha,
        "lock_sha256": args.lock_sha256,
        "config_sha256": args.config_sha256,
        "python_abi": args.python_abi,
        "platform": args.platform,
        "uv_version": args.uv_version,
        "pre_commit_version": args.pre_commit_version,
    }
    if error := validate(args.manifest, expected):
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
