# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a change that adds a test file the committed durations record does not hold.

A change that adds a test file must record it in ``config/test_file_durations.json`` in the
same change so the record cannot drift from the tree; the freshness test
``test_committed_record_leaves_fewer_than_fifty_test_files_unrecorded`` stays as the backstop.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from ..adapters.policy_file import load_policy
from ..models.model_test_duration_record_check import (
    EnumTestDurationRecordOperation,
    ModelTestDurationRecordCheckRequest,
    ModelTestDurationRecordCheckResult,
    ModelTestDurationRecordPolicy,
)
from ..protocols.protocol_file_timer import ProtocolFileTimer
from ..protocols.protocol_tree_snapshot_source import ProtocolTreeSnapshotSource

TAG = "test-duration-record"


def is_collected_test_file(path: str, policy: ModelTestDurationRecordPolicy) -> bool:
    """Return whether ``path`` is a test file pytest would collect under the policy."""
    prefix = policy.tests_dir + "/"
    if not path.startswith(prefix):
        return False
    relative = path[len(prefix) :]
    parts = relative.split("/")
    for part in parts[:-1]:
        for pattern in policy.norecurse_patterns:
            if fnmatch.fnmatchcase(part, pattern):
                return False
    name = parts[-1]
    return any(
        fnmatch.fnmatchcase(name, pattern) for pattern in policy.test_file_patterns
    )


class HandlerTestDurationRecordCheck:
    """Check that added test files are recorded, or time and record the named files."""

    def __init__(
        self,
        policy: ModelTestDurationRecordPolicy | None = None,
        timer: ProtocolFileTimer | None = None,
        snapshots: ProtocolTreeSnapshotSource | None = None,
    ) -> None:
        self._policy = policy
        self._timer = timer
        self._snapshots = snapshots

    def handle(
        self, request: ModelTestDurationRecordCheckRequest
    ) -> ModelTestDurationRecordCheckResult:
        """Run the check or record operation and return the verdict."""
        policy = self._policy if self._policy is not None else load_policy()
        if request.operation is EnumTestDurationRecordOperation.RECORD:
            return self._record(request, policy)
        if request.operation is EnumTestDurationRecordOperation.FRESHNESS:
            return self._freshness(request, policy)
        return self._check(request, policy)

    def _check(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
    ) -> ModelTestDurationRecordCheckResult:
        """Refuse any added test file the record does not hold."""
        record = self._read_record(request, policy)
        if isinstance(record, ModelTestDurationRecordCheckResult):
            return record
        collected = self._collected(request, policy)
        unrecorded = tuple(f for f in collected if f not in record)
        if unrecorded:
            lines = [
                f"{TAG}: {f} is a new test file and is not in {policy.record_path}"
                for f in unrecorded
            ]
            lines.append(f"{TAG}: record it with:")
            lines.append(policy.record_command.replace("{files}", " ".join(unrecorded)))
            return ModelTestDurationRecordCheckResult(
                admitted=False,
                exit_code=1,
                added_test_files=len(collected),
                unrecorded=unrecorded,
                recorded=(),
                output_lines=tuple(lines),
            )
        return ModelTestDurationRecordCheckResult(
            admitted=True,
            exit_code=0,
            added_test_files=len(collected),
            unrecorded=(),
            recorded=(),
            output_lines=(
                f"{TAG}: {len(collected)} added test file(s), all recorded in {policy.record_path}",
            ),
        )

    def _record(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
    ) -> ModelTestDurationRecordCheckResult:
        """Time the named files and write their entries into the record."""
        collected = self._collected(request, policy)
        if not collected:
            return ModelTestDurationRecordCheckResult(
                admitted=False,
                exit_code=1,
                added_test_files=0,
                unrecorded=(),
                recorded=(),
                output_lines=(f"{TAG}: no collectable test file named to record",),
            )
        if self._timer is None:
            return ModelTestDurationRecordCheckResult(
                admitted=False,
                exit_code=1,
                added_test_files=len(collected),
                unrecorded=(),
                recorded=(),
                output_lines=(f"{TAG}: no timer composed; refusing to record",),
            )
        record = self._read_record(request, policy)
        if isinstance(record, ModelTestDurationRecordCheckResult):
            return record
        lines: list[str] = []
        recorded: list[str] = []
        for file in collected:
            seconds = self._timer.measure(request.repo_root, file)
            if seconds is None:
                return ModelTestDurationRecordCheckResult(
                    admitted=False,
                    exit_code=1,
                    added_test_files=len(collected),
                    unrecorded=(),
                    recorded=(),
                    output_lines=(
                        f"{TAG}: could not time {file}; its tests fail or did not run; nothing recorded",
                    ),
                )
            value = max(round(seconds, policy.decimals), policy.min_seconds)
            record[file] = value
            lines.append(f"{TAG}: recorded {file} {value} s")
            recorded.append(file)
        if request.sidecar:
            for file in recorded:
                self._write_sidecar(request, policy, file, record[file])
        else:
            record_path = Path(request.repo_root) / policy.record_path
            record_path.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return ModelTestDurationRecordCheckResult(
            admitted=True,
            exit_code=0,
            added_test_files=len(collected),
            unrecorded=(),
            recorded=tuple(recorded),
            output_lines=tuple(lines),
        )

    def _read_record(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
    ) -> dict[str, float] | ModelTestDurationRecordCheckResult:
        """Return the record dict, or a refusal result when it cannot be read."""
        record_path = Path(request.repo_root) / policy.record_path
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return ModelTestDurationRecordCheckResult(
                admitted=False,
                exit_code=2,
                added_test_files=0,
                unrecorded=(),
                recorded=(),
                output_lines=(f"{TAG}: cannot read {policy.record_path}: {error}",),
            )
        if not isinstance(data, dict):
            return ModelTestDurationRecordCheckResult(
                admitted=False,
                exit_code=2,
                added_test_files=0,
                unrecorded=(),
                recorded=(),
                output_lines=(
                    f"{TAG}: cannot read {policy.record_path}: not a JSON object",
                ),
            )
        merged: dict[str, float] = {}
        for entry_file in sorted(
            (Path(request.repo_root) / policy.record_dir).glob("*.json")
        ):
            try:
                entry = json.loads(entry_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                return self._unreadable(
                    f"{policy.record_dir}/{entry_file.name}", str(error)
                )
            if not isinstance(entry, dict):
                return self._unreadable(
                    f"{policy.record_dir}/{entry_file.name}", "not a JSON object"
                )
            merged.update(entry)
        merged.update(data)
        return merged

    def _unreadable(self, name: str, reason: str) -> ModelTestDurationRecordCheckResult:
        """Return the typed refusal for a record file that cannot be read."""
        return ModelTestDurationRecordCheckResult(
            admitted=False,
            exit_code=2,
            added_test_files=0,
            unrecorded=(),
            recorded=(),
            output_lines=(f"{TAG}: cannot read {name}: {reason}",),
        )

    def _write_sidecar(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
        file: str,
        seconds: float,
    ) -> None:
        """Write one per-file entry; its own file, so no two changes touch the same path."""
        directory = Path(request.repo_root) / policy.record_dir
        directory.mkdir(parents=True, exist_ok=True)
        entry = directory / (file.replace("/", "__").removesuffix(".py") + ".json")
        entry.write_text(
            json.dumps({file: seconds}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _unrecorded_at(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
        sha: str,
    ) -> tuple[str, ...] | None:
        """Return the test files commit ``sha`` holds that its record does not, or ``None`` when unreadable."""
        assert self._snapshots is not None
        files = self._snapshots.test_tree_files(request.repo_root, sha)
        record = self._snapshots.record_entries(request.repo_root, sha)
        if files is None or record is None:
            return None
        return tuple(
            sorted(
                f
                for f in files
                if is_collected_test_file(f, policy) and f not in record
            )
        )

    def _freshness(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
    ) -> ModelTestDurationRecordCheckResult:
        """Walk a push's commits oldest first and name the first one that reaches the limit."""
        limit = (
            request.limit if request.limit is not None else policy.max_unrecorded_files
        )
        if self._snapshots is None or not request.commits:
            return self._unreadable(
                "the push's commits", "no snapshot source or no commits given"
            )
        previous_sha = request.before_sha
        previous: tuple[str, ...] | None = None
        if previous_sha is not None:
            previous = self._unrecorded_at(request, policy, previous_sha)
        lines: list[str] = []
        crossing: str | None = None
        current: tuple[str, ...] = ()
        for sha in request.commits:
            counted = self._unrecorded_at(request, policy, sha)
            if counted is None:
                return self._unreadable(
                    f"commit {sha}", "its tests or record could not be read"
                )
            current = counted
            if len(current) >= limit and crossing is None:
                if previous is not None and len(previous) >= limit:
                    lines.append(
                        f"{TAG}: {len(previous)} test files were already unrecorded before this push "
                        f"(limit {limit}); first commit read over the limit is {sha} at {len(current)}"
                    )
                    break
                crossing = sha
                added = [f for f in current if previous is None or f not in previous]
                from_count = (
                    "an unread count" if previous is None else str(len(previous))
                )
                lines.append(
                    f"{TAG}: commit {sha} takes the unrecorded test files from {from_count} to "
                    f"{len(current)} (limit {limit}); it added: {', '.join(added) if added else 'none'}"
                )
                break
            previous = current
        if len(current) < limit:
            return ModelTestDurationRecordCheckResult(
                admitted=True,
                exit_code=0,
                added_test_files=0,
                unrecorded=current,
                recorded=(),
                output_lines=(
                    f"{TAG}: {len(current)} unrecorded test files at {request.commits[-1]} (limit {limit})",
                ),
            )
        lines.append(f"{TAG}: record the files with:")
        lines.append(policy.record_command.replace("{files}", " ".join(current)))
        return ModelTestDurationRecordCheckResult(
            admitted=False,
            exit_code=1,
            added_test_files=0,
            unrecorded=current,
            recorded=(),
            output_lines=tuple(lines),
            crossing_commit=crossing,
        )

    def _collected(
        self,
        request: ModelTestDurationRecordCheckRequest,
        policy: ModelTestDurationRecordPolicy,
    ) -> tuple[str, ...]:
        """Return the de-duplicated, sorted added files that are collectable test files."""
        collected = sorted(
            {
                file
                for file in request.added_files
                if is_collected_test_file(file, policy)
                and (Path(request.repo_root) / file).is_file()
            }
        )
        return tuple(collected)
