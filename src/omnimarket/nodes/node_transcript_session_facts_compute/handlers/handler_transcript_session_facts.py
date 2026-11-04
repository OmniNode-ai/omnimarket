# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure aggregation of transcript JSONL into counts and metadata."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from omnimarket.models.work_classification import (
    EnumRecordType,
    EnumStreamKind,
    ModelSessionFacts,
    ModelSourceRevision,
)
from omnimarket.nodes.node_transcript_session_facts_compute.models import (
    ModelSessionFactsRequest,
    SourceRevisionShortError,
)

_RECORD_TYPES = frozenset(kind.value for kind in EnumRecordType)
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _permission_decisions(
    record: dict[str, Any], counts: Counter[str], depth: int = 0
) -> None:
    """Count decisions in dictionaries up to four levels below the record."""
    decision = record.get("permissionDecision")
    if isinstance(decision, str):
        counts[decision] += 1
    if depth < 4:
        for value in record.values():
            if isinstance(value, dict):
                _permission_decisions(value, counts, depth + 1)


def _first_prompt_hash(record: dict[str, Any]) -> str | None:
    """Hash a qualifying user's exact text without retaining it in the facts."""
    source = record.get("promptSource")
    if (
        record.get("type") != EnumRecordType.USER
        or not isinstance(source, str)
        or not source
        or record.get("isMeta")
        or record.get("isCompactSummary")
    ):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    else:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else None


def _assistant_facts(
    record: dict[str, Any],
    models: set[str],
    tool_counts: Counter[str],
    written_paths: set[str],
) -> None:
    """Collect model names and tool usage from an assistant message."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    model = message.get("model")
    if isinstance(model, str):
        models.add(model)
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str):
            continue
        tool_counts[name] += 1
        tool_input = block.get("input")
        if name not in _WRITE_TOOLS or not isinstance(tool_input, dict):
            continue
        path = tool_input.get("file_path")
        if not isinstance(path, str):
            path = tool_input.get("notebook_path")
        if isinstance(path, str):
            written_paths.add(path)


class HandlerTranscriptSessionFacts:
    """Compute deterministic aggregate facts from caller-supplied text."""

    def handle(self, request: ModelSessionFactsRequest) -> ModelSessionFacts:
        record_counts: Counter[str] = Counter()
        unknown_types: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter()
        permission_counts: Counter[str] = Counter()
        totals: Counter[str] = Counter()
        models: set[str] = set()
        pr_urls: set[str] = set()
        written_paths: set[str] = set()
        metadata: dict[str, str] = {}
        first_prompt_sha256: str | None = None
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        agent_id: str | None = None
        all_sidechain = True

        for line in request.transcript_text.split("\n"):
            if not line.strip():
                continue
            totals["records"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            if not isinstance(record, dict):
                totals["undecodable"] += 1
                continue

            record_type = str(record.get("type"))
            if record_type in _RECORD_TYPES:
                record_counts[record_type] += 1
            else:
                unknown_types[record_type] += 1

            for key in ("sessionId", "cwd", "gitBranch", "entrypoint", "effort"):
                value = record.get(key)
                if key == "effort" and not isinstance(value, str):
                    value = record.get("effortLevel")
                if key not in metadata and isinstance(value, str):
                    metadata[key] = value
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp

            sidechain = record.get("isSidechain") is True
            if sidechain:
                totals["sidechain"] += 1
            record_agent = record.get("agentId")
            if not sidechain or not isinstance(record_agent, str):
                all_sidechain = False
            elif agent_id is None:
                agent_id = record_agent

            subtype = record.get("subtype")
            if record_type == EnumRecordType.COMPACT_BOUNDARY or (
                record_type == EnumRecordType.SYSTEM and subtype == "compact_boundary"
            ):
                totals["compact_boundary"] += 1
            if subtype in ("stop_hook_summary", "hook_summary"):
                totals["hook_summary"] += 1
            if subtype == "api_error" or (
                record_type == EnumRecordType.ASSISTANT
                and record.get("isApiErrorMessage") is True
            ):
                totals["api_error"] += 1
            if record_type == EnumRecordType.COST_STATE:
                totals["cost_state"] += 1
            if record_type == EnumRecordType.PR_LINK:
                totals["pr_link"] += 1
                pr_url = record.get("prUrl")
                if isinstance(pr_url, str):
                    pr_urls.add(pr_url)
            if record_type == EnumRecordType.ASSISTANT:
                _assistant_facts(record, models, tool_counts, written_paths)
            _permission_decisions(record, permission_counts)
            if first_prompt_sha256 is None:
                first_prompt_sha256 = _first_prompt_hash(record)

        revision = ModelSourceRevision(
            record_count=totals["records"],
            byte_length=len(request.transcript_text.encode("utf-8")),
        )
        required = request.required_source_revision
        if required is not None and not revision.satisfies(required):
            raise SourceRevisionShortError(
                f"Computed source revision {revision!r} falls short of required source revision {required!r}"
            )
        subagent = all_sidechain and agent_id is not None
        return ModelSessionFacts(
            session_id=metadata.get("sessionId"),
            stream_kind=EnumStreamKind.SUBAGENT if subagent else EnumStreamKind.SESSION,
            agent_id=agent_id if subagent else None,
            cwd=metadata.get("cwd"),
            git_branch=metadata.get("gitBranch"),
            entrypoint=metadata.get("entrypoint"),
            effort=metadata.get("effort"),
            models=tuple(models),
            source_revision=revision,
            first_prompt_sha256=first_prompt_sha256,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            record_counts=dict(record_counts),
            unknown_record_types=dict(unknown_types),
            tool_call_counts=dict(tool_counts),
            permission_decision_counts=dict(permission_counts),
            sidechain_record_count=totals["sidechain"],
            hook_summary_count=totals["hook_summary"],
            api_error_count=totals["api_error"],
            compact_boundary_count=totals["compact_boundary"],
            cost_state_count=totals["cost_state"],
            pr_link_count=totals["pr_link"],
            undecodable_line_count=totals["undecodable"],
            pr_link_urls=tuple(pr_urls),
            written_paths=tuple(written_paths),
        )


__all__ = ["HandlerTranscriptSessionFacts"]
