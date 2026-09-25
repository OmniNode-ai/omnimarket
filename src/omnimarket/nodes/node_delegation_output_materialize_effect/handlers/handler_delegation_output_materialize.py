# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: write accepted delegation outputs to a target and the store (OMN-19600).

The paths were chosen by a model, so the write must stay inside the declared
target even when the target already contains symlinks. Resolving a path and
checking that it is under the root, then writing to it, is not enough: the
tree can change between the check and the write, and ``resolve()`` says
nothing about a symlink created a moment later. This handler instead:

1. opens the target root with ``O_DIRECTORY | O_NOFOLLOW`` (a symlinked root
   is refused for the whole request);
2. walks each parent component with ``os.open(part, O_DIRECTORY | O_NOFOLLOW,
   dir_fd=parent)``, creating missing directories with ``os.mkdir(part,
   dir_fd=parent)``, so every hop is relative to a directory file descriptor
   it already holds and a symlinked component fails the open;
3. refuses a final name that is a symlink, and never opens it for writing;
4. writes a fresh temporary file created ``O_CREAT | O_EXCL | O_NOFOLLOW``
   with mode 0600 in that directory, fsyncs it and renames it over the final
   name with both directory descriptors, which replaces a name rather than
   following it (the file is owner-only because a model chose its bytes; a
   caller that wants the group or others to read it widens that itself); and
5. re-reads the result with ``O_NOFOLLOW`` and records its sha256.

The bytes go to core's content-addressed ``ArtifactStore`` first, which
inherits its quotas and its secret refusal; a file the store refuses is not
written to the target either. Every file is checked against its manifest
entry before anything is written, so a file whose bytes differ from what the
receipt will claim is refused, never written.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
from collections.abc import Callable
from uuid import uuid4

from omnibase_core.artifacts.artifact_store import ArtifactStore
from omnibase_core.errors.error_artifact_quota_exceeded import (
    ArtifactQuotaExceededError,
)
from omnibase_core.errors.error_artifact_secret_detected import (
    ArtifactSecretDetectedError,
)

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason,
    ModelDelegationOutputFile,
    ModelDelegationOutputFileRefusal,
    ModelDelegationOutputManifestEntry,
    compute_manifest_sha256,
)
from omnimarket.nodes.node_delegation_output_materialize_effect.models.model_delegation_output_materialize import (
    ModelDelegationOutputMaterializeRequest,
    ModelDelegationOutputMaterializeResult,
    ModelMaterializedOutputFile,
)

_R = EnumDelegationOutputFileRefusalReason
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_MODE = 0o600
_ARTIFACT_KIND = "delegation_output"
_SOURCE_SYSTEM = "omnimarket.node_delegation_output_materialize_effect"


class _OutputRefusedError(Exception):
    """One file refused; carries the typed reason."""

    def __init__(
        self, reason: EnumDelegationOutputFileRefusalReason, detail: str
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_nofollow(name: str, dir_fd: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_child_dir(name: str, parent_fd: int) -> int:
    """Open (creating if absent) directory ``name`` under ``parent_fd``, never following a link."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise
        mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            raise _OutputRefusedError(
                _R.SYMLINK_IN_TARGET, f"{name!r} is a symlink"
            ) from exc
        raise _OutputRefusedError(
            _R.NOT_A_DIRECTORY, f"{name!r} is not a directory"
        ) from exc


def _within[T](parent_fd: int, parents: list[str], action: Callable[[int], T]) -> T:
    """Run ``action`` on the directory ``parents`` names under ``parent_fd``.

    Each hop opens one child with ``_open_child_dir`` and closes that same
    descriptor when everything beneath it has returned or raised.
    """
    if not parents:
        return action(parent_fd)
    child_fd = _open_child_dir(parents[0], parent_fd)
    try:
        return _within(child_fd, parents[1:], action)
    finally:
        os.close(child_fd)


def _write_atomically(name: str, data: bytes, dir_fd: int) -> None:
    temporary = f".onex-output-{uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        _FILE_MODE,
        dir_fd=dir_fd,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(temporary, dir_fd=dir_fd)
        raise
    os.close(fd)
    try:
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.unlink(temporary, dir_fd=dir_fd)
        raise


class HandlerDelegationOutputMaterialize:
    """EFFECT: accepted files and manifest in; files on disk and in the store out."""

    handler_type = "node_handler"
    handler_category = "effect"

    def __init__(self, artifact_store: ArtifactStore | None = None) -> None:
        self._artifact_store = artifact_store

    def _store(self) -> ArtifactStore:
        # Fail-fast (Operating Rule 8): ArtifactStore() raises KeyError when
        # ONEX_ARTIFACT_STORE_ROOT is unset, rather than guessing a root.
        if self._artifact_store is None:
            self._artifact_store = ArtifactStore()
        return self._artifact_store

    def handle(
        self, request: ModelDelegationOutputMaterializeRequest
    ) -> ModelDelegationOutputMaterializeResult:
        os.makedirs(request.target_root, mode=0o755, exist_ok=True)
        try:
            root_fd = os.open(request.target_root, _DIR_FLAGS)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise ValueError(
                    f"target_root is a symlink: {request.target_root!r}"
                ) from exc
            raise ValueError(
                f"target_root is not an openable directory: {request.target_root!r}"
            ) from exc

        refusals: list[ModelDelegationOutputFileRefusal] = list(
            request.manifest.refusals
        )
        written: list[ModelMaterializedOutputFile] = []
        entries = {entry.path: entry for entry in request.manifest.entries}
        supplied = {file.path for file in request.files}
        try:
            for file in request.files:
                entry = entries.get(file.path)
                try:
                    written.append(self._materialize_one(request, root_fd, file, entry))
                except _OutputRefusedError as refused:
                    refusals.append(
                        ModelDelegationOutputFileRefusal(
                            path=file.path,
                            reason=refused.reason,
                            detail=refused.detail[:512],
                        )
                    )
        finally:
            os.close(root_fd)
        refusals.extend(
            ModelDelegationOutputFileRefusal(
                path=path, reason=_R.MISSING, detail="manifest entry without a file"
            )
            for path in entries
            if path not in supplied
        )
        digest_entries = tuple(
            ModelDelegationOutputManifestEntry(
                path=item.path,
                kind=item.kind,
                media_type=item.media_type,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                artifact_ref=item.artifact_ref,
            )
            for item in written
        )
        return ModelDelegationOutputMaterializeResult(
            correlation_id=request.correlation_id,
            target_root=request.target_root,
            written=tuple(written),
            refusals=tuple(refusals),
            manifest_sha256=compute_manifest_sha256(digest_entries),
        )

    def _materialize_one(
        self,
        request: ModelDelegationOutputMaterializeRequest,
        root_fd: int,
        file: ModelDelegationOutputFile,
        entry: ModelDelegationOutputManifestEntry | None,
    ) -> ModelMaterializedOutputFile:
        data = file.content_bytes()
        digest = _sha256(data)
        if entry is None or entry.sha256 != digest or entry.size_bytes != len(data):
            raise _OutputRefusedError(
                _R.MANIFEST_MISMATCH, "file bytes differ from their manifest entry"
            )

        try:
            ref = self._store().write_blob(
                data,
                media_type=entry.media_type,
                artifact_kind=_ARTIFACT_KIND,
                source_system=_SOURCE_SYSTEM,
                scope_ref=request.scope_ref,
                correlation_id=str(request.correlation_id),
            )
        except ArtifactSecretDetectedError as exc:
            raise _OutputRefusedError(
                _R.SECRET_DETECTED, "artifact store detected a secret"
            ) from exc
        except ArtifactQuotaExceededError as exc:
            raise _OutputRefusedError(
                _R.TOO_LARGE, "artifact store quota exceeded"
            ) from exc
        if ref.ref != entry.artifact_ref:
            raise _OutputRefusedError(
                _R.MANIFEST_MISMATCH, "stored ref differs from manifest"
            )

        *parents, name = file.path.split("/")

        def place_and_reread(dir_fd: int) -> tuple[bool, str]:
            wrote = self._place(name, data, digest, dir_fd, request.overwrite)
            return wrote, _sha256(_read_nofollow(name, dir_fd))

        created, on_disk = _within(root_fd, parents, place_and_reread)
        if on_disk != digest:
            raise _OutputRefusedError(
                _R.MANIFEST_MISMATCH, "file on disk differs after the write"
            )
        return ModelMaterializedOutputFile(
            path=file.path,
            kind=entry.kind,
            media_type=entry.media_type,
            sha256=digest,
            size_bytes=len(data),
            artifact_ref=entry.artifact_ref,
            on_disk_sha256=on_disk,
            created=created,
        )

    @staticmethod
    def _place(
        name: str, data: bytes, digest: str, dir_fd: int, overwrite: str
    ) -> bool:
        """Write ``name`` unless an identical file is there; return whether it wrote."""
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            _write_atomically(name, data, dir_fd)
            return True
        if stat.S_ISLNK(mode):
            raise _OutputRefusedError(_R.SYMLINK_IN_TARGET, f"{name!r} is a symlink")
        if not stat.S_ISREG(mode):
            raise _OutputRefusedError(
                _R.EXISTS_WITH_DIFFERENT_CONTENT, f"{name!r} exists and is not a file"
            )
        if _sha256(_read_nofollow(name, dir_fd)) == digest:
            return False
        if overwrite != "replace_declared":
            raise _OutputRefusedError(
                _R.EXISTS_WITH_DIFFERENT_CONTENT,
                f"{name!r} exists with different content and overwrite is 'refuse'",
            )
        _write_atomically(name, data, dir_fd)
        return True


__all__ = ["HandlerDelegationOutputMaterialize"]
