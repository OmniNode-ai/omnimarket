# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse verbose pre-commit output and append non-gating local observations.

OMN-17469 deliberately stops at a local JSONL sink.  It does not invoke a
network client, change a hook result, or read prior sink entries.  OMN-17470
will separately decide how a repository's commit and push wrappers invoke this
module.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NewType, TypedDict, cast

HookOutcome = Literal["Passed", "Failed", "Skipped"]
CacheState = Literal["cold", "warm", "unknown"]
EnvironmentFingerprint = NewType("EnvironmentFingerprint", str)

_RESULT = re.compile(
    r"^(?P<name>.*?)\.{3,}(?P<no_files>\(no files to check\))?(?P<outcome>Passed|Failed|Skipped)$"
)
_HOOK_ID = re.compile(r"^- hook id:\s*(?P<hook_id>\S+)$")
_DURATION = re.compile(r"^- duration:\s*(?P<duration>(?:0|[1-9]\d*)(?:\.\d+)?)s$")
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ENV_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CACHE_STATES = frozenset(("cold", "warm", "unknown"))
_HOOK_OUTCOMES = frozenset(("Passed", "Failed", "Skipped"))
_MAX_EINTR_RETRIES = 3
_ENVIRONMENT_KEYS = (
    "CI",
    "HOSTNAME",
    "PRE_COMMIT_HOME",
    "PYTHONPATH",
    "RUNNER_NAME",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
)


def _validate_cache_state(cache_state: object) -> CacheState:
    """Reject runtime values outside the closed cache-comparability contract."""
    if not isinstance(cache_state, str) or cache_state not in _CACHE_STATES:
        raise ValueError("cache_state must be cold, warm, or unknown")
    return cast(CacheState, cache_state)


def _validate_environment_fingerprint(
    env_fingerprint: object,
) -> EnvironmentFingerprint:
    """Require a real canonical SHA-256 fingerprint, never arbitrary objects."""
    if not isinstance(env_fingerprint, str) or not _ENV_FINGERPRINT.fullmatch(
        env_fingerprint
    ):
        raise ValueError("env_fingerprint must be sha256:<64 lowercase hex characters>")
    return EnvironmentFingerprint(env_fingerprint)


def _validate_files_changed(files_changed: object) -> int:
    """Keep a row's changed-file count a non-negative whole number."""
    if (
        isinstance(files_changed, bool)
        or not isinstance(files_changed, int)
        or files_changed < 0
    ):
        raise ValueError("files_changed must be a non-negative integer")
    return files_changed


def _normalize_timestamp(timestamp: object) -> datetime:
    """Reject ambiguous local time and retain every observation in UTC."""
    if not isinstance(timestamp, datetime):
        raise ValueError("timestamp must be timezone-aware")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(UTC)


def _retry_interrupted[Result](operation: Callable[[], Result]) -> Result:
    """Retry a bounded number of EINTR syscall interruptions without sleeping."""
    for attempt in range(_MAX_EINTR_RETRIES):
        try:
            return operation()
        except OSError as error:
            interrupted = (
                isinstance(error, InterruptedError) or error.errno == errno.EINTR
            )
            if not interrupted or attempt == _MAX_EINTR_RETRIES - 1:
                raise
    raise AssertionError("bounded EINTR retry loop did not return or raise")


def _close_once(descriptor: int) -> None:
    """Attempt descriptor cleanup once; retrying close after EINTR is unsafe on Linux."""
    try:
        os.close(descriptor)
    except OSError:
        return


def _open_directory_at(parent_descriptor: int, component: str) -> int:
    """Open or create one trusted directory component beneath a retained dirfd."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor: int | None = None
    try:
        descriptor = _retry_interrupted(
            lambda: os.open(component, flags, dir_fd=parent_descriptor)
        )
    except FileNotFoundError:
        with suppress(FileExistsError):
            _retry_interrupted(
                lambda: os.mkdir(component, mode=0o700, dir_fd=parent_descriptor)
            )
        descriptor = _retry_interrupted(
            lambda: os.open(component, flags, dir_fd=parent_descriptor)
        )
    try:
        assert descriptor is not None
        opened_descriptor = descriptor
        if not stat.S_ISDIR(
            _retry_interrupted(lambda: os.fstat(opened_descriptor)).st_mode
        ):
            raise OSError(f"unsafe hook-event sink parent component: {component}")
        retained_descriptor = descriptor
        descriptor = None
        return retained_descriptor
    except Exception:
        raise
    finally:
        if descriptor is not None:
            _close_once(descriptor)


def _open_safe_sink_parent(sink_path: Path) -> tuple[int, str]:
    """Return a parent dirfd and basename without resolving untrusted pathnames twice."""
    raw_path = os.fspath(sink_path)
    path = Path(raw_path)
    parts = path.parts
    if not parts or any(component == ".." for component in parts):
        raise OSError("unsafe hook-event sink path")

    is_absolute = os.path.isabs(raw_path)
    components = list(parts[1:] if is_absolute else parts)
    if not components or components[-1] in ("", "."):
        raise OSError("hook-event sink requires a filename")
    basename = components.pop()
    root = "/" if is_absolute else "."
    current_descriptor: int | None = _retry_interrupted(
        lambda: os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    )
    try:
        for component in components:
            assert current_descriptor is not None
            next_descriptor = _open_directory_at(current_descriptor, component)
            _close_once(current_descriptor)
            current_descriptor = next_descriptor
        assert current_descriptor is not None
        retained_descriptor = current_descriptor
        current_descriptor = None
        return retained_descriptor, basename
    except Exception:
        raise
    finally:
        if current_descriptor is not None:
            _close_once(current_descriptor)


def _assert_private_regular_descriptor(descriptor: int) -> None:
    """Defend against redirect races and keep accepted local sinks user-private."""
    if not stat.S_ISREG(_retry_interrupted(lambda: os.fstat(descriptor)).st_mode):
        raise OSError("hook-event sink descriptor is not a regular file")
    _retry_interrupted(lambda: os.fchmod(descriptor, 0o600))
    if stat.S_IMODE(_retry_interrupted(lambda: os.fstat(descriptor)).st_mode) != 0o600:
        raise OSError("hook-event sink mode is not private")


def _write_with_eintr(descriptor: int, payload: bytes) -> int:
    """Write one contiguous payload fragment with the bounded EINTR policy."""
    return _retry_interrupted(lambda: os.write(descriptor, payload))


class HookEventPayload(TypedDict):
    """Stable, JSON-serializable local sink payload."""

    hook_id: str
    outcome: HookOutcome
    duration_s: float | None
    stage: Literal["pre-commit", "pre-push"]
    repo: str
    config_sha: str
    cache_state: CacheState
    env_fingerprint: EnvironmentFingerprint
    files_changed: int
    ts: str


@dataclass(frozen=True)
class HookEventContext:
    """Metadata shared by every hook firing in one runner invocation.

    The wrapper owns cache classification and the environmental sample because
    it is the only layer that knows how the run began.  Making both fields
    required prevents a timing record without its comparability context.
    """

    stage: Literal["pre-commit", "pre-push"]
    repo: str
    config_path: Path
    cache_state: CacheState
    env_fingerprint: EnvironmentFingerprint
    files_changed: int
    timestamp: datetime

    def __post_init__(self) -> None:
        """Reject metadata that would make timing rows incomparable or ambiguous.

        ``cold`` and ``warm`` are caller-observed cache classifications;
        ``unknown`` records that no classification was observed. This sink never
        infers a state. The fingerprint is a SHA-256 digest, never raw runner
        environment data, and timestamps are normalized to UTC immediately.
        """
        object.__setattr__(self, "cache_state", _validate_cache_state(self.cache_state))
        object.__setattr__(
            self,
            "env_fingerprint",
            _validate_environment_fingerprint(self.env_fingerprint),
        )
        object.__setattr__(
            self, "files_changed", _validate_files_changed(self.files_changed)
        )
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))


@dataclass(frozen=True)
class HookEventRecord:
    """One immutable observation derived from pre-commit's own stdout."""

    hook_id: str
    outcome: HookOutcome
    duration_s: float | None
    stage: Literal["pre-commit", "pre-push"]
    repo: str
    config_sha: str
    cache_state: CacheState
    env_fingerprint: EnvironmentFingerprint
    files_changed: int
    timestamp: datetime
    non_execution_reason: Literal["no_files"] | None = None

    def __post_init__(self) -> None:
        """Enforce the same runtime metadata boundary as invocation context.

        ``duration_s=None`` is deliberately narrow: the parser supplies
        ``no_files`` solely for pre-commit's literal
        ``(no files to check)Skipped`` row. It is not a timing value and cannot
        represent an executed hook.
        """
        if not isinstance(self.outcome, str) or self.outcome not in _HOOK_OUTCOMES:
            raise ValueError("outcome must be Passed, Failed, or Skipped")
        if self.non_execution_reason not in (None, "no_files"):
            raise ValueError("non_execution_reason must be no_files when present")
        if self.duration_s is None:
            if self.outcome != "Skipped" or self.non_execution_reason != "no_files":
                raise ValueError("duration_s may be null only for no-files Skipped")
        else:
            if (
                isinstance(self.duration_s, bool)
                or not isinstance(self.duration_s, (int, float))
                or self.duration_s < 0
                or not math.isfinite(float(self.duration_s))
            ):
                raise ValueError("duration_s must be a finite non-negative number")
            if self.non_execution_reason is not None:
                raise ValueError(
                    "executed hook records cannot have non_execution_reason"
                )
            object.__setattr__(self, "duration_s", float(self.duration_s))
        object.__setattr__(self, "cache_state", _validate_cache_state(self.cache_state))
        object.__setattr__(
            self,
            "env_fingerprint",
            _validate_environment_fingerprint(self.env_fingerprint),
        )
        object.__setattr__(
            self, "files_changed", _validate_files_changed(self.files_changed)
        )
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))

    def as_json_dict(self) -> HookEventPayload:
        """Render the stable JSONL row shape used by the local sink.

        ``duration_s`` is always present. It is null only for pre-commit's
        literal ``(no files to check)Skipped`` non-execution record; every
        observed executed outcome carries the runner-measured duration.
        """
        timestamp = self.timestamp.astimezone(UTC).isoformat(timespec="seconds")
        return {
            "hook_id": self.hook_id,
            "outcome": self.outcome,
            "duration_s": self.duration_s,
            "stage": self.stage,
            "repo": self.repo,
            "config_sha": self.config_sha,
            "cache_state": self.cache_state,
            "env_fingerprint": self.env_fingerprint,
            "files_changed": self.files_changed,
            "ts": timestamp.replace("+00:00", "Z"),
        }


@dataclass
class _PendingHookEvent:
    outcome: HookOutcome
    no_files_to_check: bool
    hook_id: str | None = None
    duration_s: float | None = None


def environment_fingerprint(environment: Mapping[str, str]) -> EnvironmentFingerprint:
    """Return a privacy-preserving, stable fingerprint of runner conditions.

    The selected keys identify the relevant venv, cache, host/CI, and import
    context without storing their raw values in the JSONL record.
    """
    digest = hashlib.sha256()
    for key in _ENVIRONMENT_KEYS:
        value = environment.get(key, "")
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return EnvironmentFingerprint(f"sha256:{digest.hexdigest()}")


def git_blob_sha(path: Path) -> str:
    """Calculate the Git blob identity without shelling out to Git."""
    content = path.read_bytes()
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def parse_hook_events(
    lines: Iterable[str],
) -> tuple[tuple[str, HookOutcome, float | None], ...]:
    """Extract contiguous runner-shaped observations from verbose pre-commit output.

    ANSI CSI sequences are presentation only and are discarded before parsing.
    A durationless row is retained only for the explicit no-files marker: it
    records a non-execution, not a zero-duration execution.

    The runner writes the result, hook id, and duration contiguously before it
    prints arbitrary hook output.  Emit an executed observation as soon as the
    complete three-line runner sequence is seen, so later hook output cannot
    replace its metadata.  This is intentionally a local diagnostic parser:
    unframed stdout cannot distinguish a hook that counterfeits an entire
    runner-shaped sequence from the runner itself.
    """
    events: list[tuple[str, HookOutcome, float | None]] = []
    current: _PendingHookEvent | None = None
    expecting: Literal["hook_id", "duration", "next_result"] | None = None

    for raw_line in lines:
        line = _ANSI_CSI.sub("", raw_line).rstrip("\r\n")
        result_match = _RESULT.match(line)
        if result_match:
            if current is not None and expecting == "next_result":
                assert current.hook_id is not None
                events.append((current.hook_id, current.outcome, None))
            current = _PendingHookEvent(
                outcome=cast(HookOutcome, result_match["outcome"]),
                no_files_to_check=result_match["no_files"] is not None,
            )
            expecting = "hook_id"
            continue
        if current is None or expecting is None:
            continue

        if expecting == "hook_id":
            hook_id_match = _HOOK_ID.match(line)
            if hook_id_match:
                current.hook_id = hook_id_match["hook_id"]
                expecting = "next_result" if current.no_files_to_check else "duration"
            else:
                current = None
                expecting = None
            continue

        if expecting == "duration":
            duration_match = _DURATION.match(line)
            if duration_match:
                assert current.hook_id is not None
                events.append(
                    (
                        current.hook_id,
                        current.outcome,
                        float(duration_match["duration"]),
                    )
                )
            current = None
            expecting = None
            continue

        current = None
        expecting = None

    if current is not None and expecting == "next_result":
        assert current.hook_id is not None
        events.append((current.hook_id, current.outcome, None))
    return tuple(events)


def build_hook_event_records(
    lines: Iterable[str], context: HookEventContext
) -> tuple[HookEventRecord, ...]:
    """Attach required invocation metadata to parsed runner observations."""
    config_sha = git_blob_sha(context.config_path)
    return tuple(
        HookEventRecord(
            hook_id=hook_id,
            outcome=outcome,
            duration_s=duration_s,
            stage=context.stage,
            repo=context.repo,
            config_sha=config_sha,
            cache_state=context.cache_state,
            env_fingerprint=context.env_fingerprint,
            files_changed=context.files_changed,
            timestamp=context.timestamp,
            non_execution_reason="no_files" if duration_s is None else None,
        )
        for hook_id, outcome, duration_s in parse_hook_events(lines)
    )


def emit_hook_events(
    lines: Iterable[str], context: HookEventContext, sink_path: Path
) -> None:
    """Best-effort append of observations; telemetry failure is never a gate.

    The broad failure boundary is intentional: the caller may invoke this from
    a commit or push hook, where an unavailable state directory, corrupt input,
    or a full volume must leave the hook's independent verdict untouched.
    An uncontended sink appends every parsed record in one locked JSONL payload.
    A contended sink drops that payload promptly rather than delaying the hook.
    """
    try:
        records = build_hook_event_records(lines, context)
        if not records:
            return
        payload = b"".join(
            (json.dumps(record.as_json_dict(), sort_keys=True).encode("utf-8") + b"\n")
            for record in records
        )
        parent_descriptor: int | None = None
        descriptor: int | None = None
        locked = False
        lock_unavailable = False
        try:
            parent_descriptor, basename = _open_safe_sink_parent(sink_path)
            descriptor = _retry_interrupted(
                lambda: os.open(
                    basename,
                    os.O_APPEND
                    | os.O_CREAT
                    | os.O_WRONLY
                    | os.O_NONBLOCK
                    | os.O_NOFOLLOW,
                    mode=0o600,
                    dir_fd=parent_descriptor,
                )
            )
            _assert_private_regular_descriptor(descriptor)
            try:
                _retry_interrupted(
                    lambda: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                )
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    lock_unavailable = True
                else:
                    raise
            if not lock_unavailable:
                locked = True
                remaining = payload
                while remaining:
                    written = _write_with_eintr(descriptor, remaining)
                    if written <= 0:
                        raise OSError("hook-event sink write made no progress")
                    remaining = remaining[written:]
        finally:
            if descriptor is not None:
                try:
                    if locked:
                        _retry_interrupted(
                            lambda: fcntl.flock(descriptor, fcntl.LOCK_UN)
                        )
                finally:
                    _close_once(descriptor)
            if parent_descriptor is not None:
                _close_once(parent_descriptor)
    except Exception:
        return
