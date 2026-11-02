# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read only hash-pinned snapshots; keep the resulting bundle inside the replay root.

The operator prepares ``cm1_sources.json`` and frozen files on the same host. Both
required roots are checked before I/O. The transcript root must be inside the replay
root, and the inventory must name every transcript. The public compute node makes
the decisions; this effect only validates files and writes immutable bundle bytes.
The receipt contains counts and hashes, never transcript or ledger contents.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import yaml

from omnimarket.models.replay_bundle import (
    ManifestRefusalError,
    ModelBundleFile,
    ModelLedgerFile,
)
from omnimarket.models.transcript_manifest import (
    MissingRequiredVariableError,
    ModelCheckpointSnapshot,
    ModelTranscriptFile,
    ModelTranscriptManifestRequest,
)
from omnimarket.nodes.node_context_compaction_manifest_compute import (
    NodeContextCompactionManifestCompute,
)
from omnimarket.nodes.node_transcript_manifest_effect.models.model_transcript_manifest_source import (
    ModelFrozenFile,
    ModelFrozenTranscriptSources,
    ModelTranscriptManifestSourceRequest,
    ModelTranscriptManifestSourceResult,
)

TRANSCRIPT_ROOT_ENV = "ONEX_TRANSCRIPT_ROOT"
REPLAY_ROOT_ENV = "ONEX_CONTEXT_REPLAY_ROOT"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ManifestRefusalError("snapshot path resolves outside its frozen root")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ManifestRefusalError("snapshot path resolves outside its frozen root")
    return target


def _read(root: Path, file: ModelFrozenFile) -> bytes:
    data = _inside(root, file.relative_path).read_bytes()
    if _sha(data) != file.sha256:
        raise ManifestRefusalError(
            f"sha256 mismatch for frozen file {file.relative_path}"
        )
    return data


class HandlerTranscriptManifestSource:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if environ is None else environ

    def handle(
        self, request: ModelTranscriptManifestSourceRequest
    ) -> ModelTranscriptManifestSourceResult:
        for variable in (TRANSCRIPT_ROOT_ENV, REPLAY_ROOT_ENV):
            if not self._env.get(variable, "").strip():
                raise MissingRequiredVariableError(variable)
        root = Path(self._env[REPLAY_ROOT_ENV]).resolve()
        transcripts = Path(self._env[TRANSCRIPT_ROOT_ENV]).resolve()
        if not transcripts.is_relative_to(root) or transcripts == root:
            raise ManifestRefusalError(
                "ONEX_TRANSCRIPT_ROOT must be inside ONEX_CONTEXT_REPLAY_ROOT; live reads are refused"
            )
        if not root.is_dir() or not transcripts.is_dir():
            raise ManifestRefusalError("both frozen roots must be existing directories")
        index_bytes = _inside(root, request.source_index).read_bytes()
        index = ModelFrozenTranscriptSources.model_validate_json(index_bytes)
        names = [file.relative_path for file in index.transcripts]
        if len(set(names)) != len(names):
            raise ManifestRefusalError("duplicate transcript path in frozen inventory")
        present = {
            path.relative_to(transcripts).as_posix()
            for path in transcripts.rglob("*.jsonl")
            if path.is_file()
        }
        if present != set(names):
            raise ManifestRefusalError(
                "unindexed transcript or missing inventoried transcript"
            )
        result = NodeContextCompactionManifestCompute().handle(
            ModelTranscriptManifestRequest(
                transcripts=tuple(
                    ModelTranscriptFile(
                        relative_path=file.relative_path,
                        content=_read(transcripts, file),
                    )
                    for file in index.transcripts
                ),
                ledger_files=tuple(
                    ModelLedgerFile(name=file.relative_path, content=_read(root, file))
                    for file in index.ledger_files
                ),
                summary_pins=index.summary_pins,
                obligation_counts=index.obligation_counts,
                checkpoints=tuple(
                    ModelCheckpointSnapshot(
                        session_id=item.session_id,
                        boundary_uuid=item.boundary_uuid,
                        captured_at=item.captured_at,
                        content=_read(root, item.file),
                    )
                    for item in index.checkpoints
                ),
                seed=request.seed,
            )
        )
        document = yaml.safe_load(result.manifest_yaml)
        document["source_index_sha256"] = _sha(index_bytes)
        manifest = yaml.safe_dump(document, sort_keys=True, allow_unicode=True).encode()
        digest = _sha(manifest)
        directory = f"manifests/{digest}"
        files = (
            *result.bundle_files,
            ModelBundleFile(path="transcript_manifest.yaml", content=manifest),
        )
        targets = [
            (_inside(root, f"{directory}/{file.path}"), file.content) for file in files
        ]
        for target, data in targets:
            if target.exists() and target.read_bytes() != data:
                raise ManifestRefusalError(
                    "existing frozen bundle bytes differ; refusing overwrite"
                )
        for target, data in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with target.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                pass
            if target.read_bytes() != data:
                raise ManifestRefusalError("bundle read-back bytes differ")
        return ModelTranscriptManifestSourceResult(
            manifest_path=f"{directory}/transcript_manifest.yaml",
            manifest_sha256=digest,
            source_index_sha256=_sha(index_bytes),
            transcripts=len(result.transcripts),
            boundaries=sum(item.boundary_records for item in result.transcripts),
            cases=len(result.cases),
            exclusion_counts=result.exclusion_counts,
        )
