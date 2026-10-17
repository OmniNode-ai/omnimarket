# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select the invariants a profile's scopes call for, verbatim from the registry (OMN-21126).

Selection is ``status: active`` and at least one requested scope among the
entry's ``scope`` tags, in file order. Every superseded entry is listed as
excluded with its successor, whatever its scopes. Each admitted entry's text is
the registry item's own lines, cut whole from the file text at the pinned
commit, so the statement and the ``enforcement`` line are byte-equal to the
file's and the text is a contiguous substring of it; the entry is labelled a
paraphrase because the registry paraphrases the ruling. No token budget is
applied and no hash over the selection is computed: the builder owns both.
"""

from __future__ import annotations

import hashlib

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)
from pydantic import ValidationError

from omnimarket.nodes.node_context_invariants_select_compute.models import (
    EnumInvariantExclusionReason,
    EnumInvariantsSelectError,
    ModelInvariantExclusion,
    ModelInvariantRecord,
    ModelInvariantsSelectRequest,
    ModelInvariantsSelectResult,
    ModelSelectedInvariant,
)

__all__ = ["HandlerContextInvariantsSelect"]


def _last_content_line(node: yaml.Node) -> int:
    """Zero-based last line holding content of ``node``; a mark at column 0 ends the line before."""
    if isinstance(node, yaml.MappingNode):
        return max(
            max(_last_content_line(key), _last_content_line(value))
            for key, value in node.value
        )
    if isinstance(node, yaml.SequenceNode) and node.value:
        return max(_last_content_line(child) for child in node.value)
    end = node.end_mark
    return end.line - 1 if end.column == 0 else end.line


def _refuse(
    code: EnumInvariantsSelectError, message: str
) -> ModelInvariantsSelectResult:
    return ModelInvariantsSelectResult(
        status=EnumContextSourceStatus.UNAVAILABLE,
        error_code=code,
        error_message=message,
    )


class HandlerContextInvariantsSelect:
    """Pure compute: registry text and scopes in, admitted invariants and exclusions out."""

    def handle(
        self, request: ModelInvariantsSelectRequest
    ) -> ModelInvariantsSelectResult:
        invalid = EnumInvariantsSelectError.INVALID_REGISTRY
        try:
            root = yaml.compose(request.file_text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            return _refuse(invalid, f"registry does not parse: {exc}")
        if not isinstance(root, yaml.MappingNode):
            return _refuse(invalid, "registry root is not a mapping")
        top = {key.value: value for key, value in root.value}
        if "scopes" not in top or "invariants" not in top:
            return _refuse(invalid, "registry declares no scopes or no invariants")
        items = top["invariants"]
        if not isinstance(items, yaml.SequenceNode):
            return _refuse(invalid, "invariants is not a list")

        loaded = yaml.safe_load(request.file_text)
        if not isinstance(loaded["scopes"], list):
            return _refuse(invalid, "scopes is not a list")
        vocabulary = {str(scope) for scope in loaded["scopes"]}
        unknown = sorted(set(request.scopes) - vocabulary)
        if unknown:
            return _refuse(
                EnumInvariantsSelectError.UNKNOWN_SCOPE,
                f"scopes not in the registry vocabulary: {', '.join(unknown)}",
            )

        lines = request.file_text.splitlines(keepends=True)
        wanted = set(request.scopes)
        ref_base = f"{request.source_path}@{request.commit_sha}"
        seen: set[str] = set()
        selected: list[ModelSelectedInvariant] = []
        excluded: list[ModelInvariantExclusion] = []
        for node, raw in zip(items.value, loaded["invariants"], strict=True):
            try:
                record = ModelInvariantRecord.model_validate(raw)
            except ValidationError as exc:
                return _refuse(invalid, f"registry entry does not validate: {exc}")
            if record.id in seen:
                return _refuse(invalid, f"registry id {record.id} appears twice")
            seen.add(record.id)
            source_ref = f"{ref_base}#{record.id}"
            if record.status == "superseded":
                excluded.append(
                    ModelInvariantExclusion(
                        invariant_id=record.id,
                        reason=EnumInvariantExclusionReason.SUPERSEDED,
                        superseded_by=record.superseded_by,
                        source_ref=source_ref,
                    )
                )
                continue
            if not wanted.intersection(record.scope):
                continue
            first = node.start_mark.line
            last = _last_content_line(node)
            text = "".join(lines[first : last + 1])
            entry = ModelContextSourceEntry(
                factor=EnumContextFactor.INVARIANTS,
                text=text,
                source_ref=source_ref,
                span=ModelContextSourceSpan(
                    unit=EnumContextSpanUnit.LINE, start=first + 1, end=last + 1
                ),
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                status=EnumContextSourceStatus.RESOLVED,
                supersession_status=EnumContextSupersessionStatus.ACTIVE,
                paraphrase=True,
                as_of=request.as_of,
            )
            selected.append(
                ModelSelectedInvariant(
                    invariant_id=record.id,
                    statement=record.statement,
                    enforcement=record.enforcement,
                    scopes=record.scope,
                    line_start=first + 1,
                    line_end=last + 1,
                    entry=entry,
                )
            )
        status = (
            EnumContextSourceStatus.RESOLVED
            if selected
            else EnumContextSourceStatus.UNRESOLVED
        )
        return ModelInvariantsSelectResult(
            status=status, selected=tuple(selected), excluded=tuple(excluded)
        )
