# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17694: the dep-health gate must not serialize unrelated lanes.

OMN-16816 gave the gate a content-addressed cache and a *machine-wide* scan
lock. That converted "N lanes run N simultaneous full scans" into "N lanes run N
*sequential* full scans behind one lock", and the lock's 1800 s wait is shorter
than a single scan under contention (observed 24-42 min), so the second lane in
the queue was arithmetically guaranteed to fail closed and the fourth could
never succeed. Two verbatim refusals were recorded on 2026-09-03.

These tests pin the three properties that fix it, without relaxing the
fail-closed posture:

* two checkouts holding identical inputs share one scan result;
* a changed input — including a test file, which the sweep reads and the
  OMN-16816 key did not cover — misses;
* lanes whose inputs differ scan concurrently instead of queueing, bounded by a
  slot pool so "concurrently" never means "unbounded", which is the thrash
  OMN-16816 was filed for.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_MODULE_PATH = REPO_ROOT / "scripts" / "validation" / "dep_health_gate_cache.py"
SCAN_INPUTS_PATH = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dependency_health_sweep"
    / "engine"
    / "scan_inputs.py"
)


def _load_by_path(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_by_path("dep_health_gate_cache_omn17694", GATE_MODULE_PATH)
scan_inputs = _load_by_path("dep_health_scan_inputs_omn17694", SCAN_INPUTS_PATH)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures: miniature repos shaped like the trees the sweep walks
# ---------------------------------------------------------------------------


def _make_repo(root: Path, *, handler_body: str = "import os\n") -> Path:
    """A repo with the two input families the sweep reads: src/ and tests/."""
    src = root / "src" / "pkg" / "node_a"
    src.mkdir(parents=True)
    (src / "handler_a.py").write_text(handler_body)
    (src / "contract.yaml").write_text(
        "name: node_a\nevent_bus:\n  publish_topics: [onex.evt.a.b.v1]\n"
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_handler_a.py").write_text("from pkg import handler_a\n")
    (root / ".onex_state").mkdir()
    (root / ".onex_state" / "dep_health_baseline.json").write_text('{"findings": []}')
    return root


def _collect(repo_root: Path) -> tuple[list[Path], list[Path]]:
    """The production input collector, over the real shared definition."""
    return (
        scan_inputs.iter_scanned_source_files(repo_root / "src"),
        scan_inputs.iter_coverage_corpus_files(repo_root),
    )


class _RecordingSweep:
    """Counts scans and records how many ran at the same instant."""

    def __init__(
        self,
        *,
        hold_s: float = 0.0,
        rendezvous: int | None = None,
        rendezvous_timeout_s: float = 30.0,
    ) -> None:
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.hold_s = hold_s
        self.rendezvous = rendezvous
        self._lock = threading.Lock()
        # A barrier, not an overlapping sleep: proving "these ran at the same
        # time" by hoping N threads land inside one 0.5 s window is a race that
        # a loaded machine loses. Every lane must arrive before any may leave,
        # so serialization cannot satisfy it and slowness cannot fake it.
        self._barrier = (
            threading.Barrier(rendezvous, timeout=rendezvous_timeout_s)
            if rendezvous is not None
            else None
        )

    def __call__(self, repo_root: Path, sweep_args: list[str]) -> tuple[int, str]:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self._barrier is not None:
                try:
                    self._barrier.wait()
                except threading.BrokenBarrierError:
                    raise AssertionError(
                        f"fewer than {self.rendezvous} scans were inside the "
                        "sweep at once, so the lanes serialized"
                    ) from None
            time.sleep(self.hold_s)
        finally:
            with self._lock:
                self.active -= 1
        return 0, '{"status": "clean"}'


def _run(root: Path, cache_root: Path, sweep: Any, **overrides: Any) -> int:
    kwargs: dict[str, Any] = {
        "repo_root": root,
        "cache_root": cache_root,
        "run_sweep": sweep,
        "collect_inputs": _collect,
        "lock_timeout_s": 30.0,
    }
    kwargs.update(overrides)
    return int(gate.run_gate(**kwargs))


def _run_all(
    targets: list[Path], cache_root: Path, sweep: Any, **overrides: Any
) -> None:
    """Run the gate for every target concurrently and re-raise any failure."""
    failures: list[Exception] = []

    def lane(root: Path) -> None:
        try:
            _run(root, cache_root, sweep, **overrides)
        except Exception as exc:  # re-raised on the main thread below
            failures.append(exc)

    threads = [threading.Thread(target=lane, args=(root,)) for root in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for thread in threads:
        assert not thread.is_alive(), "a gate lane never finished"
    if failures:
        raise failures[0]


# ---------------------------------------------------------------------------
# (a) Identical inputs share one scan
# ---------------------------------------------------------------------------


def test_two_checkouts_with_identical_inputs_share_one_scan(tmp_path: Path) -> None:
    """Two lanes at the same inputs must cost one scan, not two."""
    lane_a = _make_repo(tmp_path / "lane_a")
    lane_b = _make_repo(tmp_path / "lane_b")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()

    assert _run(lane_a, cache_root, sweep) == 0
    assert _run(lane_b, cache_root, sweep) == 0
    assert sweep.calls == 1, "the second lane must be served the first lane's result"


def test_identical_keys_racing_collapse_to_one_scan(tmp_path: Path) -> None:
    """Concurrent lanes at the same key queue on that key and share the result."""
    lanes = [_make_repo(tmp_path / f"lane_{i}") for i in range(4)]
    sweep = _RecordingSweep(hold_s=0.3)

    _run_all(lanes, tmp_path / "cache", sweep, max_concurrent_scans=4)

    assert sweep.calls == 1, "four lanes at one key must not run four scans"


# ---------------------------------------------------------------------------
# (b) Changed inputs miss — including the test files the sweep reads
# ---------------------------------------------------------------------------


def test_a_changed_source_file_misses_the_cache(tmp_path: Path) -> None:
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()

    _run(root, cache_root, sweep)
    (root / "src" / "pkg" / "node_a" / "handler_a.py").write_text("import sys\n")
    _run(root, cache_root, sweep)

    assert sweep.calls == 2


def test_a_changed_test_file_misses_the_cache(tmp_path: Path) -> None:
    """The coverage passes read tests/, so a test edit is a changed input.

    OMN-16816 keyed only ``src/**``. Editing the test that covers a handler
    flips its UNTESTED_HANDLER verdict while leaving the key untouched, so the
    gate replayed a verdict computed against different inputs — a cache hit that
    was not verifiably for the inputs at hand.
    """
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()

    _run(root, cache_root, sweep)
    (root / "tests" / "test_handler_a.py").write_text("# coverage deleted\n")
    _run(root, cache_root, sweep)

    assert sweep.calls == 2, "a test-file edit must invalidate the cached verdict"


def test_an_added_test_file_misses_the_cache(tmp_path: Path) -> None:
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()

    _run(root, cache_root, sweep)
    (root / "tests" / "test_handler_b.py").write_text("from pkg import handler_b\n")
    _run(root, cache_root, sweep)

    assert sweep.calls == 2


def test_a_vendored_test_file_does_not_invalidate_the_cache(tmp_path: Path) -> None:
    """``.venv`` is pruned from the read set, so installing a package is not an input."""
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()

    _run(root, cache_root, sweep)
    vendored = root / ".venv" / "lib" / "site-packages" / "thirdparty"
    vendored.mkdir(parents=True)
    (vendored / "test_handler_a.py").write_text("# not our coverage\n")
    _run(root, cache_root, sweep)

    assert sweep.calls == 1


# ---------------------------------------------------------------------------
# (c) Distinct inputs scan concurrently, bounded
# ---------------------------------------------------------------------------


def test_distinct_keys_do_not_serialize(tmp_path: Path) -> None:
    """The defect: four unrelated lanes queued behind one machine-wide lock.

    A lane whose inputs nobody else is scanning has nothing to wait for.
    """
    lanes = [
        _make_repo(tmp_path / f"lane_{i}", handler_body=f"import os  # {i}\n")
        for i in range(4)
    ]
    sweep = _RecordingSweep(rendezvous=4)

    _run_all(lanes, tmp_path / "cache", sweep, max_concurrent_scans=4)

    assert sweep.calls == 4, "four distinct input sets are four distinct verdicts"
    assert sweep.peak == 4, (
        "distinct keys must scan concurrently; "
        f"peak concurrency was {sweep.peak}, so they serialized"
    )


def test_concurrent_scans_are_bounded_by_the_slot_pool(tmp_path: Path) -> None:
    """Concurrency is bounded, so per-key locking does not restore the thrash."""
    lanes = [
        _make_repo(tmp_path / f"lane_{i}", handler_body=f"import os  # {i}\n")
        for i in range(4)
    ]
    sweep = _RecordingSweep(hold_s=0.3)

    _run_all(lanes, tmp_path / "cache", sweep, max_concurrent_scans=2)

    assert sweep.calls == 4
    assert sweep.peak <= 2, f"slot pool of 2 admitted {sweep.peak} concurrent scans"


def test_default_slot_pool_is_a_bounded_share_of_the_machine(tmp_path: Path) -> None:
    slots = gate.default_scan_slots()
    assert slots >= 1
    assert slots <= max(1, (__import__("os").cpu_count() or 1))


# ---------------------------------------------------------------------------
# Fail-closed posture survives the change
# ---------------------------------------------------------------------------


def test_a_held_key_lock_fails_closed_rather_than_skipping(tmp_path: Path) -> None:
    """A lane that cannot take its own key's lock blocks the commit."""
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()
    key = gate.scan_key_for(repo_root=root, collect_inputs=_collect)

    with gate.scan_lock(
        gate.key_lock_path(cache_root, key), timeout_s=5.0, poll_s=0.05
    ):
        rc = _run(root, cache_root, sweep, lock_timeout_s=0.2)

    assert rc != 0, "fail-closed: a lock the lane cannot take must not exit 0"
    assert sweep.calls == 0


def test_an_exhausted_slot_pool_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()

    with gate.scan_slot(
        gate.slots_dir(cache_root), slots=1, timeout_s=5.0, poll_s=0.05
    ):
        rc = _run(root, cache_root, sweep, lock_timeout_s=0.2, max_concurrent_scans=1)

    assert rc != 0
    assert sweep.calls == 0
    assert "could not acquire" in capsys.readouterr().err.lower()


def test_a_busy_slot_pool_does_not_block_a_cache_hit(tmp_path: Path) -> None:
    """The hit path takes no lock at all — a warm lane never queues."""
    root = _make_repo(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep()
    assert _run(root, cache_root, sweep) == 0

    with gate.scan_slot(
        gate.slots_dir(cache_root), slots=1, timeout_s=5.0, poll_s=0.05
    ):
        assert (
            _run(root, cache_root, sweep, lock_timeout_s=0.2, max_concurrent_scans=1)
            == 0
        )
    assert sweep.calls == 1


# ---------------------------------------------------------------------------
# The key is computed over exactly the shared definition
# ---------------------------------------------------------------------------


def test_key_is_insensitive_to_checkout_path(tmp_path: Path) -> None:
    a = _make_repo(tmp_path / "some" / "deep" / "lane")
    b = _make_repo(tmp_path / "b")
    assert gate.scan_key_for(repo_root=a, collect_inputs=_collect) == gate.scan_key_for(
        repo_root=b, collect_inputs=_collect
    )


def test_key_ignores_files_the_sweep_never_reads(tmp_path: Path) -> None:
    root = _make_repo(tmp_path / "repo")
    before = gate.scan_key_for(repo_root=root, collect_inputs=_collect)
    (root / "README.md").write_text("# docs\n")
    (root / "src" / "pkg" / "node_a" / "handler.pyc").write_bytes(b"\x00")
    assert gate.scan_key_for(repo_root=root, collect_inputs=_collect) == before


# ---------------------------------------------------------------------------
# Pruning must not unname a lock somebody is holding
# ---------------------------------------------------------------------------


def _age(path: Path, *, days: float) -> None:
    stale = time.time() - days * 24 * 3600
    __import__("os").utime(path, (stale, stale))


def test_prune_removes_a_stale_unheld_lock(tmp_path: Path) -> None:
    lock = gate.key_lock_path(tmp_path / "cache", "e" * 64)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    _age(lock, days=30)

    gate.prune_cache(tmp_path / "cache")

    assert not lock.exists()


def test_prune_leaves_a_held_lock_alone(tmp_path: Path) -> None:
    """An unnamed-but-held lock would let a second lane rescan the same inputs."""
    cache_root = tmp_path / "cache"
    lock = gate.key_lock_path(cache_root, "f" * 64)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    _age(lock, days=30)

    with gate.scan_lock(lock, timeout_s=5.0, poll_s=0.05):
        gate.prune_cache(cache_root)
        assert lock.exists()
