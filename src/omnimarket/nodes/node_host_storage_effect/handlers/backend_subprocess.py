# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real storage backend: an argv list run as a subprocess, behind the host's transport prefix.

No local shell is involved: the overlay's words are passed as a list. The free-bytes reading is the
overlay's own argv, run the same way.

A host with a transport prefix is a remote-shell transport (an ssh invocation): the words after
the prefix reach a shell on the far side, which would re-split them. They are therefore joined
into ONE quoted command string, so each declared word arrives as the one literal word it is.

A command runs in its own process group. Its output is read while it runs and only a bounded tail
is kept, so a verbose command cannot grow this process's memory. On a timeout the whole group is
killed, so no helper the command started goes on changing storage after the result says it ended.
Output that is not valid text is decoded with replacement and never hides the exit status.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
from collections import deque
from io import BufferedReader
from typing import cast

from omnimarket.nodes.node_host_storage_effect.handlers.entry_remover_local import (
    EntryRemoverLocal,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    ModelHostStorageEntryRun,
    ModelHostStorageRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelStorageCommand,
    ModelStorageHost,
)

_TAIL_CHARS = 400
# Bytes kept per stream while the command runs: enough for the tail in any text encoding.
_KEEP_BYTES = _TAIL_CHARS * 4
_READ_CHUNK = 65536
_DRAIN_JOIN_SECONDS = 5.0


def _transport_argv(prefix: tuple[str, ...], words: list[str]) -> list[str]:
    """The argv for a host: the words alone locally, or the prefix and one quoted command string."""
    if not prefix:
        return words
    return [*prefix, shlex.join(words)]


class _TailReader(threading.Thread):
    """Reads one pipe to its end, keeping only the last ``_KEEP_BYTES`` bytes."""

    def __init__(self, stream: BufferedReader) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._chunks: deque[bytes] = deque()
        self._kept = 0

    def run(self) -> None:
        read = self._stream.read1
        while True:
            try:
                chunk = read(_READ_CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self._chunks.append(chunk[-_KEEP_BYTES:])
            self._kept += len(self._chunks[-1])
            while self._kept - len(self._chunks[0]) >= _KEEP_BYTES:
                self._kept -= len(self._chunks.popleft())

    def tail(self) -> bytes:
        return b"".join(self._chunks)[-_KEEP_BYTES:]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _run_bounded(
    argv: list[str], timeout_seconds: float
) -> tuple[int | None, bool, str, str]:
    """(exit code, timed out, stdout tail, stderr tail). Raises OSError when the process cannot start."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    readers = (
        _TailReader(cast(BufferedReader, proc.stdout)),
        _TailReader(cast(BufferedReader, proc.stderr)),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        proc.wait()
    for reader in readers:
        reader.join(timeout=_DRAIN_JOIN_SECONDS)
    proc.stdout.close()
    proc.stderr.close()
    out, err = (r.tail().decode("utf-8", errors="replace") for r in readers)
    return (None if timed_out else proc.returncode), timed_out, out, err


class BackendHostStorageSubprocess:
    def __init__(
        self,
        free_bytes_argv: tuple[str, ...],
        free_bytes_timeout_seconds: float,
        entry_remover: EntryRemoverLocal | None = None,
    ) -> None:
        self._free_argv = free_bytes_argv
        self._free_timeout = free_bytes_timeout_seconds
        self._entry_remover = entry_remover or EntryRemoverLocal()

    def remove_entries(
        self,
        host: ModelStorageHost,
        command: ModelStorageCommand,
        entries: tuple[str, ...],
    ) -> ModelHostStorageEntryRun:
        """Entry commands run on this machine: the overlay validator keeps a transport prefix off them."""
        return self._entry_remover.remove_entries(host, command, entries)

    def free_bytes(self, host: ModelStorageHost, path: str) -> int | None:
        argv = _transport_argv(
            host.transport_prefix,
            [w.replace("{path}", path) for w in self._free_argv],
        )
        try:
            code, timed_out, text, _ = _run_bounded(argv, self._free_timeout)
        except OSError:
            return None
        if timed_out or code != 0:
            return None
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        first = lines[-1].split()[0]
        return int(first) if first.isdigit() else None

    def run(
        self, host: ModelStorageHost, command: ModelStorageCommand
    ) -> ModelHostStorageRun:
        argv = _transport_argv(host.transport_prefix, list(command.argv))
        try:
            code, timed_out, out, err = _run_bounded(argv, command.timeout_seconds)
        except OSError as exc:
            return ModelHostStorageRun(exit_code=None, output_tail=type(exc).__name__)
        if timed_out:
            return ModelHostStorageRun(exit_code=None, timed_out=True)
        return ModelHostStorageRun(
            exit_code=code, output_tail=(out + err)[-_TAIL_CHARS:]
        )
