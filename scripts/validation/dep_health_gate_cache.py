#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-addressed cache + per-key serialization for the dep-health gate.

Why this exists (OMN-16816)
---------------------------
``dep-health-gate`` is a ``pass_filenames: false`` pre-commit hook that ran a full
``src/`` tree sweep on every invocation. It had no cache and no cross-process
serialization, so N concurrent worktree lanes each ran their own full scan.
Observed 2026-08-27: 8-16 simultaneous copies, load average 53, and a
``pre-commit run --all-files`` killed by its own 45-minute timeout.

Why it changed again (OMN-17694)
--------------------------------
OMN-16816 added a content-addressed cache plus a single **machine-wide** lock.
That turned "N simultaneous scans" into "N *sequential* scans behind one lock",
and the lock's 1800 s wait is shorter than one scan under contention (observed
24-42 min), so the second lane in the queue was arithmetically guaranteed to
fail closed and the fourth could never succeed. Two verbatim refusals were
recorded on 2026-09-03 across four lanes.

Three properties, all of them load-bearing:

1. **The key is the inputs.** A SHA-256 over exactly the bytes the sweep reads —
   every scanned file under ``src/``, every test file the handler-coverage
   passes read, the baseline JSON, the gate scripts, and the argument signature.
   The file set comes from ``engine/scan_inputs.py``, the same module the sweep
   engine walks with, so "this hit is for these inputs" holds by construction
   rather than by two files agreeing. Paths are hashed repo-relative, so two
   checkouts of the same content share one entry.
2. **The lock is per key, not per machine.** Lanes whose inputs differ have
   nothing to wait for and scan concurrently; lanes at the same key queue on
   that key and the double-checked cache read hands the waiters the winner's
   result instead of repeating the scan.
3. **Concurrency is bounded.** A slot pool caps simultaneous scans at a quarter
   of the machine's logical cores, so (2) cannot restore the unbounded thrash
   OMN-16816 was filed for.

Failure posture is unchanged: a lock or slot this process cannot obtain within
the timeout **fails closed**. This is a blocking delta gate; silently skipping it
is not in its contract. Engine failures (rc >= 2) are likewise never cached —
they are infrastructure errors, not verdicts, and a cached one would be sticky.

Stdlib only, on purpose: the cache-hit path must not pay ``uv run`` startup, so
this module runs under the bare system ``python3``, loads ``scan_inputs.py`` by
file path rather than importing the ``omnimarket`` package, and only shells into
``uv run`` on a miss.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
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
from types import ModuleType

SEVERITY_THRESHOLD = "CRITICAL"
BASELINE_RELPATH = Path(".onex_state") / "dep_health_baseline.json"
SWEEP_RELPATH = Path("scripts") / "ci" / "run_dep_health_sweep.py"
WRAPPER_RELPATH = Path("scripts") / "validation" / "run_dep_health_gate.sh"
SCAN_INPUTS_RELPATH = (
    Path("src")
    / "omnimarket"
    / "nodes"
    / "node_dependency_health_sweep"
    / "engine"
    / "scan_inputs.py"
)

# Entries older than this are pruned opportunistically after a successful write.
CACHE_MAX_AGE_S = 14 * 24 * 3600
LOCK_FILE_MODE = 0o600
"""Owner-only. These lock files gate a fail-closed check under a per-user
cache root, so no other local account has any reason to read or write them —
and a world-writable lock is one another account could hold or truncate."""

DEFAULT_LOCK_TIMEOUT_S = 1800.0
DEFAULT_POLL_S = 0.5

# The gate never claims more than this share of the machine's logical cores.
# A scan is one CPU-bound process, so the slot count is the core budget.
SCAN_CORE_SHARE_DIVISOR = 4

RunSweep = Callable[[Path, list[str]], "tuple[int, str]"]
CollectInputs = Callable[[Path], "tuple[list[Path], list[Path]]"]


class ScanLockTimeoutError(RuntimeError):
    """Raised when a scan lock or slot could not be acquired in time."""


class ScanInputsUnavailableError(RuntimeError):
    """Raised when the shared scan-input definition cannot be loaded."""


@dataclass(frozen=True)
class CacheEntry:
    """A replayable gate verdict: the sweep's exit code and its merged output."""

    key: str
    returncode: int
    output: str
    created_at: float


# ---------------------------------------------------------------------------
# Input set — shared with the sweep engine
# ---------------------------------------------------------------------------


def load_scan_inputs(repo_root: Path) -> ModuleType:
    """Load ``engine/scan_inputs.py`` by path, under the bare interpreter.

    Loading by path, not by package import, keeps the hit path free of the
    ``omnimarket`` dependency tree. A missing module is fatal rather than
    recoverable: without the shared definition this wrapper cannot prove its key
    covers what the sweep reads, and a key it cannot vouch for is worse than no
    cache at all.
    """
    path = repo_root / SCAN_INPUTS_RELPATH
    spec = importlib.util.spec_from_file_location(
        "onex_dep_health_scan_inputs", str(path)
    )
    if spec is None or spec.loader is None:
        raise ScanInputsUnavailableError(f"cannot load scan-input definition at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def default_collect_inputs(repo_root: Path) -> tuple[list[Path], list[Path]]:
    """Return (scanned source files, coverage corpus files) for ``repo_root``."""
    scan_inputs = load_scan_inputs(repo_root)
    src_root = repo_root / "src"
    return (
        list(scan_inputs.iter_scanned_source_files(src_root)),
        list(scan_inputs.iter_coverage_corpus_files(repo_root)),
    )


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def _relative_label(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX label, so the key never encodes a checkout path.

    A file outside the repo is labelled by name alone: its distance from the
    checkout root is a property of where the checkout sits, not of its content.

    Pure string arithmetic against an already-resolved root. The previous
    revision called ``Path.resolve()`` on every file *and* on the root, which
    cost 5.45 s per 3983 files under load — paid on the cache-hit path, on every
    commit.
    """
    if path.is_relative_to(repo_root):
        return path.relative_to(repo_root).as_posix()
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


def _absorb(
    digest: hashlib._Hash,
    paths: Sequence[Path],
    repo_root: Path,
    *,
    mark_absent: bool,
) -> None:
    for path in paths:
        digest.update(_relative_label(path, repo_root).encode("utf-8"))
        digest.update(b"\0")
        if mark_absent and not path.is_file():
            # An absent file is a stable, distinct state — not a hash collision
            # with an empty one.
            digest.update(b"absent")
        else:
            digest.update(_file_digest(path).encode("ascii"))
        digest.update(b"\n")


def compute_scan_key(
    *,
    repo_root: Path,
    scanned_files: Sequence[Path],
    coverage_files: Sequence[Path],
    extra_files: Sequence[Path],
    arg_signature: str,
) -> str:
    """Hash exactly the inputs that determine the sweep's verdict.

    ``scanned_files`` and ``coverage_files`` are kept as separate sections so a
    file that moves between the two roles cannot alias onto the same digest.
    """
    digest = hashlib.sha256()
    digest.update(b"omn-17694-dep-health-gate-v2\n")
    digest.update(arg_signature.encode("utf-8"))
    digest.update(b"\nscanned\n")
    _absorb(digest, scanned_files, repo_root, mark_absent=False)
    digest.update(b"coverage\n")
    _absorb(digest, coverage_files, repo_root, mark_absent=False)
    digest.update(b"extra\n")
    _absorb(digest, extra_files, repo_root, mark_absent=True)
    return digest.hexdigest()


def build_arg_signature(sweep_args: Sequence[str]) -> str:
    """Everything outside the file set that can change the verdict."""
    graphify = shutil.which("graphify") or "absent"
    return "|".join(sweep_args) + f"|graphify={graphify}"


def scan_key_for(
    *,
    repo_root: Path,
    collect_inputs: CollectInputs = default_collect_inputs,
    sweep_args: Sequence[str] | None = None,
) -> str:
    """Compute the cache key for a checkout, resolving its root exactly once."""
    root = repo_root.resolve()
    args = list(sweep_args) if sweep_args is not None else build_sweep_args(root)
    scanned_files, coverage_files = collect_inputs(root)
    return compute_scan_key(
        repo_root=root,
        scanned_files=scanned_files,
        coverage_files=coverage_files,
        extra_files=[
            root / BASELINE_RELPATH,
            root / SWEEP_RELPATH,
            root / WRAPPER_RELPATH,
            root / SCAN_INPUTS_RELPATH,
            Path(__file__).resolve(),
        ],
        arg_signature=build_arg_signature(args),
    )


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


def key_lock_path(cache_root: Path, key: str) -> Path:
    """The lock a lane takes to scan one specific input set."""
    return cache_root / "locks" / f"{key}.lock"


def slots_dir(cache_root: Path) -> Path:
    """Directory holding the machine-wide scan-concurrency slots."""
    return cache_root / "slots"


def default_scan_slots() -> int:
    """How many scans may run at once on this machine.

    A scan is a single CPU-bound process, so the slot count is a core budget:
    the gate never claims more than ``1 / SCAN_CORE_SHARE_DIVISOR`` of the
    logical cores, and always at least one so a single-core machine still works.
    """
    cores = os.cpu_count()
    if cores is None:
        return 1
    return max(1, cores // SCAN_CORE_SHARE_DIVISOR)


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


def _is_unheld(lock_path: Path) -> bool:
    """True when no process currently holds ``lock_path``.

    Unlinking a held lock would unname it while its holder keeps the open file
    description, so the next lane would create a fresh file, take it
    uncontended, and scan the same inputs the holder is already scanning.
    """
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, LOCK_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def prune_cache(cache_root: Path, *, max_age_s: float = CACHE_MAX_AGE_S) -> int:
    """Best-effort removal of stale entries and their locks. Never touches a fresh one."""
    cutoff = time.time() - max_age_s
    removed = 0
    for subdir in ("entries", "locks"):
        directory = cache_root / subdir
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            try:
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                if subdir == "locks" and not _is_unheld(path):
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


# ---------------------------------------------------------------------------
# Serialization: per-key exclusion, machine-wide bound
# ---------------------------------------------------------------------------


@contextmanager
def scan_lock(
    lock_path: Path,
    *,
    timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
) -> Iterator[None]:
    """Hold one exclusive lock file, or raise ScanLockTimeoutError.

    ``fcntl.flock(2)`` is used directly rather than ``flock(1)`` because macOS
    ships no ``flock`` binary, and the lock is released by the kernel if the
    holder dies — a crashed lane cannot wedge every other lane.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, LOCK_FILE_MODE)
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


@contextmanager
def scan_slot(
    directory: Path,
    *,
    slots: int,
    timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
) -> Iterator[int]:
    """Hold one of ``slots`` machine-wide scan slots, or raise on exhaustion.

    A counting semaphore built from ``slots`` lock files: a lane takes the first
    one it can, so distinct input sets proceed in parallel while total scan
    concurrency stays bounded. Kernel-released on death, like the per-key lock.
    """
    directory.mkdir(parents=True, exist_ok=True)
    # Opened one at a time and unwound on failure: a comprehension that raises
    # part-way through (EMFILE, say) would leak every descriptor it had already
    # opened, and this runs on every commit.
    fds: list[int] = []
    try:
        for index in range(slots):
            fds.append(
                os.open(
                    str(directory / f"{index}.lock"),
                    os.O_CREAT | os.O_RDWR,
                    LOCK_FILE_MODE,
                )
            )
    except OSError:
        for fd in fds:
            os.close(fd)
        raise
    deadline = time.monotonic() + timeout_s
    held: int | None = None
    try:
        while held is None:
            for index, fd in enumerate(fds):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                held = index
                break
            if held is None:
                if time.monotonic() >= deadline:
                    raise ScanLockTimeoutError(
                        f"all {slots} scan slot(s) in {directory} busy "
                        f"after {timeout_s:.0f}s"
                    ) from None
                time.sleep(poll_s)
        try:
            yield held
        finally:
            fcntl.flock(fds[held], fcntl.LOCK_UN)
    finally:
        for fd in fds:
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
    run_sweep: RunSweep = _default_run_sweep,
    collect_inputs: CollectInputs = default_collect_inputs,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    max_concurrent_scans: int | None = None,
    use_cache: bool = True,
) -> int:
    root = repo_root.resolve()
    sweep_args = build_sweep_args(root)
    key = scan_key_for(
        repo_root=root, collect_inputs=collect_inputs, sweep_args=sweep_args
    )
    entry_path = cache_entry_path(cache_root, key)
    slots = (
        max_concurrent_scans
        if max_concurrent_scans is not None
        else default_scan_slots()
    )

    if use_cache:
        entry = read_cache_entry(entry_path)
        if entry is not None:
            _emit(entry.output, cache_state="cache hit", key=key)
            return entry.returncode

    waited_at = time.monotonic()
    waited_s = 0.0
    try:
        # Per key, not per machine: a lane whose inputs nobody else is scanning
        # has nothing to wait for. Lanes at the same key queue here and are
        # handed the winner's result by the double-checked read below.
        with scan_lock(key_lock_path(cache_root, key), timeout_s=lock_timeout_s):
            if use_cache:
                entry = read_cache_entry(entry_path)
                if entry is not None:
                    _emit(entry.output, cache_state="cache hit (after wait)", key=key)
                    return entry.returncode
            # Bounded, so per-key locking cannot restore the unbounded thrash
            # OMN-16816 was filed for.
            with scan_slot(
                slots_dir(cache_root), slots=slots, timeout_s=lock_timeout_s
            ):
                waited_s = time.monotonic() - waited_at
                returncode, output = run_sweep(root, sweep_args)
            # Publish before releasing the per-key lock. Otherwise a waiter can
            # acquire the lock in the gap between unlock and os.replace(), miss
            # the entry, and repeat the same scan.
            if use_cache and returncode in (0, 1):
                write_cache_entry(entry_path, returncode=returncode, output=output)
                prune_cache(cache_root)
    except ScanLockTimeoutError as exc:
        # Fail CLOSED. This gate blocks; a lock it cannot take is not a licence
        # to skip the scan.
        print(
            f"dep-health-gate: FAILED CLOSED — could not acquire the scan lock "
            f"({exc}). Another lane is scanning the same inputs, or the machine "
            f"is at its {slots}-scan concurrency bound. Re-run the commit once it "
            f"finishes. The gate was NOT skipped.",
            file=sys.stderr,
        )
        return 2

    # rc 0/1 are verdicts (clean / new findings) and are safe to replay.
    # rc >= 2 is an engine or infrastructure failure — caching it would make a
    # transient failure permanent for every lane sharing the key.
    _emit(
        output,
        cache_state=f"cache miss (scanned, waited {waited_s:.1f}s)",
        key=key,
    )
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
        help="Seconds to wait for a scan lock or slot before failing closed.",
    )
    parser.add_argument(
        "--max-concurrent-scans",
        type=int,
        default=None,
        help=(
            "Machine-wide cap on simultaneous scans "
            f"(default: cores // {SCAN_CORE_SHARE_DIVISOR}, at least 1)."
        ),
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
    for required in (SWEEP_RELPATH, SCAN_INPUTS_RELPATH):
        if not (repo_root / required).is_file():
            print(
                f"ERROR: dep-health gate input not found at {repo_root / required}",
                file=sys.stderr,
            )
            return 2

    cache_root = default_cache_root()
    return run_gate(
        repo_root=repo_root,
        cache_root=cache_root,
        lock_timeout_s=args.lock_timeout,
        max_concurrent_scans=args.max_concurrent_scans,
        use_cache=not args.no_cache,
    )


if __name__ == "__main__":
    sys.exit(main())
