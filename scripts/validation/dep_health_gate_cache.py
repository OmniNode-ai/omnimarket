#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16816: content-addressed cache + machine-wide lock for the dep-health gate.

Why this exists
---------------
``dep-health-gate`` is a ``pass_filenames: false`` pre-commit hook that ran a full
``src/`` tree sweep on every invocation: an AST import graph over ~3k ``.py``
files plus a per-extension ``rglob`` topic scan over ~3.9k files. It had no cache
and no cross-process serialization, so N concurrent worktree lanes each ran their
own full scan. Observed 2026-08-27: 8-16 simultaneous copies, load average 53,
and a ``pre-commit run --all-files`` killed by its own 45-minute timeout.

Two fixes, both needed:

1. **Content-addressed cache.** The key is a SHA-256 over exactly the bytes the
   sweep reads — every ``src/**`` file with a suffix the sweep opens, plus the
   baseline JSON, the gate scripts, and the argument signature. Because the key
   is pure content (repo-relative paths only, never absolute ones), two worktree
   lanes holding the same tree share one entry. Entries are content-addressed
   files written with ``os.replace``, so concurrent writers can never tear one.
2. **Machine-wide lock.** Only the cache-*miss* path takes an ``fcntl.flock``, and
   it re-reads the cache after acquiring (double-checked locking) so the lanes
   that queued behind the winner get its result instead of repeating the scan.

Failure posture: a lock this process cannot obtain within the timeout **fails
closed**. This is a blocking delta gate; silently skipping it is not in its
contract. Engine failures (rc >= 2) are likewise never cached — they are
infrastructure errors, not verdicts, and a cached one would be sticky.

Stdlib only, on purpose: the cache-hit path must not pay ``uv run`` startup, so
this module runs under the bare system ``python3`` and only shells into ``uv run``
on a miss.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Suffixes the sweep actually opens: ContractTopologyParser._SOURCE_EXTENSIONS
# (topic-literal scan) plus the .py files the AST import scanner parses.
SCANNED_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".yaml", ".yml"})

SEVERITY_THRESHOLD = "CRITICAL"
BASELINE_RELPATH = Path(".onex_state") / "dep_health_baseline.json"
SWEEP_RELPATH = Path("scripts") / "ci" / "run_dep_health_sweep.py"
WRAPPER_RELPATH = Path("scripts") / "validation" / "run_dep_health_gate.sh"

# Entries older than this are pruned opportunistically after a successful write.
CACHE_MAX_AGE_S = 14 * 24 * 3600
DEFAULT_LOCK_TIMEOUT_S = 1800.0
DEFAULT_POLL_S = 0.5

RunSweep = Callable[[Path, list[str]], "tuple[int, str]"]


class ScanLockTimeoutError(RuntimeError):
    """Raised when the machine-wide scan lock could not be acquired in time."""


@dataclass(frozen=True)
class CacheEntry:
    """A replayable gate verdict: the sweep's exit code and its merged output."""

    key: str
    returncode: int
    output: str
    created_at: float


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def iter_scanned_files(scan_roots: Sequence[Path]) -> list[Path]:
    """Return every file under scan_roots the sweep would open, sorted."""
    found: list[Path] = []
    for root in scan_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix in SCANNED_SUFFIXES and path.is_file():
                found.append(path)
    return sorted(found)


def _relative_label(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX label, so the key never encodes a checkout path."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return digest.hexdigest()


def compute_scan_key(
    *,
    repo_root: Path,
    scan_roots: Sequence[Path],
    extra_files: Sequence[Path],
    arg_signature: str,
) -> str:
    """Hash exactly the inputs that determine the sweep's verdict.

    Paths are hashed repo-relative so the key is identical across checkouts —
    that is what lets concurrent worktree lanes share a single cache entry.
    """
    digest = hashlib.sha256()
    digest.update(b"omn-16816-dep-health-gate-v1\n")
    digest.update(arg_signature.encode("utf-8"))
    digest.update(b"\nscanned\n")
    for path in iter_scanned_files(scan_roots):
        digest.update(_relative_label(path, repo_root).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_digest(path).encode("ascii"))
        digest.update(b"\n")
    digest.update(b"extra\n")
    for path in extra_files:
        digest.update(_relative_label(path, repo_root).encode("utf-8"))
        digest.update(b"\0")
        # An absent file is a stable, distinct state — not a hash collision with
        # an empty one.
        marker = _file_digest(path) if path.is_file() else "absent"
        digest.update(marker.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Cache entries
# ---------------------------------------------------------------------------


def default_cache_root() -> Path:
    """Machine-wide, per-user cache root. Entries inside are content-addressed."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "onex" / "dep-health-gate"


def cache_entry_path(cache_root: Path, key: str) -> Path:
    return cache_root / "entries" / f"{key}.json"


def write_cache_entry(path: Path, *, returncode: int, output: str) -> None:
    """Atomically publish one cache entry — never a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": path.stem,
        "returncode": returncode,
        "output": output,
        "created_at": time.time(),
    }
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # A failed publish must leave no debris behind for the next reader.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_cache_entry(path: Path) -> CacheEntry | None:
    """Load one entry, or None when it is absent, torn, or key-mismatched."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("key")
    returncode = payload.get("returncode")
    output = payload.get("output")
    if not isinstance(key, str) or key != path.stem:
        return None
    if not isinstance(returncode, int) or not isinstance(output, str):
        return None
    created_at = payload.get("created_at")
    return CacheEntry(
        key=key,
        returncode=returncode,
        output=output,
        created_at=float(created_at) if isinstance(created_at, (int, float)) else 0.0,
    )


def prune_cache(cache_root: Path, *, max_age_s: float = CACHE_MAX_AGE_S) -> int:
    """Best-effort removal of stale entries. Never touches a fresh one."""
    entries_dir = cache_root / "entries"
    if not entries_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for path in entries_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Machine-wide serialization
# ---------------------------------------------------------------------------


@contextmanager
def scan_lock(
    lock_path: Path,
    *,
    timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
) -> Iterator[None]:
    """Hold an exclusive machine-wide lock, or raise ScanLockTimeoutError.

    ``fcntl.flock(2)`` is used directly rather than ``flock(1)`` because macOS
    ships no ``flock`` binary, and the lock is released by the kernel if the
    holder dies — a crashed lane cannot wedge every other lane.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ScanLockTimeoutError(
                        f"scan lock {lock_path} still held after {timeout_s:.0f}s"
                    ) from None
                time.sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def build_sweep_args(repo_root: Path) -> list[str]:
    """Mirror the phased rollout the shell wrapper implemented.

    Phase 1 (no baseline committed): advisory, no --delta-mode.
    Phase 2 (baseline present): delta-blocking against the committed baseline.
    """
    args = [
        "--repo-roots",
        "src/",
        "--severity-threshold",
        SEVERITY_THRESHOLD,
    ]
    if (repo_root / BASELINE_RELPATH).is_file():
        args += [
            "--baseline-path",
            BASELINE_RELPATH.as_posix(),
            "--delta-mode",
        ]
    return args


def _default_run_sweep(repo_root: Path, sweep_args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["uv", "run", "python", SWEEP_RELPATH.as_posix(), *sweep_args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _emit(output: str, *, cache_state: str, key: str) -> None:
    finding_count = output.count('"finding_type"')
    print(
        f"dep-health-gate: {finding_count} finding(s) detected "
        f"(threshold: {SEVERITY_THRESHOLD})",
        file=sys.stderr,
    )
    print(f"dep-health-gate: {cache_state} [{key[:12]}]", file=sys.stderr)
    print(output)


def run_gate(
    *,
    repo_root: Path,
    cache_root: Path,
    lock_path: Path,
    run_sweep: RunSweep = _default_run_sweep,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    use_cache: bool = True,
) -> int:
    sweep_args = build_sweep_args(repo_root)
    graphify = shutil.which("graphify") or "absent"
    arg_signature = "|".join(sweep_args) + f"|graphify={graphify}"
    key = compute_scan_key(
        repo_root=repo_root,
        scan_roots=[repo_root / "src"],
        extra_files=[
            repo_root / BASELINE_RELPATH,
            repo_root / SWEEP_RELPATH,
            repo_root / WRAPPER_RELPATH,
            Path(__file__).resolve(),
        ],
        arg_signature=arg_signature,
    )
    entry_path = cache_entry_path(cache_root, key)

    if use_cache:
        entry = read_cache_entry(entry_path)
        if entry is not None:
            _emit(entry.output, cache_state="cache hit", key=key)
            return entry.returncode

    try:
        with scan_lock(lock_path, timeout_s=lock_timeout_s):
            # Double-checked: a lane that queued here may have been overtaken by
            # the winner, whose result is now cached under the identical key.
            if use_cache:
                entry = read_cache_entry(entry_path)
                if entry is not None:
                    _emit(entry.output, cache_state="cache hit (after wait)", key=key)
                    return entry.returncode
            returncode, output = run_sweep(repo_root, sweep_args)
    except ScanLockTimeoutError as exc:
        # Fail CLOSED. This gate blocks; a lock it cannot take is not a licence
        # to skip the scan.
        print(
            f"dep-health-gate: FAILED CLOSED — could not acquire the machine-wide "
            f"scan lock ({exc}). Another lane is running the dependency-health "
            f"sweep. Re-run the commit once it finishes, or clear a stuck holder "
            f"at {lock_path}. The gate was NOT skipped.",
            file=sys.stderr,
        )
        return 2

    # rc 0/1 are verdicts (clean / new findings) and are safe to replay.
    # rc >= 2 is an engine or infrastructure failure — caching it would make a
    # transient failure permanent for every lane sharing the key.
    if use_cache and returncode in (0, 1):
        write_cache_entry(entry_path, returncode=returncode, output=output)
        prune_cache(cache_root)

    _emit(output, cache_state="cache miss (scanned)", key=key)
    return returncode


def _discover_repo_root() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit("dep-health-gate: not inside a git repository")
    return Path(proc.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: git rev-parse --show-toplevel).",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_S,
        help="Seconds to wait for the machine-wide scan lock before failing closed.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Always run the scan and do not store the result. This only ever does "
            "MORE work than the default — it is not a way to skip the gate."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else _discover_repo_root()
    sweep_script = repo_root / SWEEP_RELPATH
    if not sweep_script.is_file():
        print(
            f"ERROR: dep-health gate script not found at {sweep_script}",
            file=sys.stderr,
        )
        return 2

    cache_root = default_cache_root()
    return run_gate(
        repo_root=repo_root,
        cache_root=cache_root,
        lock_path=cache_root / "scan.lock",
        lock_timeout_s=args.lock_timeout,
        use_cache=not args.no_cache,
    )


if __name__ == "__main__":
    sys.exit(main())
