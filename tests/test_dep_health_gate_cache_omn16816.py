# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16816: content-addressed cache + machine-wide lock for the dep-health gate.

The ``dep-health-gate`` pre-commit hook re-ran a full ``src/`` tree sweep on every
invocation with no caching and no cross-process serialization, so N concurrent
worktree lanes ran N full scans at once. These tests pin the two properties that
fix costs:

* the cache key is a pure function of the bytes the sweep actually reads, so two
  checkouts with identical content share a cache entry and an unrelated edit
  (a ``.md`` file, a ``.pyc``) does not invalidate it;
* the cache-miss scan is serialized machine-wide, and a lock the process cannot
  obtain fails CLOSED rather than silently skipping a blocking gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_MODULE_PATH = REPO_ROOT / "scripts" / "validation" / "dep_health_gate_cache.py"


def _load_gate_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "dep_health_gate_cache_omn16816", GATE_MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate_module()

pytestmark = pytest.mark.unit


def _make_tree(root: Path) -> Path:
    """Build a miniature repo whose src/ mirrors the shapes the sweep reads."""
    src = root / "src"
    (src / "pkg" / "node_a").mkdir(parents=True)
    (src / "pkg" / "node_a" / "handler.py").write_text("import os\n")
    (src / "pkg" / "node_a" / "contract.yaml").write_text(
        "name: node_a\nevent_bus:\n  publish_topics: [onex.evt.a.b.v1]\n"
    )
    (src / "pkg" / "widget.tsx").write_text("export const x = 1;\n")
    (root / ".onex_state").mkdir()
    (root / ".onex_state" / "dep_health_baseline.json").write_text('{"findings": []}')
    return root


def _key(root: Path, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "repo_root": root,
        "scan_roots": [root / "src"],
        "extra_files": [root / ".onex_state" / "dep_health_baseline.json"],
        "arg_signature": "CRITICAL|delta",
    }
    kwargs.update(overrides)
    return str(gate.compute_scan_key(**kwargs))


# ---------------------------------------------------------------------------
# Cache key: content-addressed, tied to exactly what the sweep reads
# ---------------------------------------------------------------------------


def test_key_is_identical_for_two_checkouts_with_identical_content(
    tmp_path: Path,
) -> None:
    """Two worktrees holding the same bytes must share one cache entry.

    This is the property that makes N concurrent lanes cost one scan instead of N.
    """
    a = _make_tree(tmp_path / "lane_a")
    b = _make_tree(tmp_path / "lane_b")
    assert _key(a) == _key(b)


def test_key_changes_when_a_scanned_source_file_changes(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "repo")
    before = _key(root)
    (root / "src" / "pkg" / "node_a" / "handler.py").write_text(
        "import os\nimport sys\n"
    )
    assert _key(root) != before


def test_key_changes_when_a_scanned_contract_is_added(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "repo")
    before = _key(root)
    (root / "src" / "pkg" / "node_b").mkdir()
    (root / "src" / "pkg" / "node_b" / "contract.yaml").write_text("name: node_b\n")
    assert _key(root) != before


def test_key_ignores_files_the_sweep_never_reads(tmp_path: Path) -> None:
    """A README or a stale .pyc must not evict a valid cache entry."""
    root = _make_tree(tmp_path / "repo")
    before = _key(root)
    (root / "src" / "pkg" / "README.md").write_text("# docs\n")
    (root / "src" / "pkg" / "handler.pyc").write_bytes(b"\x00\x01")
    assert _key(root) == before


def test_key_changes_when_the_baseline_changes(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "repo")
    before = _key(root)
    (root / ".onex_state" / "dep_health_baseline.json").write_text(
        '{"findings": [{"finding_type": "ORPHAN_TOPIC"}]}'
    )
    assert _key(root) != before


def test_key_changes_when_the_arg_signature_changes(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "repo")
    assert _key(root, arg_signature="CRITICAL|advisory") != _key(
        root, arg_signature="CRITICAL|delta"
    )


def test_key_is_insensitive_to_checkout_path(tmp_path: Path) -> None:
    """Keys must hash repo-relative paths, never absolute ones."""
    a = _make_tree(tmp_path / "some" / "deep" / "lane")
    b = _make_tree(tmp_path / "b")
    assert _key(a) == _key(b)


# ---------------------------------------------------------------------------
# Cache entries: content-addressed filenames, atomic writes
# ---------------------------------------------------------------------------


def test_cache_entry_path_is_content_addressed(tmp_path: Path) -> None:
    one = gate.cache_entry_path(tmp_path, "a" * 64)
    two = gate.cache_entry_path(tmp_path, "b" * 64)
    assert one != two
    assert "a" * 64 in one.name
    assert one.parent == two.parent


def test_cache_entry_roundtrip_leaves_no_partial_files(tmp_path: Path) -> None:
    path = gate.cache_entry_path(tmp_path, "c" * 64)
    gate.write_cache_entry(path, returncode=1, output="findings\n")
    entry = gate.read_cache_entry(path)
    assert entry is not None
    assert entry.returncode == 1
    assert entry.output == "findings\n"
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_read_cache_entry_returns_none_for_a_torn_file(tmp_path: Path) -> None:
    path = gate.cache_entry_path(tmp_path, "d" * 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"returncode": 0, "outp')
    assert gate.read_cache_entry(path) is None


def test_read_cache_entry_returns_none_when_absent(tmp_path: Path) -> None:
    assert gate.read_cache_entry(tmp_path / "missing.json") is None


# ---------------------------------------------------------------------------
# Machine-wide serialization
# ---------------------------------------------------------------------------


def test_lock_is_exclusive_and_times_out(tmp_path: Path) -> None:
    lock = tmp_path / "scan.lock"
    with (
        gate.scan_lock(lock, timeout_s=0.2, poll_s=0.05),
        pytest.raises(gate.ScanLockTimeoutError),
        gate.scan_lock(lock, timeout_s=0.2, poll_s=0.05),
    ):
        pytest.fail("second holder must not acquire an exclusive lock")


def test_lock_is_reacquirable_after_release(tmp_path: Path) -> None:
    lock = tmp_path / "scan.lock"
    with gate.scan_lock(lock, timeout_s=0.2, poll_s=0.05):
        pass
    with gate.scan_lock(lock, timeout_s=0.2, poll_s=0.05):
        pass


# ---------------------------------------------------------------------------
# main(): hit / miss / fail-closed
# ---------------------------------------------------------------------------


class _RecordingSweep:
    def __init__(self, returncode: int, output: str) -> None:
        self.returncode = returncode
        self.output = output
        self.calls = 0

    def __call__(self, repo_root: Path, sweep_args: list[str]) -> tuple[int, str]:
        self.calls += 1
        return self.returncode, self.output


def _run_gate(
    tmp_path: Path, root: Path, sweep: _RecordingSweep, **overrides: Any
) -> int:
    kwargs: dict[str, Any] = {
        "repo_root": root,
        "cache_root": tmp_path / "cache",
        "lock_path": tmp_path / "cache" / "scan.lock",
        "run_sweep": sweep,
        "lock_timeout_s": 5.0,
    }
    kwargs.update(overrides)
    return int(gate.run_gate(**kwargs))


def test_cold_run_invokes_the_sweep_and_warm_run_does_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_tree(tmp_path / "repo")
    sweep = _RecordingSweep(0, '{"status": "clean"}')

    assert _run_gate(tmp_path, root, sweep) == 0
    assert sweep.calls == 1

    assert _run_gate(tmp_path, root, sweep) == 0
    assert sweep.calls == 1, "second run must be served from the cache"

    captured = capsys.readouterr()
    assert '{"status": "clean"}' in captured.out
    assert "cache hit" in captured.err


def test_cache_replays_the_blocking_verdict_not_just_success(tmp_path: Path) -> None:
    """A cached run that found new CRITICAL findings must still block."""
    root = _make_tree(tmp_path / "repo")
    sweep = _RecordingSweep(1, '{"status": "findings"}')

    assert _run_gate(tmp_path, root, sweep) == 1
    assert _run_gate(tmp_path, root, sweep) == 1
    assert sweep.calls == 1


def test_changed_source_invalidates_the_cache(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "repo")
    sweep = _RecordingSweep(0, '{"status": "clean"}')
    _run_gate(tmp_path, root, sweep)
    (root / "src" / "pkg" / "node_a" / "handler.py").write_text("import json\n")
    _run_gate(tmp_path, root, sweep)
    assert sweep.calls == 2


def test_engine_failure_is_never_cached(tmp_path: Path) -> None:
    """rc>=2 is an engine/infrastructure error, not a verdict — caching it is sticky."""
    root = _make_tree(tmp_path / "repo")
    sweep = _RecordingSweep(2, "ERROR: dep-health sweep engine failed")
    assert _run_gate(tmp_path, root, sweep) == 2
    assert _run_gate(tmp_path, root, sweep) == 2
    assert sweep.calls == 2


def test_lock_timeout_fails_closed_without_running_or_skipping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gate that cannot serialize must block the commit, never wave it through."""
    root = _make_tree(tmp_path / "repo")
    sweep = _RecordingSweep(0, '{"status": "clean"}')
    lock = tmp_path / "cache" / "scan.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    with gate.scan_lock(lock, timeout_s=5.0, poll_s=0.05):
        rc = _run_gate(tmp_path, root, sweep, lock_timeout_s=0.2)

    assert rc != 0, "fail-closed: lock exhaustion must not exit 0"
    assert sweep.calls == 0
    assert "could not acquire" in capsys.readouterr().err.lower()


def test_a_lane_that_waits_for_the_lock_gets_the_winners_cached_result(
    tmp_path: Path,
) -> None:
    """Double-checked locking: the queued lane re-reads the cache after acquiring."""
    root = _make_tree(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    winner = _RecordingSweep(1, '{"status": "findings"}')
    assert _run_gate(tmp_path, root, winner, cache_root=cache_root) == 1

    loser = _RecordingSweep(0, "must not run")
    assert _run_gate(tmp_path, root, loser, cache_root=cache_root) == 1
    assert loser.calls == 0


def test_advisory_phase_when_no_baseline_is_present(tmp_path: Path) -> None:
    """Phase 1 (no baseline committed) must not pass --delta-mode."""
    root = _make_tree(tmp_path / "repo")
    (root / ".onex_state" / "dep_health_baseline.json").unlink()

    seen: list[list[str]] = []

    def sweep(repo_root: Path, sweep_args: list[str]) -> tuple[int, str]:
        seen.append(sweep_args)
        return 0, "{}"

    assert _run_gate(tmp_path, root, sweep) == 0  # type: ignore[arg-type]
    assert "--delta-mode" not in seen[0]
    assert "--baseline-path" not in seen[0]


def test_delta_phase_when_a_baseline_is_present(tmp_path: Path) -> None:
    root = _make_tree(tmp_path / "repo")
    seen: list[list[str]] = []

    def sweep(repo_root: Path, sweep_args: list[str]) -> tuple[int, str]:
        seen.append(sweep_args)
        return 0, "{}"

    assert _run_gate(tmp_path, root, sweep) == 0  # type: ignore[arg-type]
    assert "--delta-mode" in seen[0]
    assert "--severity-threshold" in seen[0]
    assert "CRITICAL" in seen[0]


def test_cached_entry_records_the_key_it_was_written_under(tmp_path: Path) -> None:
    """A cache file whose recorded key disagrees with its name is not trusted."""
    root = _make_tree(tmp_path / "repo")
    cache_root = tmp_path / "cache"
    sweep = _RecordingSweep(0, '{"status": "clean"}')
    _run_gate(tmp_path, root, sweep, cache_root=cache_root)

    entries = list((cache_root / "entries").iterdir())
    assert len(entries) == 1
    payload = json.loads(entries[0].read_text())
    assert payload["key"] == entries[0].stem
