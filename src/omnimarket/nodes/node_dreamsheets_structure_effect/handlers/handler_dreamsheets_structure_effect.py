# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDreamsheetsStructureEffect -- one DreamSheets document in, one
structure record out, from DreamSheets' own headless binary run in a sandbox.

Canonical def-B handler: ``handle(request) -> result``.

ONE RUN. Copy the document into a private run directory and hash it; start the
binary through the launcher (bubblewrap by default); send exactly three
JSON-RPC messages -- ``initialize``, ``open_document`` on the copy,
``get_structure_summary`` -- and close stdin, which ends the server; read at
most ``max_output_bytes`` of stdout under a wall-clock limit; kill the process
group whatever happened; re-hash the copy and the source; delete the run
directory.

NOTHING FROM THE DOCUMENT LEAVES. The record holds counts only (DreamSheets
spec: no names, ids, paths or text). Errors carry DreamSheets' bare codes
(``E_OPEN_IO`` ...) or this node's own, with fixed ``detail`` strings: the
binary's own message text is never copied into a result, and neither is the
document's path. stderr is discarded.

PRIVACY MODE. An empty config dir makes the binary withhold row counts
(``values_withheld: true``, size fields ``None``). ``values_withheld=False``
writes ``{"withholdData": false}`` so the record carries counts; cell values
never leave either way, because the process has no tool that returns them. A
record whose ``values_withheld`` disagrees with the configured mode is
rejected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from omnimarket.nodes.node_dreamsheets_structure_effect.models.model_dreamsheets_structure import (
    EnumDreamsheetsStructureStatus,
    ModelDreamsheetsStructureRequest,
    ModelDreamsheetsStructureResult,
    ModelDreamsheetsStructureSummary,
)
from omnimarket.nodes.node_dreamsheets_structure_effect.protocols.sandbox_launcher import (
    BwrapDreamsheetsLauncher,
    ProtocolSandboxLauncher,
    RunLayout,
)

logger = logging.getLogger(__name__)

#: Where the extracted DreamSheets AppImage (``squashfs-root``) lives.
ENV_APPIMAGE_ROOT = "ONEX_DREAMSHEETS_APPIMAGE_ROOT"
DEFAULT_APPIMAGE_ROOT = "/opt/dreamsheets/squashfs-root"
#: The sha256 the binary must have; unset = not pinned (development only).
ENV_BINARY_SHA256 = "ONEX_DREAMSHEETS_BINARY_SHA256"

RUN_DIR_PREFIX = "dsstruct-"
_CODE = re.compile(r"^(E_[A-Z_]{2,40}):")
_VERSION = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")

#: This node's own codes. DreamSheets' codes (E_OPEN_*, E_NOT_DSHEET,
#: E_NO_DOCUMENT, E_LOADING, E_TOO_LARGE) pass through with a generic detail.
_DETAIL: dict[str, str] = {
    "E_INPUT_PATH": "the document path must be absolute and end in .dsheet",
    "E_INPUT_MISSING": "the document is not a readable file",
    "E_INPUT_TOO_LARGE": "the document is larger than this node accepts",
    "E_BINARY_MISSING": "the configured DreamSheets binary does not exist",
    "E_BINARY_HASH": "the DreamSheets binary does not match the pinned sha256",
    "E_SPAWN": "the sandbox could not be started",
    "E_TIMEOUT": "the run exceeded its time limit and was killed",
    "E_OUTPUT_TOO_LARGE": "the binary wrote more output than this node accepts",
    "E_PROTOCOL": "the binary's reply was missing or malformed",
    "E_RECORD_INVALID": "the record does not match summary_version 1",
    "E_PRIVACY_MISMATCH": "the record's privacy mode is not the configured one",
    "E_INPUT_CHANGED": "the document changed while it was being read",
}
_GENERIC_DREAMSHEETS_DETAIL = "DreamSheets refused the document"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rpc(id_: int, method: str, params: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})


def _tool(id_: int, name: str, arguments: dict[str, Any]) -> str:
    return _rpc(id_, "tools/call", {"name": name, "arguments": arguments})


class _RunFailedError(Exception):
    """A run that ended without a record; ``code`` is safe to report."""

    def __init__(self, status: EnumDreamsheetsStructureStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class HandlerDreamsheetsStructureEffect:
    """EFFECT handler: summarize one DreamSheets document in a sandbox."""

    def __init__(
        self,
        launcher: ProtocolSandboxLauncher | None = None,
        *,
        binary_sha256: str | None = None,
        values_withheld: bool = True,
        timeout_s: float = 60.0,
        max_input_bytes: int = 256 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024,
        run_base_dir: Path | None = None,
        stale_run_age_s: float = 3600.0,
    ) -> None:
        # Defaults come from the host's environment, never from a request.
        self._launcher = launcher or BwrapDreamsheetsLauncher(
            Path(os.environ.get(ENV_APPIMAGE_ROOT, DEFAULT_APPIMAGE_ROOT))
        )
        pinned = binary_sha256 or os.environ.get(ENV_BINARY_SHA256)
        self._binary_sha256 = pinned.lower() if pinned else None
        self._values_withheld = values_withheld
        self._timeout_s = timeout_s
        self._max_input_bytes = max_input_bytes
        self._max_output_bytes = max_output_bytes
        self._run_base_dir = run_base_dir or Path(tempfile.gettempdir())
        self._stale_run_age_s = stale_run_age_s

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self, request: ModelDreamsheetsStructureRequest
    ) -> ModelDreamsheetsStructureResult:
        import asyncio

        return await asyncio.to_thread(self.run_sync, request)

    def run_sync(
        self, request: ModelDreamsheetsStructureRequest
    ) -> ModelDreamsheetsStructureResult:
        hashes: dict[str, str | None] = {"input": None, "binary": None}
        version: list[str | None] = [None]
        try:
            summary = self._run(request, hashes, version)
        except _RunFailedError as f:
            logger.info(
                "dreamsheets_structure %s: %s correlation_id=%s",
                f.status.value,
                f.code,
                request.correlation_id,
            )
            return ModelDreamsheetsStructureResult(
                status=f.status,
                correlation_id=request.correlation_id,
                error_code=f.code,
                detail=_DETAIL.get(f.code, _GENERIC_DREAMSHEETS_DETAIL),
                input_sha256=hashes["input"],
                binary_sha256=hashes["binary"],
                dreamsheets_version=version[0],
            )
        return ModelDreamsheetsStructureResult(
            status=EnumDreamsheetsStructureStatus.COMPLETED,
            correlation_id=request.correlation_id,
            summary=summary,
            input_sha256=hashes["input"],
            binary_sha256=hashes["binary"],
            dreamsheets_version=version[0],
        )

    # -- the run -----------------------------------------------------------

    def _run(
        self,
        request: ModelDreamsheetsStructureRequest,
        hashes: dict[str, str | None],
        version: list[str | None],
    ) -> ModelDreamsheetsStructureSummary:
        refused = EnumDreamsheetsStructureStatus.REFUSED
        source = Path(request.document_path)
        if not source.is_absolute() or source.suffix.lower() != ".dsheet":
            raise _RunFailedError(refused, "E_INPUT_PATH")
        if not source.is_file():
            raise _RunFailedError(refused, "E_INPUT_MISSING")
        if source.stat().st_size > self._max_input_bytes:
            raise _RunFailedError(refused, "E_INPUT_TOO_LARGE")
        binary = self._launcher.binary
        if not binary.is_file():
            raise _RunFailedError(refused, "E_BINARY_MISSING")
        hashes["binary"] = _sha256(binary)
        if self._binary_sha256 is not None and hashes["binary"] != self._binary_sha256:
            raise _RunFailedError(refused, "E_BINARY_HASH")

        self._sweep_stale_runs()
        self._run_base_dir.mkdir(parents=True, exist_ok=True)
        layout = RunLayout(
            Path(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX, dir=self._run_base_dir))
        )
        try:
            layout.create()
            hashes["input"] = _sha256(source)
            shutil.copyfile(source, layout.input_file)
            os.chmod(layout.input_file, 0o444)
            if _sha256(layout.input_file) != hashes["input"]:
                raise _RunFailedError(
                    EnumDreamsheetsStructureStatus.FAILED, "E_INPUT_CHANGED"
                )
            if not self._values_withheld:
                (layout.config_dir / "ai-config.json").write_text(
                    '{"withholdData": false}'
                )
            replies = self._exchange(layout)
            summary = self._record(replies, version)
            # The copy is read-only to the binary and the source is ours to
            # trust only as of the hash: either changing voids the record.
            if (
                _sha256(layout.input_file) != hashes["input"]
                or _sha256(source) != hashes["input"]
            ):
                raise _RunFailedError(
                    EnumDreamsheetsStructureStatus.FAILED, "E_INPUT_CHANGED"
                )
            return summary
        finally:
            _remove_tree(layout.root)

    def _exchange(self, layout: RunLayout) -> dict[int, dict[str, Any]]:
        failed = EnumDreamsheetsStructureStatus.FAILED
        messages = "\n".join(
            [
                _rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
                _tool(
                    2, "open_document", {"path": self._launcher.document_path(layout)}
                ),
                _tool(3, "get_structure_summary", {}),
            ]
        )
        try:
            proc = subprocess.Popen(
                self._launcher.command(layout),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._launcher.environment(layout),
                cwd=layout.work_dir,
                close_fds=True,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            raise _RunFailedError(failed, "E_SPAWN") from exc
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            _kill(proc)

        timer = threading.Timer(self._timeout_s, expire)
        timer.start()
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            try:
                proc.stdin.write((messages + "\n").encode())
                proc.stdin.close()
            except OSError:
                pass  # the process died early; its (missing) output says why
            out = proc.stdout.read(self._max_output_bytes + 1)
        finally:
            timer.cancel()
            _kill(proc)
            proc.wait()
        if timed_out.is_set():
            raise _RunFailedError(failed, "E_TIMEOUT")
        if len(out) > self._max_output_bytes:
            raise _RunFailedError(failed, "E_OUTPUT_TOO_LARGE")
        replies: dict[int, dict[str, Any]] = {}
        for line in out.decode("utf-8", errors="replace").splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                raise _RunFailedError(failed, "E_PROTOCOL") from None
            if isinstance(msg, dict) and isinstance(msg.get("id"), int):
                replies[msg["id"]] = msg
        return replies

    def _record(
        self, replies: dict[int, dict[str, Any]], version: list[str | None]
    ) -> ModelDreamsheetsStructureSummary:
        failed = EnumDreamsheetsStructureStatus.FAILED
        info = replies.get(1, {}).get("result", {}).get("serverInfo", {})
        v = info.get("version") if isinstance(info, dict) else None
        version[0] = v if isinstance(v, str) and _VERSION.match(v) else None
        for id_ in (2, 3):
            result = replies.get(id_, {}).get("result")
            if not isinstance(result, dict):
                raise _RunFailedError(failed, "E_PROTOCOL")
            if result.get("isError"):
                raise _RunFailedError(failed, _error_code(result))
        text = _first_text(replies[3]["result"])
        try:
            summary = ModelDreamsheetsStructureSummary.model_validate_json(text)
        except ValidationError:
            raise _RunFailedError(failed, "E_RECORD_INVALID") from None
        if summary.values_withheld != self._values_withheld:
            raise _RunFailedError(failed, "E_PRIVACY_MISMATCH")
        return summary

    def _sweep_stale_runs(self) -> None:
        """Delete run dirs a crashed earlier run left behind."""
        if not self._run_base_dir.is_dir():
            return
        cutoff = time.time() - self._stale_run_age_s
        for d in self._run_base_dir.glob(RUN_DIR_PREFIX + "*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    _remove_tree(d)
            except OSError:
                continue


def _remove_tree(root: Path) -> None:
    """Delete a run dir, read-only input copy included (Windows refuses to
    unlink a read-only file; clear the bit and retry)."""

    def retry(func: Any, path: str, _exc: BaseException) -> None:
        os.chmod(path, 0o600)
        func(path)

    try:
        shutil.rmtree(root, onexc=retry)
    except OSError:
        logger.warning("dreamsheets_structure: run dir left for the next sweep")


def _first_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            return text
    return ""


def _error_code(result: dict[str, Any]) -> str:
    """The leading ``E_*`` code of a DreamSheets tool error, or a generic one.
    The rest of the message is dropped unread."""
    m = _CODE.match(_first_text(result))
    return m.group(1) if m else "E_DREAMSHEETS_ERROR"


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the whole process group (bwrap and everything under it)."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


__all__ = [
    "DEFAULT_APPIMAGE_ROOT",
    "ENV_APPIMAGE_ROOT",
    "ENV_BINARY_SHA256",
    "RUN_DIR_PREFIX",
    "HandlerDreamsheetsStructureEffect",
]
