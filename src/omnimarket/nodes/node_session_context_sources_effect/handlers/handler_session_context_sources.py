# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session context sources from pinned bundles or live projections (OMN-21108, plan task TA.6).

A context source in the TC.1 shape: `handle(ModelContextSourceRequest) -> ModelContextSourceResult`,
one factor per read. The request's one scope names the mode.

`case:<case_id>` is pinned mode. It reads `manifest.yaml` and the bundle files of a compaction
replay bundle under ONEX_CONTEXT_REPLAY_ROOT (required, no default), as the T0.2 manifest builder
writes them, and only the pinned bytes: the request's revision must be the case's transcript-chain
sha256, and the chain and every ledger prefix must match the manifest's length and sha256.

OPERATOR_WORDS_UNRECORDED selects the operator prompts of the chain (every boundary of the case,
from session start): an `operator_prompt` record is operator-typed; a `prompt` record is the same
prompt when an `operator_prompt` record with the same content follows it, is dropped when a later
Stop read classified other words and not these, and is otherwise carried as unconfirmed. A record
whose content is a boundary's compaction summary is never a prompt. Citations (`prompt_id=` with
`covers=<start>:<end>`) and review rows (`reviewed=<first>..<last>` on a RULING row) come from the
pinned ledger prefixes only, so a row written after the case's boundary cannot change the read.
RULING and OPERATOR-CONSENT quotes also record words, as in live mode. The carry rule is in
`carry_rule`.

REPO_STATE is `unavailable` in pinned mode where the case excludes it, and always in live mode;
it is never filled from a cwd.

`session:<session_id>` reads the contract-bound session_content and work_ledger_rows
projections up to the revision watermark for an explicitly bound tenant. The same prompt
classification and carry rule apply; RULING and OPERATOR-CONSENT quotes also record words.
Reader failures are unavailable. Any other live factor but REPO_STATE is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
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

from omnimarket.nodes.node_session_context_sources_effect.handlers.carry_rule import (
    carry,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.live_session_reader import (
    PostgresSessionProjectionReader,
    ProtocolSessionContentReader,
    ProtocolSessionLedgerReader,
    ledger_lines,
    load_session_live_binding,
    quote_citations,
)
from omnimarket.nodes.node_session_context_sources_effect.models import (
    EnumCarryDisposition,
    EnumPromptOrigin,
    EnumSessionSourceFailure,
    ModelPromptCitation,
    ModelReviewedRange,
    ModelSessionPrompt,
)

REPLAY_ROOT_ENV = "ONEX_CONTEXT_REPLAY_ROOT"
MANIFEST_NAME = "manifest.yaml"
CASE_SCOPE = "case:"
SESSION_SCOPE = "session:"
PINNED_FACTORS = frozenset(
    {EnumContextFactor.OPERATOR_WORDS_UNRECORDED, EnumContextFactor.REPO_STATE}
)
_ROW_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SOURCE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_COVERS = re.compile(r"^(\d+):(\d+)$")
_REVIEWED = re.compile(r"^(\S+)\.\.(\S+)$")


class _ReadFailureError(Exception):
    def __init__(self, failure: EnumSessionSourceFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure
        self.message = message


def _fail(failure: EnumSessionSourceFailure, message: str) -> ModelContextSourceResult:
    return ModelContextSourceResult.error(
        EnumContextSourceErrorCode.INVALID_REQUEST, f"{failure.value}: {message}"
    )


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _inside(root: Path, relative: str) -> Path:
    resolved = root.resolve()
    target = (resolved / relative).resolve()
    if resolved not in target.parents:
        raise _ReadFailureError(
            EnumSessionSourceFailure.PIN_MISMATCH,
            f"bundle path {relative} resolves outside {REPLAY_ROOT_ENV}",
        )
    return target


def _pinned_bytes(root: Path, relative: str, length: int, digest: str) -> bytes:
    path = _inside(root, relative)
    if not path.is_file():
        raise _ReadFailureError(
            EnumSessionSourceFailure.PIN_MISMATCH, f"bundle file {relative} is missing"
        )
    content = path.read_bytes()
    if len(content) != length or _sha(content) != digest:
        raise _ReadFailureError(
            EnumSessionSourceFailure.PIN_MISMATCH,
            f"bundle file {relative} does not match its pinned length and sha256",
        )
    return content


def _timestamp(row: Mapping[str, Any], where: str) -> datetime:
    value = row.get("emitted_at")
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise _ReadFailureError(
            EnumSessionSourceFailure.NO_SOURCE_TIMESTAMP,
            f"{where} has no timezone-aware emitted_at",
        )
    return moment


def _chain_records(
    chain: bytes, chain_path: str
) -> tuple[list[dict[str, Any]], set[str]]:
    """Content records (chunks joined, in instant order) and the boundary summary hashes."""
    rows = [json.loads(line) for line in chain.decode("utf-8").splitlines()]
    summaries: set[str] = set()
    content = []
    for number, row in enumerate(rows, start=1):
        _timestamp(row, f"{chain_path} line {number}")
        if str(row.get("kind", "")).startswith("hook:"):
            if row.get("summary_sha256"):
                summaries.add(str(row["summary_sha256"]))
        else:
            content.append(row)
    return _join_chunks(content, chain_path), summaries


def _join_chunks(rows: Iterable[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Join each event's chunks and sort captures for both source modes."""
    parts: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for row in rows:
        row = {**row, "_at": _timestamp(row, f"{source}#{row['event_id']}")}
        parts.setdefault(str(row["event_id"]), []).append(
            (int(row.get("chunk_index", 0)), row)
        )
    records: list[dict[str, Any]] = []
    for event_id, chunks in parts.items():
        chunks.sort(key=lambda item: item[0])
        first = chunks[0][1]
        records.append(
            {
                "event_id": event_id,
                "kind": str(first.get("kind", "")),
                "at": first["_at"],
                "prompt_id": first.get("prompt_id"),
                "content": "".join(str(row.get("content", "")) for _, row in chunks),
            }
        )
    records.sort(key=lambda record: (record["at"], record["event_id"]))
    return records


def _operator_prompts(
    records: list[dict[str, Any]], summaries: set[str], chain_path: str
) -> list[ModelSessionPrompt]:
    content = [
        r
        for r in records
        if r["kind"] in {"prompt", "operator_prompt"}
        and _sha(r["content"].encode("utf-8")) not in summaries
    ]
    operator = [r for r in content if r["kind"] == "operator_prompt"]
    # The UserPromptSubmit twin's instant is when the words were said; the Stop record's is later.
    said: dict[str, datetime] = {}
    chosen: list[tuple[dict[str, Any], EnumPromptOrigin]] = []
    for record in (r for r in content if r["kind"] == "prompt"):
        later = [r for r in operator if r["at"] >= record["at"]]
        twin = next(
            (
                r
                for r in later
                if r["event_id"] not in said and r["content"] == record["content"]
            ),
            None,
        )
        if twin is not None:
            said[twin["event_id"]] = record["at"]
        elif not later:
            # No Stop read after it: a turn that never reached Stop, or words older than the
            # capture. Carried; the carry set may hold more than the operator typed.
            chosen.append((record, EnumPromptOrigin.PROMPT_UNCONFIRMED))
    chosen.extend(
        (
            {**r, "at": said.get(r["event_id"], r["at"])},
            EnumPromptOrigin.OPERATOR_PROMPT,
        )
        for r in operator
    )
    chosen.sort(key=lambda item: (item[0]["at"], item[0]["event_id"]))
    return [
        ModelSessionPrompt(
            prompt_id=str(record["prompt_id"] or record["event_id"]),
            capture_sequence=index,
            text=record["content"],
            said_at=record["at"],
            origin=origin,
            source_ref=f"{chain_path}#{record['event_id']}",
        )
        for index, (record, origin) in enumerate(chosen)
    ]


def _ledger_rows(
    prefixes: list[bytes],
) -> tuple[list[ModelPromptCitation], list[ModelReviewedRange]]:
    citations: list[ModelPromptCitation] = []
    ranges: list[ModelReviewedRange] = []
    for prefix in prefixes:
        for line in prefix.decode("utf-8", errors="replace").splitlines():
            cells = [cell.strip() for cell in line.split(" | ")]
            if len(cells) < 3 or not _ROW_STAMP.match(cells[0]):
                continue
            fields = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
            row_ref = f"{cells[0]} {cells[1]} lane={fields.get('lane', '')}"
            covers = _COVERS.match(fields.get("covers", ""))
            if fields.get("prompt_id") and covers:
                start, end = int(covers.group(1)), int(covers.group(2))
                if end >= start:
                    citations.append(
                        ModelPromptCitation(
                            prompt_id=fields["prompt_id"],
                            start=start,
                            end=end,
                            row_ref=row_ref,
                        )
                    )
            reviewed = _REVIEWED.match(fields.get("reviewed", ""))
            if cells[1] == "RULING" and reviewed:
                ranges.append(
                    ModelReviewedRange(
                        first_prompt_id=reviewed.group(1),
                        last_prompt_id=reviewed.group(2),
                        row_ref=row_ref,
                    )
                )
    return citations, ranges


class HandlerSessionContextSources:
    """Session-tier reads from pinned replay cases or tenant-bound live projections."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        contract_path: Path | None = None,
        content_reader: ProtocolSessionContentReader | None = None,
        ledger_reader: ProtocolSessionLedgerReader | None = None,
        tenant_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._env = env
        binding = load_session_live_binding(
            contract_path
            if contract_path is not None
            else Path(__file__).resolve().parents[1] / "contract.yaml"
        )
        reader = PostgresSessionProjectionReader(binding)
        self._content_reader = content_reader if content_reader is not None else reader
        self._ledger_reader = ledger_reader if ledger_reader is not None else reader
        self._tenant_ids = binding.tenant_ids if tenant_ids is None else tenant_ids

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        modes = [s for s in request.scopes if s.startswith((CASE_SCOPE, SESSION_SCOPE))]
        if len(modes) != 1 or modes[0].endswith(":"):
            return _fail(
                EnumSessionSourceFailure.SCOPE_INVALID,
                "exactly one scope `case:<case_id>` or `session:<session_id>` is required",
            )
        scope = modes[0]
        if scope.startswith(SESSION_SCOPE):
            if request.factor is EnumContextFactor.REPO_STATE:
                return ModelContextSourceResult(
                    status=EnumContextSourceStatus.UNAVAILABLE
                )
            if request.factor is not EnumContextFactor.OPERATOR_WORDS_UNRECORDED:
                return _fail(
                    EnumSessionSourceFailure.FACTOR_NOT_SERVED,
                    f"live mode does not serve {request.factor.value}",
                )
            try:
                return self._live(request, scope.removeprefix(SESSION_SCOPE))
            except _ReadFailureError as failure:
                return _fail(failure.failure, failure.message)
        if request.factor not in PINNED_FACTORS:
            return _fail(
                EnumSessionSourceFailure.FACTOR_NOT_SERVED,
                f"pinned mode does not serve {request.factor.value}",
            )
        try:
            return self._pinned(request, scope.removeprefix(CASE_SCOPE))
        except _ReadFailureError as failure:
            return _fail(failure.failure, failure.message)

    def _root(self) -> Path:
        env = os.environ if self._env is None else self._env
        value = env.get(REPLAY_ROOT_ENV, "")
        if not value or not Path(value).is_dir():
            raise _ReadFailureError(
                EnumSessionSourceFailure.REPLAY_ROOT_MISSING,
                f"{REPLAY_ROOT_ENV} is required, has no default, and must name a directory",
            )
        return Path(value)

    def _pinned(
        self, request: ModelContextSourceRequest, case_id: str
    ) -> ModelContextSourceResult:
        root = self._root()
        manifest_path = root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise _ReadFailureError(
                EnumSessionSourceFailure.CASE_NOT_FOUND,
                f"{MANIFEST_NAME} is missing under {REPLAY_ROOT_ENV}",
            )
        manifest: dict[str, Any] = yaml.safe_load(manifest_path.read_text("utf-8"))
        case = next(
            (c for c in manifest.get("cases", []) if c.get("case_id") == case_id), None
        )
        if case is None:
            raise _ReadFailureError(
                EnumSessionSourceFailure.CASE_NOT_FOUND,
                f"case {case_id} is not in the manifest",
            )
        pin = case["transcript_chain"]
        if request.revision != pin["sha256"]:
            raise _ReadFailureError(
                EnumSessionSourceFailure.PIN_MISMATCH,
                f"revision {request.revision} is not case {case_id}'s transcript-chain sha256",
            )
        if request.factor is EnumContextFactor.REPO_STATE:
            # The T0.2 bundle pins no cwd or branch (the captured events carry none).
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNAVAILABLE)
        chain_path = str(pin["bundle_path"])
        chain = _pinned_bytes(root, chain_path, int(pin["bytes"]), str(pin["sha256"]))
        prefixes = [
            _pinned_bytes(
                root,
                str(entry["bundle_path"]),
                int(entry["prefix_bytes"]),
                str(entry["prefix_sha256"]),
            )
            for entry in case["ledger"]["files"]
        ]
        records, summaries = _chain_records(chain, chain_path)
        prompts = _operator_prompts(records, summaries, chain_path)
        citations, ranges = _ledger_rows(prefixes)
        rows = [
            row
            for prefix in prefixes
            for row in ledger_lines(
                prefix.decode("utf-8", errors="replace"), until=None
            )
        ]
        citations.extend(quote_citations(prompts, rows))
        return _carried(prompts, citations, ranges)

    def _live(
        self, request: ModelContextSourceRequest, session_id: str
    ) -> ModelContextSourceResult:
        if request.access.tenant_id not in self._tenant_ids:
            return ModelContextSourceResult(status=EnumContextSourceStatus.DENIED)
        until = (
            datetime.fromisoformat(request.revision)
            if _SOURCE_STAMP.match(request.revision)
            else None
        )
        watermark = until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else None
        try:
            content = self._content_reader.read_prompt_rows(
                session_id=session_id, until=until
            )
            raw_rows = self._ledger_reader.read_raw_rows(until=until)
        except Exception as exc:
            return ModelContextSourceResult.error(
                EnumContextSourceErrorCode.INVALID_RESPONSE, str(exc)
            )
        prompts = _operator_prompts(
            _join_chunks(content, "session_content"), set(), "session_content"
        )
        rows = ledger_lines("\n".join(raw_rows), until=watermark)
        citations, ranges = _ledger_rows(["\n".join(rows).encode("utf-8")])
        citations.extend(quote_citations(prompts, rows))
        return _carried(prompts, citations, ranges)


def _carried(
    prompts: list[ModelSessionPrompt],
    citations: list[ModelPromptCitation],
    ranges: list[ModelReviewedRange],
) -> ModelContextSourceResult:
    """The carry rule's CARRIED prompts as entries, with every disposition counted."""
    decisions = carry(prompts, citations, ranges)
    carried = [d for d in decisions if d.disposition is EnumCarryDisposition.CARRIED]
    counts = {
        disposition.value: sum(1 for d in decisions if d.disposition is disposition)
        for disposition in EnumCarryDisposition
    }
    counts["unconfirmed_origin"] = sum(
        1 for d in carried if d.prompt.origin is EnumPromptOrigin.PROMPT_UNCONFIRMED
    )
    entries = tuple(
        ModelContextSourceEntry(
            factor=EnumContextFactor.OPERATOR_WORDS_UNRECORDED,
            text=d.prompt.text,
            source_ref=d.prompt.source_ref,
            span=ModelContextSourceSpan(
                unit=EnumContextSpanUnit.CHARACTER, start=0, end=len(d.prompt.text)
            ),
            text_sha256=_sha(d.prompt.text.encode("utf-8")),
            status=EnumContextSourceStatus.RESOLVED,
            verbatim_verified=True,
            as_of=d.prompt.said_at,
        )
        for d in carried
    )
    return ModelContextSourceResult(
        status=(
            EnumContextSourceStatus.RESOLVED
            if entries
            else EnumContextSourceStatus.UNRESOLVED
        ),
        entries=entries,
        entry_counts=counts,
    )


__all__ = [
    "REPLAY_ROOT_ENV",
    "HandlerSessionContextSources",
]
