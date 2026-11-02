# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read and write a compaction replay bundle on disk under an explicit root.

The compute node node_context_compaction_manifest_compute is pure; these helpers are the only
file I/O around it and are kept outside the node so its handler stays free of I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from omnimarket.models.replay_bundle import ModelBundleFile
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    REPLAY_ROOT_ENV,
    ManifestRefusalError,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_transcript_manifest import (
    TRANSCRIPT_ROOT_ENV,
    HandlerTranscriptManifest,
    MissingRequiredVariableError,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_compaction_manifest import (
    ModelLedgerFile,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_transcript_manifest import (
    ModelSummaryPin,
    ModelTranscriptFile,
    ModelTranscriptManifestRequest,
    ModelTranscriptManifestResult,
)

__all__ = [
    "TRANSCRIPT_MANIFEST_NAME",
    "build_transcript_manifest_from_env",
    "read_bundle_files",
    "read_transcript_files",
    "replay_root_from_env",
    "transcript_root_from_env",
    "write_bundle_files",
]

TRANSCRIPT_MANIFEST_NAME = "transcript_manifest.yaml"


def _required_root(env: Mapping[str, str], variable: str) -> Path:
    value = env.get(variable, "")
    if not value.strip():
        raise MissingRequiredVariableError(variable)
    return Path(value)


def replay_root_from_env(env: Mapping[str, str]) -> Path:
    """The bundle root: required, no default."""
    return _required_root(env, REPLAY_ROOT_ENV)


def transcript_root_from_env(env: Mapping[str, str]) -> Path:
    """The transcript root: required, no default."""
    return _required_root(env, TRANSCRIPT_ROOT_ENV)


def read_transcript_files(root: Path) -> tuple[ModelTranscriptFile, ...]:
    """Every `*.jsonl` under the transcript root, by path relative to it."""
    if not root.is_dir():
        raise ManifestRefusalError(f"{TRANSCRIPT_ROOT_ENV} {root} is not a directory")
    return tuple(
        ModelTranscriptFile(
            relative_path=path.relative_to(root).as_posix(), content=path.read_bytes()
        )
        for path in sorted(root.rglob("*.jsonl"))
        if path.is_file()
    )


def build_transcript_manifest_from_env(
    env: Mapping[str, str],
    *,
    ledger_files: Iterable[ModelLedgerFile],
    seed: str,
    summary_pins: Iterable[ModelSummaryPin] = (),
) -> ModelTranscriptManifestResult:
    """Read the transcripts, build the manifest and write it under the replay root.

    Both roots are checked before anything is read, so an unset variable fails the whole run.
    """
    transcript_root = transcript_root_from_env(env)
    replay_root = replay_root_from_env(env)
    result = HandlerTranscriptManifest().handle(
        ModelTranscriptManifestRequest(
            transcripts=read_transcript_files(transcript_root),
            ledger_files=tuple(ledger_files),
            summary_pins=tuple(summary_pins),
            seed=seed,
        )
    )
    write_bundle_files(
        replay_root,
        (
            ModelBundleFile(
                path=TRANSCRIPT_MANIFEST_NAME,
                content=result.manifest_yaml.encode("utf-8"),
            ),
        ),
    )
    return result


def write_bundle_files(root: Path, files: Iterable[ModelBundleFile]) -> None:
    resolved = root.resolve()
    for file in files:
        target = (resolved / file.path).resolve()
        if resolved not in target.parents:
            raise ManifestRefusalError(
                f"bundle path {file.path} resolves outside {REPLAY_ROOT_ENV}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.content)


def read_bundle_files(root: Path) -> tuple[ModelBundleFile, ...]:
    if not root.is_dir():
        raise ManifestRefusalError(f"{REPLAY_ROOT_ENV} {root} is not a directory")
    return tuple(
        ModelBundleFile(
            path=path.relative_to(root).as_posix(), content=path.read_bytes()
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
