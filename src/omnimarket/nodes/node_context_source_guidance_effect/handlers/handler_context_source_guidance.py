# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B handler: the guidance context source (OMN-21111).

``handle(ModelContextSourceRequest) -> ModelContextSourceResult`` answers
``LANE_RULES`` and ``ARCHITECTURE_PATTERNS`` with the guidance-file sections the
contract names for each factor. Each document is read under the root the
contract's ``root_env`` names (required, no default), split by the pack
builder's ``GuidanceSectionParser``, and every section whose last heading
starts with one of the document's listed headings comes back whole, in file
order. ``{repository}`` in a path is the caller scope's repository; a document
that needs one the request does not name is skipped.

The entry's ``as_of`` is the request's revision when that is a UTC row stamp,
otherwise the file's modification time. A missing root is ``unavailable``; a
factor no listed section answers is ``unresolved``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_context_pack_builder_compute.parsers.parser_guidance_section import (
    GuidanceSectionParser,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_REPOSITORY = "{repository}"
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class ModelGuidanceDocument(BaseModel):
    """One guidance file and the headings that answer its factor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor
    path: str = Field(min_length=1)
    headings: tuple[str, ...] = Field(min_length=1)


class ModelGuidanceBinding(BaseModel):
    """The contract's ``context_source_guidance`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    documents: tuple[ModelGuidanceDocument, ...] = Field(min_length=1)


def load_guidance_binding(contract_path: Path) -> ModelGuidanceBinding:
    """Load and validate the contract's ``context_source_guidance`` block."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("context_source_guidance") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ValueError("context_source_guidance must be a mapping")
    return ModelGuidanceBinding.model_validate(block)


class HandlerContextSourceGuidance:
    """The guidance tier behind ``ProtocolContextSource``."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        binding: ModelGuidanceBinding | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        self._binding = binding or load_guidance_binding(self._contract_path)
        self._env = env
        self._factors = frozenset(d.factor for d in self._binding.documents)

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        if not isinstance(request, ModelContextSourceRequest):
            raise TypeError("guidance context source requires its contract payload")
        if request.factor not in self._factors:
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNRESOLVED)
        env = os.environ if self._env is None else self._env
        value = env.get(self._binding.root_env, "")
        if not value or not Path(value).is_dir():
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNAVAILABLE)
        root = Path(value).resolve()
        repository = request.caller_scope.repository
        entries: list[ModelContextSourceEntry] = []
        skipped = 0
        for document in self._binding.documents:
            if document.factor is not request.factor:
                continue
            path = self._path(root, document.path, repository)
            if path is None or not path.is_file():
                skipped += 1
                continue
            entries.extend(self._sections(root, path, document, request.revision))
        counts = {"sections": len(entries), "documents_skipped": skipped}
        if not entries:
            return ModelContextSourceResult(
                status=EnumContextSourceStatus.UNRESOLVED, entry_counts=counts
            )
        return ModelContextSourceResult(
            status=EnumContextSourceStatus.RESOLVED,
            entries=tuple(entries),
            entry_counts=counts,
        )

    @staticmethod
    def _path(root: Path, template: str, repository: str | None) -> Path | None:
        if _REPOSITORY in template:
            if not repository or not _REPOSITORY_NAME.match(repository):
                return None
            template = template.replace(_REPOSITORY, repository)
        target = (root / template).resolve()
        return target if root in target.parents else None

    @staticmethod
    def _sections(
        root: Path, path: Path, document: ModelGuidanceDocument, revision: str
    ) -> list[ModelContextSourceEntry]:
        relative = path.relative_to(root).as_posix()
        if _STAMP.match(revision):
            as_of = datetime.strptime(revision, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        else:
            as_of = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        sections = GuidanceSectionParser().parse(
            path.read_text(encoding="utf-8"), source_file=relative
        )
        return [
            ModelContextSourceEntry(
                factor=document.factor,
                text=section.content,
                source_ref=f"{relative}#{section.heading_path[-1]}",
                span=ModelContextSourceSpan(
                    unit=EnumContextSpanUnit.CHARACTER,
                    start=0,
                    end=len(section.content),
                ),
                text_sha256=hashlib.sha256(section.content.encode("utf-8")).hexdigest(),
                status=EnumContextSourceStatus.RESOLVED,
                verbatim_verified=True,
                as_of=as_of,
            )
            for section in sections
            if section.heading_path
            and section.content.strip()
            and section.heading_path[-1].startswith(document.headings)
        ]


__all__ = [
    "HandlerContextSourceGuidance",
    "ModelGuidanceBinding",
    "ModelGuidanceDocument",
    "load_guidance_binding",
]
