# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live-mode reads of the session sources (OMN-21111): a session transcript and the ledger.

The transcript is the harness's JSONL session log, `<session_id>.jsonl` under
ONEX_SESSION_TRANSCRIPT_ROOT (required, no default). An operator prompt is a `user` record
whose content is typed text: never a tool result, a meta record, a compaction summary, a
sidechain (subagent) record or a local-command echo. Its prompt id is the record's `promptId`
(its `uuid` when it has none), and the instant is the record's `timestamp`.

The ledger is the markdown work ledger at ONEX_LEDGER_PATH (required, no default). Besides the
pinned mode's `prompt_id=`/`covers=` citations and `reviewed=` review rows, a RULING or
OPERATOR-CONSENT row records the words it quotes: every quoted fragment found verbatim in a
prompt covers that span. Rows stamped after the request's watermark are not read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from omnimarket.nodes.node_session_context_sources_effect.models import (
    EnumPromptOrigin,
    ModelPromptCitation,
    ModelSessionPrompt,
)

TRANSCRIPT_ROOT_ENV = "ONEX_SESSION_TRANSCRIPT_ROOT"
LEDGER_PATH_ENV = "ONEX_LEDGER_PATH"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ROW_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_AUTHORITY_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})
# A quoted run in a cell: straight or curly quotes, or the ledger's %22-encoded quotes.
_QUOTED = re.compile(r'"([^"\n]*\S[^"\n]*)"|“([^”\n]*\S[^”\n]*)”')
_QUESTION = re.compile(r"(?i)^question=")
_FRAGMENT_SPLIT = " / "
_ECHO_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)


def _typed_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    parts = [
        str(b.get("text", ""))
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(parts) if parts else None


def transcript_prompts(
    lines: Iterable[str], transcript_ref: str, *, until: datetime | None
) -> list[ModelSessionPrompt]:
    """The operator prompts of one transcript, in transcript order, said at or before `until`."""
    found: list[tuple[datetime, int, dict[str, Any], str]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        if (
            record.get("isMeta")
            or record.get("isCompactSummary")
            or record.get("isSidechain")
        ):
            continue
        message = record.get("message")
        text = _typed_text(
            message.get("content") if isinstance(message, dict) else None
        )
        if text is None or not text.strip() or text.lstrip().startswith(_ECHO_PREFIXES):
            continue
        try:
            said = datetime.fromisoformat(str(record.get("timestamp", "")))
        except ValueError:
            continue
        if said.tzinfo is None or (until is not None and said > until):
            continue
        found.append((said, number, record, text))
    found.sort(key=lambda item: (item[0], item[1]))
    return [
        ModelSessionPrompt(
            prompt_id=str(
                record.get("promptId") or record.get("uuid") or f"line-{number}"
            ),
            capture_sequence=index,
            text=text,
            said_at=said,
            origin=EnumPromptOrigin.OPERATOR_PROMPT,
            source_ref=f"{transcript_ref}#{record.get('uuid') or number}",
        )
        for index, (said, number, record, text) in enumerate(found)
    ]


def ledger_lines(text: str, *, until: str | None) -> list[str]:
    """The ledger's row lines stamped at or before `until` (every row when it is None)."""
    rows = []
    for line in text.splitlines():
        stamp = line.split(" | ", 1)[0].strip()
        if _ROW_STAMP.match(stamp) and (until is None or stamp <= until):
            rows.append(line)
    return rows


def quoted_fragments(raw_row: str) -> tuple[str, ...]:
    """Every quoted fragment of the row's first quoted cell (not `question=`), unquoted."""
    for cell in (part.strip() for part in raw_row.split("|")[2:]):
        if _QUESTION.match(cell):
            continue
        decoded = unquote(cell) if "%22" in cell else cell
        match = _QUOTED.search(decoded)
        if match:
            words = match.group(1) or match.group(2) or ""
            return tuple(
                part.strip() for part in words.split(_FRAGMENT_SPLIT) if part.strip()
            )
    return ()


def quote_citations(
    prompts: Iterable[ModelSessionPrompt], rows: Iterable[str]
) -> list[ModelPromptCitation]:
    """A citation for every RULING or OPERATOR-CONSENT fragment found verbatim in a prompt."""
    quoted: list[tuple[str, str]] = []
    for raw in rows:
        cells = [cell.strip() for cell in raw.split(" | ")]
        if len(cells) < 3 or cells[1] not in _AUTHORITY_TYPES:
            continue
        lane = next((c[5:] for c in cells[2:] if c.startswith("lane=")), "")
        row_ref = f"{cells[0]} {cells[1]} lane={lane}"
        quoted.extend((fragment, row_ref) for fragment in quoted_fragments(raw))
    citations: list[ModelPromptCitation] = []
    for prompt in prompts:
        for fragment, row_ref in quoted:
            start = prompt.text.find(fragment)
            if start >= 0:
                citations.append(
                    ModelPromptCitation(
                        prompt_id=prompt.prompt_id,
                        start=start,
                        end=start + len(fragment),
                        row_ref=row_ref,
                    )
                )
    return citations


def transcript_path(root: Path, session_id: str) -> Path | None:
    """`<root>/<session_id>.jsonl`, or None when the id is not a plain file name."""
    if not SESSION_ID.match(session_id):
        return None
    return root / f"{session_id}.jsonl"


__all__ = [
    "LEDGER_PATH_ENV",
    "TRANSCRIPT_ROOT_ENV",
    "ledger_lines",
    "quote_citations",
    "quoted_fragments",
    "transcript_path",
    "transcript_prompts",
]
