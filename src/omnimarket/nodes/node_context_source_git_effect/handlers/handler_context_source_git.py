# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped, pinned Git context behind ProtocolContextSource (TC.3)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import yaml
from omnibase_core.enums.enum_context_source_status import (
    EnumContextSourceStatus as Status,
)
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_context_source_git_effect.handlers.git_object_reader import (
    GitObjectReader,
    GitSourceReadError,
    ProtocolGitObjectReader,
)
from omnimarket.nodes.node_context_source_git_effect.models.model_git_source import (
    ModelGitSourceEntry,
    ModelGitSourceOverlay,
    ModelGitSourcePolicy,
    ModelGitSourceResult,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


class HandlerContextSourceGit:
    """Return every configured file whole; selection, budget and pack hash are downstream."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        overlay: ModelGitSourceOverlay | None = None,
        reader: ProtocolGitObjectReader | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        contract_path = contract_path or _CONTRACT
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        self._policy = ModelGitSourcePolicy.model_validate(raw["context_source_git"])
        self._overlay = overlay
        self._reader = reader if reader is not None else GitObjectReader()
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self.terminal_event = contract_publish_topics(contract_path)[0]

    def handle(self, request: ModelContextSourceRequest) -> ModelGitSourceResult:
        if not isinstance(request, ModelContextSourceRequest):
            raise TypeError("Git context source requires its contract payload")
        try:
            overlay = load_structured_overlay(
                "node_context_source_git_effect",
                ModelGitSourceOverlay,
                explicit=self._overlay,
            )
        except NodeOverlayError:
            return ModelGitSourceResult(status=Status.UNAVAILABLE)
        factors = {source.factor for source in overlay.sources}
        if request.factor not in factors:
            return ModelGitSourceResult(status=Status.UNRESOLVED)
        source = next(
            (
                source
                for source in overlay.sources
                if source.factor == request.factor
                and source.repository == request.caller_scope.repository
            ),
            None,
        )
        if (
            source is None
            or request.access.tenant_id not in source.tenant_ids
            or request.revision != source.commit_sha
        ):
            return ModelGitSourceResult(status=Status.DENIED)
        try:
            snapshot = self._reader.read(
                source.repository_path,
                source.commit_sha,
                source.paths,
                timeout_seconds=self._policy.timeout_seconds,
            )
            if (
                snapshot.commit_sha != source.commit_sha
                or tuple(blob.path for blob in snapshot.blobs) != source.paths
                or any(blob.path not in snapshot.tree_paths for blob in snapshot.blobs)
            ):
                raise GitSourceReadError("reader returned a different pin or file set")
            age = (self._now() - snapshot.committed_at).total_seconds()
            entries: list[ModelGitSourceEntry] = []
            for blob in snapshot.blobs:
                text = blob.raw_bytes.decode("utf-8")
                entries.append(
                    ModelGitSourceEntry(
                        factor=request.factor,
                        text=text,
                        raw_bytes=blob.raw_bytes,
                        path=blob.path,
                        source_ref=f"{blob.path}@{source.commit_sha}",
                        span=ModelContextSourceSpan(
                            unit=EnumContextSpanUnit.CHARACTER, start=0, end=len(text)
                        ),
                        text_sha256=hashlib.sha256(blob.raw_bytes).hexdigest(),
                        status=Status.RESOLVED,
                        verbatim_verified=True,
                        as_of=snapshot.committed_at,
                        blob_sha=blob.blob_sha,
                        commit_sha=source.commit_sha,
                        policy_id=source.policy_id,
                        freshness="stale"
                        if age > source.freshness_seconds
                        else "fresh",
                    )
                )
        except (GitSourceReadError, UnicodeError, ValueError):
            return ModelGitSourceResult(status=Status.UNAVAILABLE)
        return ModelGitSourceResult(
            status=Status.RESOLVED,
            entries=tuple(entries),
            tree_paths=snapshot.tree_paths,
            entry_counts={"git": len(entries)},
        )
