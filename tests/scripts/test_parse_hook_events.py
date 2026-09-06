# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""RED-first contract for the OMN-17469 local hook-outcome sink.

The foundation is deliberately independent of a git hook.  OMN-17470 owns
that wrapper wiring; this suite proves the reusable parser and local writer
cannot affect a caller's gate result in the meantime.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import json
import multiprocessing
import os
import socket
import stat
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

import scripts.hooks.parse_hook_events as hook_event_sink
from scripts.hooks.parse_hook_events import (
    CacheState,
    HookEventContext,
    HookEventRecord,
    build_hook_event_records,
    emit_hook_events,
    environment_fingerprint,
    parse_hook_events,
)

_VERBOSE_OUTPUT = """ruff format........................................................Passed
- hook id: ruff-format
- duration: 0.03s
mypy.......................................................................Failed
- hook id: mypy-type-check
- duration: 33.77s
"""


def _context(tmp_path: Path) -> HookEventContext:
    config_path = tmp_path / ".pre-commit-config.yaml"
    config_path.write_text("default_stages: [pre-commit]\n", encoding="utf-8")
    return HookEventContext(
        stage="pre-commit",
        repo="omnimarket",
        config_path=config_path,
        cache_state="warm",
        env_fingerprint=environment_fingerprint({"CI": "test"}),
        files_changed=3,
        timestamp=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )


def test_records_preserve_the_full_observation_shape(tmp_path: Path) -> None:
    """Every firing must retain the fields needed for later attribution."""
    records = build_hook_event_records(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path))

    assert [record.hook_id for record in records] == ["ruff-format", "mypy-type-check"]
    assert [record.outcome for record in records] == ["Passed", "Failed"]
    assert [record.duration_s for record in records] == [0.03, 33.77]

    payload = records[0].as_json_dict()
    assert payload["stage"] == "pre-commit"
    assert payload["repo"] == "omnimarket"
    assert isinstance(payload["config_sha"], str)
    assert len(payload["config_sha"]) == 40
    assert payload["cache_state"] == "warm"
    assert payload["env_fingerprint"].startswith("sha256:")
    assert payload["files_changed"] == 3
    assert payload["ts"] == "2026-09-01T09:00:00Z"


def test_emission_is_a_local_append_and_does_not_read_existing_sink(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Malformed historic lines cannot be consulted by a new observation."""
    sink_path = tmp_path / ".onex_state" / "hook_events.jsonl"
    sink_path.parent.mkdir()
    sink_path.write_text(
        "not json and intentionally unreadable as a record\n", encoding="utf-8"
    )

    def _forbid_read_text(_: Path, *args: object, **kwargs: object) -> str:
        raise AssertionError("the hook-event sink must never be read on a gate path")

    monkeypatch.setattr(Path, "read_text", _forbid_read_text)
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    rows = sink_path.read_bytes().decode("utf-8").splitlines()
    assert len(rows) == 3
    assert json.loads(rows[-1])["hook_id"] == "mypy-type-check"


def test_broken_sink_cannot_change_a_caller_verdict(tmp_path: Path) -> None:
    """A deliberately unwritable sink is telemetry-only, never a gate."""
    broken_sink = tmp_path / "hook-events-is-a-directory"
    broken_sink.mkdir()

    gate_verdict = 17
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), broken_sink)

    assert gate_verdict == 17


def test_emission_never_opens_a_network_socket(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The foundation can only use local filesystem operations."""

    def _forbid_socket(*args: object, **kwargs: object) -> Any:
        raise AssertionError("hook-event emission must never open a network socket")

    monkeypatch.setattr(socket, "socket", _forbid_socket)
    emit_hook_events(
        _VERBOSE_OUTPUT.splitlines(),
        _context(tmp_path),
        tmp_path / ".onex_state" / "hook_events.jsonl",
    )


def test_sink_module_has_no_network_or_subprocess_dependency() -> None:
    """Keep the no-network guarantee structural as well as behavioral."""
    module_path = (
        Path(__file__).parents[2] / "scripts" / "hooks" / "parse_hook_events.py"
    )
    module = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(module)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not imported_roots.intersection(
        {"httpx", "requests", "socket", "subprocess", "urllib"}
    )


# =============================================================================
# OMN-17469 review regressions
# =============================================================================


def test_parser_accepts_ansi_colored_real_verbose_output(tmp_path: Path) -> None:
    """Terminal SGR colour must not change the runner's observed outcome."""
    colored_output = (
        "ruff format........................................\x1b[32mPassed\x1b[0m\n"
        "- hook id: ruff-format\n"
        "- duration: 0.03s\n"
    )

    records = build_hook_event_records(colored_output.splitlines(), _context(tmp_path))

    assert [
        (record.hook_id, record.outcome, record.duration_s) for record in records
    ] == [("ruff-format", "Passed", 0.03)]


def test_durationless_no_files_skipped_is_retained_without_invented_timing(
    tmp_path: Path,
) -> None:
    """A skipped hook did not execute, so no duration measurement exists."""
    no_files_output = (
        "validate docs............................(no files to check)Skipped\n"
        "- hook id: validate-docs\n"
    )

    assert parse_hook_events(no_files_output.splitlines()) == (
        ("validate-docs", "Skipped", None),
    )

    record = build_hook_event_records(no_files_output.splitlines(), _context(tmp_path))[
        0
    ]
    payload = record.as_json_dict()
    assert "duration_s" in payload
    assert payload["duration_s"] is None


def test_durationless_executed_outcomes_are_not_represented_as_null() -> None:
    """Only pre-commit's no-files marker denotes a non-execution."""
    durationless_executed_output = (
        "conditional validation........................................Skipped\n"
        "- hook id: conditional-validation\n"
    )

    assert parse_hook_events(durationless_executed_output.splitlines()) == ()


def test_parser_emits_before_later_hook_output_can_overwrite_metadata() -> None:
    """A completed runner record is immutable when a hook prints lookalike text."""
    output = """first hook........................................................Passed
- hook id: first-hook
- duration: 0.03s
hook stdout: - hook id: forged-hook
- duration: 999s
second hook......................................................Failed
- hook id: second-hook
- duration: 4.2s
"""

    assert parse_hook_events(output.splitlines()) == (
        ("first-hook", "Passed", 0.03),
        ("second-hook", "Failed", 4.2),
    )


def test_parser_discards_interrupted_metadata_without_losing_next_valid_record() -> (
    None
):
    """Noise between runner metadata cannot create or poison an adjacent row."""
    output = """broken hook.......................................................Passed
- hook id: broken-hook
hook stdout between runner lines
- duration: 3.1s
valid hook........................................................Passed
- hook id: valid-hook
- duration: 1.5s
"""

    assert parse_hook_events(output.splitlines()) == (("valid-hook", "Passed", 1.5),)


def test_parser_drops_result_shaped_hook_stdout_without_contiguous_metadata() -> None:
    """A hook's result-looking text alone cannot become a local observation."""
    output = """valid hook........................................................Passed
- hook id: valid-hook
- duration: 0.1s
hook stdout.......................................................Passed
"""

    assert parse_hook_events(output.splitlines()) == (("valid-hook", "Passed", 0.1),)


@pytest.mark.parametrize("duration", [".5", "1.", "01", "1e3", "1.2.3", "NaN"])
def test_parser_rejects_malformed_duration_without_losing_next_valid_record(
    duration: str,
) -> None:
    """Only the runner's plain non-negative decimal timing grammar is accepted."""
    output = f"""malformed duration.................................................Passed
- hook id: malformed-duration
- duration: {duration}s
valid hook........................................................Passed
- hook id: valid-hook
- duration: 0s
"""

    assert parse_hook_events(output.splitlines()) == (("valid-hook", "Passed", 0.0),)


def test_aware_timestamps_normalize_to_utc_and_naive_timestamps_refuse(
    tmp_path: Path,
) -> None:
    """Rows have a single unambiguous instant; naive local time is rejected."""
    context = _context(tmp_path)
    offset_context = HookEventContext(
        stage=context.stage,
        repo=context.repo,
        config_path=context.config_path,
        cache_state=context.cache_state,
        env_fingerprint=context.env_fingerprint,
        files_changed=context.files_changed,
        timestamp=datetime(2026, 9, 1, 5, 0, tzinfo=timezone(timedelta(hours=-4))),
    )
    record = build_hook_event_records(_VERBOSE_OUTPUT.splitlines(), offset_context)[0]

    assert record.as_json_dict()["ts"] == "2026-09-01T09:00:00Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        HookEventContext(
            stage=context.stage,
            repo=context.repo,
            config_path=context.config_path,
            cache_state=context.cache_state,
            env_fingerprint=context.env_fingerprint,
            files_changed=context.files_changed,
            timestamp=datetime(2026, 9, 1, 9, 0),
        )


@pytest.mark.parametrize("cache_state", ["", "hot", "WARM"])
def test_context_rejects_unknown_cache_state(tmp_path: Path, cache_state: str) -> None:
    """Cache state is a closed comparability label, not arbitrary text."""
    context = _context(tmp_path)

    with pytest.raises(ValueError, match="cache_state"):
        HookEventContext(
            stage=context.stage,
            repo=context.repo,
            config_path=context.config_path,
            cache_state=cast(CacheState, cache_state),
            env_fingerprint=context.env_fingerprint,
            files_changed=context.files_changed,
            timestamp=context.timestamp,
        )


@pytest.mark.parametrize(
    "fingerprint", ["environment-under-test", "sha256:too-short", "sha512:" + "0" * 64]
)
def test_context_rejects_noncanonical_environment_fingerprints(
    tmp_path: Path, fingerprint: str
) -> None:
    """The fingerprint must be the fixed privacy-preserving SHA-256 shape."""
    context = _context(tmp_path)

    with pytest.raises(ValueError, match="env_fingerprint"):
        HookEventContext(
            stage=context.stage,
            repo=context.repo,
            config_path=context.config_path,
            cache_state=context.cache_state,
            env_fingerprint=hook_event_sink.EnvironmentFingerprint(fingerprint),
            files_changed=context.files_changed,
            timestamp=context.timestamp,
        )


def _emit_from_another_process(config_path: str, sink_path: str, worker: int) -> None:
    """Write a distinct row from a fresh process for append-lock coverage."""
    context = HookEventContext(
        stage="pre-commit",
        repo=f"omnimarket-worker-{worker}",
        config_path=Path(config_path),
        cache_state="unknown",
        env_fingerprint=environment_fingerprint({"CI": str(worker)}),
        files_changed=worker,
        timestamp=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), context, Path(sink_path))


def _hold_sink_lock(
    sink_path: str,
    lock_acquired: multiprocessing.synchronize.Event,
    release_lock: multiprocessing.synchronize.Event,
) -> None:
    """Hold an advisory lock in another process until the parent releases it."""
    descriptor = os.open(sink_path, os.O_CREAT | os.O_RDWR, mode=0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        lock_acquired.set()
        release_lock.wait(timeout=30)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_sink_keeps_jsonl_rows_valid_under_cross_process_appends(
    tmp_path: Path,
) -> None:
    """Uncontended winners emit every parsed row; lock contention may drop a payload."""
    config_path = tmp_path / ".pre-commit-config.yaml"
    config_path.write_text("default_stages: [pre-commit]\n", encoding="utf-8")
    sink_path = tmp_path / ".onex_state" / "hook_events.jsonl"
    workers = [
        multiprocessing.Process(
            target=_emit_from_another_process,
            args=(str(config_path), str(sink_path), worker),
        )
        for worker in range(8)
    ]

    for process in workers:
        process.start()
    for process in workers:
        process.join(timeout=20)

    assert all(process.exitcode == 0 for process in workers)
    rows = [
        json.loads(line) for line in sink_path.read_text(encoding="utf-8").splitlines()
    ]
    assert 0 < len(rows) <= 16
    expected_repos = {f"omnimarket-worker-{worker}" for worker in range(8)}
    assert {row["repo"] for row in rows}.issubset(expected_repos)
    # A retained writer always carries both parsed records; only a whole
    # contended payload may be dropped by the nonblocking flock policy.
    assert all(count == 2 for count in Counter(row["repo"] for row in rows).values())


def test_sink_retries_short_writes_until_the_entire_jsonl_row_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial OS write cannot leave a truncated event or change the gate."""
    real_write = os.write

    def _short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:7])

    monkeypatch.setattr(os, "write", _short_write)
    sink_path = tmp_path / ".onex_state" / "hook_events.jsonl"

    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    rows = [
        json.loads(line) for line in sink_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["hook_id"] for row in rows] == ["ruff-format", "mypy-type-check"]


# =============================================================================
# OMN-17469 second-review sink hardening regressions
# =============================================================================


def test_sink_rejects_a_final_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    """A telemetry path must not redirect an append into another regular file."""
    target = tmp_path / "protected.jsonl"
    target.write_text("protected\n", encoding="utf-8")
    sink_path = tmp_path / "hook_events.jsonl"
    sink_path.symlink_to(target)

    gate_verdict = 17
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert gate_verdict == 17
    assert target.read_text(encoding="utf-8") == "protected\n"


def test_sink_rejects_a_symlinked_parent_without_creating_the_redirected_file(
    tmp_path: Path,
) -> None:
    """A path component redirect is as unsafe as a symlinked final filename."""
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir()
    redirected_parent = tmp_path / "redirected-parent"
    redirected_parent.symlink_to(target_parent, target_is_directory=True)
    sink_path = redirected_parent / "hook_events.jsonl"

    gate_verdict = 17
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert gate_verdict == 17
    assert not (target_parent / "hook_events.jsonl").exists()


def test_sink_parent_swap_cannot_redirect_the_final_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent swapped after dirfd traversal cannot redirect the final append."""
    trusted_parent = tmp_path / "trusted-parent"
    trusted_parent.mkdir()
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir()
    held_parent = tmp_path / "held-parent"
    sink_path = trusted_parent / "hook_events.jsonl"
    real_open = os.open
    swapped = False

    def _swap_parent_before_final_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "hook_events.jsonl" and dir_fd is not None and not swapped:
            trusted_parent.rename(held_parent)
            trusted_parent.symlink_to(target_parent, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", _swap_parent_before_final_open)
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert swapped
    assert not (target_parent / "hook_events.jsonl").exists()
    assert (
        len(
            (held_parent / "hook_events.jsonl").read_text(encoding="utf-8").splitlines()
        )
        == 2
    )


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_sink_rejects_non_regular_targets_without_changing_the_gate(
    tmp_path: Path, kind: str
) -> None:
    """Directories and FIFOs must fail closed without a blocking open."""
    sink_path = tmp_path / "hook_events"
    if kind == "directory":
        sink_path.mkdir()
    else:
        os.mkfifo(sink_path)

    gate_verdict = 17
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert gate_verdict == 17


def test_sink_corrects_existing_permissive_regular_file_mode(tmp_path: Path) -> None:
    """An accepted regular sink is private to the local user after every append."""
    sink_path = tmp_path / "hook_events.jsonl"
    sink_path.write_text("", encoding="utf-8")
    sink_path.chmod(0o644)

    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert stat.S_IMODE(sink_path.stat().st_mode) == 0o600
    assert len(sink_path.read_text(encoding="utf-8").splitlines()) == 2


def test_sink_appends_to_a_regular_file(tmp_path: Path) -> None:
    """Hardening preserves ordinary append-only local telemetry."""
    sink_path = tmp_path / "hook_events.jsonl"
    sink_path.write_text("historic\n", encoding="utf-8")

    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert sink_path.read_text(encoding="utf-8").startswith("historic\n")
    assert len(sink_path.read_text(encoding="utf-8").splitlines()) == 3


def test_sink_retries_interrupted_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient EINTR while opening remains invisible to the hook verdict."""
    real_open = os.open
    attempts = 0

    def _interrupted_open(path: Path | str, flags: int, mode: int = 0o777) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InterruptedError(errno.EINTR, "interrupted")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _interrupted_open)
    gate_verdict = 17
    emit_hook_events(
        _VERBOSE_OUTPUT.splitlines(), _context(tmp_path), tmp_path / "events"
    )

    assert gate_verdict == 17
    assert attempts == 2


def test_sink_retries_interrupted_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient lock interruption does not discard a usable local event."""
    real_flock = fcntl.flock
    attempts = 0

    def _interrupted_lock(fd: int, operation: int) -> None:
        nonlocal attempts
        if operation & fcntl.LOCK_EX:
            attempts += 1
            if attempts == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", _interrupted_lock)
    sink_path = tmp_path / "events"
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert attempts == 2
    assert len(sink_path.read_text(encoding="utf-8").splitlines()) == 2


def test_sink_retries_interrupted_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient interrupted write completes the full JSONL append."""
    real_write = os.write
    attempts = 0

    def _interrupted_write(fd: int, data: bytes) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InterruptedError(errno.EINTR, "interrupted")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _interrupted_write)
    sink_path = tmp_path / "events"
    emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)

    assert attempts >= 2
    assert len(sink_path.read_text(encoding="utf-8").splitlines()) == 2


def test_sink_retries_interrupted_unlock_and_still_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlock retry is bounded and cannot prevent descriptor cleanup."""
    real_flock = fcntl.flock
    real_close = os.close
    unlock_attempts = 0
    closed = False

    def _interrupted_unlock(fd: int, operation: int) -> None:
        nonlocal unlock_attempts
        if operation == fcntl.LOCK_UN:
            unlock_attempts += 1
            if unlock_attempts == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
        real_flock(fd, operation)

    def _track_close(fd: int) -> None:
        nonlocal closed
        closed = True
        real_close(fd)

    monkeypatch.setattr(fcntl, "flock", _interrupted_unlock)
    monkeypatch.setattr(os, "close", _track_close)
    emit_hook_events(
        _VERBOSE_OUTPUT.splitlines(), _context(tmp_path), tmp_path / "events"
    )

    assert unlock_attempts == 2
    assert closed


def test_sink_attempts_interrupted_close_once_without_reusing_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux may close an EINTR descriptor, so cleanup must not retry it."""
    real_close = os.close
    attempts = 0

    def _interrupted_close(fd: int) -> None:
        nonlocal attempts
        attempts += 1
        real_close(fd)
        raise InterruptedError(errno.EINTR, "interrupted")

    descriptor = os.open(tmp_path / "events", os.O_CREAT | os.O_RDWR, mode=0o600)
    monkeypatch.setattr(os, "close", _interrupted_close)
    hook_event_sink._close_once(descriptor)

    assert attempts == 1


def test_sink_unlock_error_still_attempts_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal unlock failure cannot skip descriptor cleanup."""
    real_flock = fcntl.flock
    real_close = os.close
    closed = False

    def _failing_unlock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "unlock failed")
        real_flock(fd, operation)

    def _track_close(fd: int) -> None:
        nonlocal closed
        closed = True
        real_close(fd)

    monkeypatch.setattr(fcntl, "flock", _failing_unlock)
    monkeypatch.setattr(os, "close", _track_close)
    emit_hook_events(
        _VERBOSE_OUTPUT.splitlines(), _context(tmp_path), tmp_path / "events"
    )

    assert closed


def test_held_sink_lock_drops_telemetry_without_blocking(
    tmp_path: Path,
) -> None:
    """A held external lock drops a payload before its owner releases it.

    The generous timeout protects the test harness from a stuck implementation
    without treating scheduler delay under a loaded CI machine as hook latency.
    The lock holder stays alive until after ``emit_hook_events`` returns, which
    directly proves the writer did not wait to acquire its advisory lock.
    """
    sink_path = tmp_path / "hook_events.jsonl"
    lock_acquired = multiprocessing.Event()
    release_lock = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_sink_lock,
        args=(str(sink_path), lock_acquired, release_lock),
    )
    emission_finished = threading.Event()
    emitter: threading.Thread | None = None

    def _emit() -> None:
        emit_hook_events(_VERBOSE_OUTPUT.splitlines(), _context(tmp_path), sink_path)
        emission_finished.set()

    holder.start()
    try:
        assert lock_acquired.wait(timeout=5)
        emitter = threading.Thread(target=_emit)
        emitter.start()
        assert emission_finished.wait(timeout=5)
        emitter.join(timeout=1)
        assert not emitter.is_alive()
        assert holder.is_alive()
        assert sink_path.read_text(encoding="utf-8") == ""
    finally:
        release_lock.set()
        if emitter is not None:
            emitter.join(timeout=5)
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)


def test_sink_abandons_a_zero_progress_write_without_changing_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-length write is a sink failure, never an endless hook-path loop."""
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    gate_verdict = 17

    emit_hook_events(
        _VERBOSE_OUTPUT.splitlines(), _context(tmp_path), tmp_path / "events"
    )

    assert gate_verdict == 17


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("outcome", "invalid", "outcome"),
        ("cache_state", "hot", "cache_state"),
        ("env_fingerprint", "not-a-sha", "env_fingerprint"),
        ("env_fingerprint", b"sha256:" + b"0" * 64, "env_fingerprint"),
        ("files_changed", -1, "files_changed"),
        ("timestamp", datetime(2026, 9, 1, 9, 0), "timezone-aware"),
    ],
)
def test_record_rejects_invalid_runtime_metadata(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    """Records validate the same runtime boundary as their context."""
    context = _context(tmp_path)
    fields: dict[str, object] = {
        "hook_id": "ruff-format",
        "outcome": "Passed",
        "duration_s": 0.03,
        "stage": context.stage,
        "repo": context.repo,
        "config_sha": "0" * 40,
        "cache_state": context.cache_state,
        "env_fingerprint": context.env_fingerprint,
        "files_changed": context.files_changed,
        "timestamp": context.timestamp,
    }
    fields[field] = value

    with pytest.raises(ValueError, match=match):
        HookEventRecord(**cast(Any, fields))


def test_record_normalizes_aware_timestamp_and_rejects_invalid_duration_semantics(
    tmp_path: Path,
) -> None:
    """Only the exact runner no-files non-execution can carry a null duration."""
    context = _context(tmp_path)
    fields: dict[str, object] = {
        "hook_id": "ruff-format",
        "outcome": "Passed",
        "duration_s": 0.03,
        "stage": context.stage,
        "repo": context.repo,
        "config_sha": "0" * 40,
        "cache_state": context.cache_state,
        "env_fingerprint": context.env_fingerprint,
        "files_changed": context.files_changed,
        "timestamp": datetime(2026, 9, 1, 5, 0, tzinfo=timezone(timedelta(hours=-4))),
    }

    record = HookEventRecord(**cast(Any, fields))
    assert record.as_json_dict()["ts"] == "2026-09-01T09:00:00Z"

    for duration_s in (None, -0.01):
        invalid_fields = {**fields, "duration_s": duration_s}
        with pytest.raises(ValueError, match="duration"):
            HookEventRecord(**cast(Any, invalid_fields))

    no_files_fields = {
        **fields,
        "outcome": "Skipped",
        "duration_s": None,
        "non_execution_reason": "no_files",
    }
    no_files_record = HookEventRecord(**cast(Any, no_files_fields))
    assert no_files_record.as_json_dict()["duration_s"] is None
