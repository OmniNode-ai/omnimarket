# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReplayBundleWriter: write a replay bundle under the overlay-declared root, then verify (OMN-21108).

The root comes from the private overlay entry for the host and has no default: an absent
overlay, an absent host entry or an empty root is a typed refusal. Every file is written, read
back, compared byte for byte with what was written, and the manifest's pinned hashes are
recomputed from the read-back bytes. Any mismatch is a typed refusal, never a partial success.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    HandlerReplayBundleVerifier,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models import (
    ModelBundleFile,
    ModelVerifyBundleRequest,
)
from omnimarket.nodes.node_replay_bundle_writer_effect.models import (
    EnumReplayBundleWriteStatus,
    ModelReplayBundleWriteRequest,
    ModelReplayBundleWriteResult,
    ModelWrittenBundleFile,
)
from omnimarket.protocols.protocol_replay_bundle_filesystem import (
    ProtocolReplayBundleFilesystem,
)

OVERLAY_SCHEMA = "replay_root.v1"


class LocalReplayBundleFilesystem:
    """ProtocolReplayBundleFilesystem over the local disk."""

    def resolve(self, path: str) -> str:
        return str(Path(path).resolve())

    def write_bytes(self, path: str, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read_bytes(self, path: str) -> bytes | None:
        target = Path(path)
        return target.read_bytes() if target.is_file() else None


def _expand(value: str, environ: Mapping[str, str]) -> str | None:
    """Expand a leading $OMNI_HOME/ or $HOME/ only; anything else is not a declared root."""
    for var in ("OMNI_HOME", "HOME"):
        prefix = "$" + var
        if value == prefix or value.startswith(prefix + "/"):
            base = environ.get(var, "")
            return base + value[len(prefix) :] if base else None
    return None


class HandlerReplayBundleWriter:
    def __init__(
        self,
        filesystem: ProtocolReplayBundleFilesystem | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._fs: ProtocolReplayBundleFilesystem = (
            LocalReplayBundleFilesystem() if filesystem is None else filesystem
        )
        # only $OMNI_HOME and $HOME are read, to expand the overlay's root; the root itself
        # has no default and no environment variable names it.
        self._environ = os.environ if environ is None else environ

    def _declared_root(self, request: ModelReplayBundleWriteRequest) -> str | None:
        raw = self._fs.read_bytes(request.overlay_path)
        if raw is None:
            return None
        overlay = yaml.safe_load(raw)
        if not isinstance(overlay, dict) or overlay.get("schema") != OVERLAY_SCHEMA:
            return None
        hosts = overlay.get("hosts")
        entry = hosts.get(request.host_id) if isinstance(hosts, dict) else None
        value = entry.get("root") if isinstance(entry, dict) else None
        if not isinstance(value, str) or not value.strip():
            return None
        return _expand(value.strip(), self._environ)

    def handle(
        self, request: ModelReplayBundleWriteRequest
    ) -> ModelReplayBundleWriteResult:
        declared = self._declared_root(request)
        if declared is None:
            return ModelReplayBundleWriteResult(
                status=EnumReplayBundleWriteStatus.REFUSED_ROOT_UNDECLARED
            )
        root = self._fs.resolve(declared)
        targets: dict[str, str] = {}
        for file in request.bundle_files:
            target = self._fs.resolve(f"{root}/{file.path}")
            if not target.startswith(root.rstrip("/") + "/"):
                return ModelReplayBundleWriteResult(
                    status=EnumReplayBundleWriteStatus.REFUSED_OUTSIDE_ROOT,
                    root=root,
                    failures=(f"{file.path} resolves outside the declared root",),
                )
            targets[file.path] = target
        for file in request.bundle_files:
            self._fs.write_bytes(targets[file.path], file.content)

        failures: list[str] = []
        written: list[ModelWrittenBundleFile] = []
        read_back: list[ModelBundleFile] = []
        for file in request.bundle_files:
            data = self._fs.read_bytes(targets[file.path])
            ok = data == file.content
            if not ok:
                failures.append(f"{file.path}: read-back bytes differ from written")
            read_back.append(ModelBundleFile(path=file.path, content=data or b""))
            written.append(
                ModelWrittenBundleFile(
                    path=file.path,
                    byte_count=len(file.content),
                    sha256=hashlib.sha256(file.content).hexdigest(),
                    verified=ok,
                )
            )
        cases_checked = 0
        try:
            verdict = HandlerReplayBundleVerifier().handle(
                ModelVerifyBundleRequest(
                    manifest_yaml=request.manifest_yaml, bundle_files=tuple(read_back)
                )
            )
        except (ValueError, KeyError) as exc:
            # a corrupted chain file is unparseable; that is a mismatch, not a crash.
            failures.append(f"pinned hash check could not parse the bundle: {exc!r}")
        else:
            failures.extend(verdict.failures)
            cases_checked = verdict.cases_checked
        return ModelReplayBundleWriteResult(
            status=(
                EnumReplayBundleWriteStatus.REFUSED_HASH_MISMATCH
                if failures
                else EnumReplayBundleWriteStatus.WRITTEN
            ),
            root=root,
            files=tuple(written),
            cases_verified=0 if failures else cases_checked,
            failures=tuple(failures),
        )


__all__ = [
    "OVERLAY_SCHEMA",
    "HandlerReplayBundleWriter",
    "LocalReplayBundleFilesystem",
]
