# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pinned replay manifest for compaction evaluation from caller-supplied events (OMN-19998).

For each compaction boundary, the transcript prefix, ledger prefix and scoring (the operator's
exact words and a fixed question with an expected answer) are pinned by hash before any model
runs. The caller supplies the hook-event rows and the session-content rows the caller reads.
The bundle helpers at the end are the only I/O and take an explicit root.

Pairing keeps an unpaired PreCompact; a window starts at the previous *paired* PostCompact;
`operator_prompt` is used where it exists and the heuristic reports its measured accuracy; a
repeat with no boundary between is a control; a session may carry more than one lane. Native
summaries are resolved from complete `compact` captures by session, character length and hash,
and exported to `native/<sha256>.txt` separately from the pre-boundary chain. An id alone is
`SUMMARY_UNAVAILABLE`, even if the caller lists it as resolvable.

HASH RULES. The transcript chain is one JSON object per line (keys sorted, UTF-8, no ASCII
escaping), ordered by instant, event id and chunk index; the pin is the sha256 of those bytes. A
ledger file is cut before the first stamped line whose stamp is not strictly before the boundary's
second; the pin is the byte length and sha256 of the bytes before the cut. A later archive roll
moves rows between files, so a roll invalidates the per-file pins and the bundle is rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import yaml

from omnimarket.nodes.node_context_compaction_manifest_compute.models import (
    EnumBoundaryHook,
    EnumRepeatClass,
    EnumSummaryStatus,
    ModelBoundaryPair,
    ModelBoundaryPairing,
    ModelBoundaryPairingRequest,
    ModelBuildManifestRequest,
    ModelBuildManifestResult,
    ModelBundleFile,
    ModelCapturedRecord,
    ModelCaseSpec,
    ModelHookBoundaryEvent,
    ModelLedgerFile,
    ModelPromptClassification,
    ModelPromptWindow,
    ModelPromptWindowRequest,
    ModelRepeatRequest,
    ModelRepeatVerdict,
    ModelVerifyBundleRequest,
    ModelVerifyBundleResult,
)

TICKET = "OMN-19998"
SCHEMA = "omnimarket/context_compaction_manifest/1"
REPLAY_ROOT_ENV = "ONEX_CONTEXT_REPLAY_ROOT"
CHAIN_KINDS = frozenset({"prompt", "operator_prompt", "assistant_reply"})
# Historical measured accuracy of the prompt-classification heuristic.
HEURISTIC_PRECISION = 0.59
HEURISTIC_RECALL = 0.93
HEURISTIC_MAX_CHARS = 1500
TEMPLATE_MIN_REPEATS = 3
QUOTE_MIN_CHARS = 20
CHECKPOINT_REASON = (
    "no checkpoint bytes are recoverable from the captured events; the `checkpoint` capture "
    "record is not built"
)
BRANCH_REASON = (
    "the captured hook events carry no cwd or branch; REPO_STATE is "
    "unavailable in this form"
)
# Curly quotes fold to straight ones, so a ledger paraphrase still matches the captured words.
_QUOTE_FOLD = str.maketrans(
    {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'}
)
_STAMP_LINE = re.compile(rb"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", re.MULTILINE)
_RULING_LINE = re.compile(
    rb"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| (?:RULING|OPERATOR-CONSENT) \|[^\n]*",
    re.MULTILINE,
)

__all__ = [
    "HandlerBoundaryPairing",
    "HandlerPromptWindow",
    "HandlerRepeatClassifier",
    "HandlerReplayBundleVerifier",
    "HandlerReplayManifestBuilder",
    "ManifestRefusalError",
]


class ManifestRefusalError(ValueError):
    """The manifest cannot be built or the bundle cannot be read as asked."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _second(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------------------------
# Pairing, windows, classification, repeats


class HandlerBoundaryPairing:
    """Pair PreCompact with PostCompact per session; report what does not pair."""

    def handle(self, request: ModelBoundaryPairingRequest) -> ModelBoundaryPairing:
        resolvable = set(request.resolvable_record_ids)
        sessions: dict[str, list[ModelHookBoundaryEvent]] = {}
        for event in request.events:
            sessions.setdefault(event.session_id, []).append(event)
        pairs: list[tuple[ModelHookBoundaryEvent, ModelHookBoundaryEvent | None]] = []
        unpaired_post: list[str] = []
        for events in sessions.values():
            pending: ModelHookBoundaryEvent | None = None
            for event in sorted(events, key=lambda e: (e.emitted_at, e.event_id)):
                if event.hook_event_name is EnumBoundaryHook.PRE:
                    if pending is not None:
                        pairs.append((pending, None))
                    pending = event
                elif pending is not None:
                    pairs.append((pending, event))
                    pending = None
                else:
                    unpaired_post.append(event.event_id)
            if pending is not None:
                pairs.append((pending, None))
        pairs.sort(key=lambda pair: (pair[0].emitted_at, pair[0].event_id))
        out = tuple(self._pair(pre, post, pairs, resolvable) for pre, post in pairs)
        return ModelBoundaryPairing(
            pairs=out,
            unpaired_pre=tuple(p.pre_event_id for p in out if not p.paired),
            unpaired_post=tuple(sorted(unpaired_post)),
        )

    @staticmethod
    def _pair(
        pre: ModelHookBoundaryEvent,
        post: ModelHookBoundaryEvent | None,
        pairs: list[tuple[ModelHookBoundaryEvent, ModelHookBoundaryEvent | None]],
        resolvable: set[str],
    ) -> ModelBoundaryPair:
        earlier = [
            done.emitted_at
            for other, done in pairs
            if done is not None
            and other.session_id == pre.session_id
            and done.emitted_at < pre.emitted_at
        ]
        record_id = post.summary_record_id if post is not None else None
        if record_id is None:
            status = EnumSummaryStatus.NOT_CAPTURED
        elif record_id in resolvable:
            status = EnumSummaryStatus.RESOLVED
        else:
            status = EnumSummaryStatus.UNAVAILABLE
        return ModelBoundaryPair(
            session_id=pre.session_id,
            pre_event_id=pre.event_id,
            pre_at=pre.emitted_at,
            post_event_id=post.event_id if post is not None else None,
            post_at=post.emitted_at if post is not None else None,
            trigger=pre.trigger,
            paired=post is not None,
            window_start=max(earlier) if earlier else None,
            summary_status=status,
            summary_length=post.summary_length if post is not None else None,
            summary_sha256=post.summary_sha256 if post is not None else None,
            summary_record_id=record_id,
        )


class HandlerPromptWindow:
    """The operator prompts of one window: exact where `operator_prompt` exists, else heuristic."""

    def handle(self, request: ModelPromptWindowRequest) -> ModelPromptWindow:
        records = request.records
        floor = request.window_start
        if floor is None and records:
            floor = min(record.emitted_at for record in records)
        exact = floor is not None and floor >= request.operator_prompt_capture_start
        window = [
            record
            for record in records
            if (
                request.window_start is None or record.emitted_at > request.window_start
            )
            and record.emitted_at <= request.window_end
        ]
        window.sort(key=lambda record: (record.emitted_at, record.event_id))
        if exact:
            prompts = tuple(
                ModelPromptClassification(
                    event_id=record.event_id,
                    emitted_at=record.emitted_at,
                    operator=True,
                    method="operator_prompt",
                )
                for record in window
                if record.content_kind == "operator_prompt"
            )
            return ModelPromptWindow(
                prompts=prompts, operator_count=len(prompts), exact=True
            )
        seen = Counter(
            record.content for record in records if record.content_kind == "prompt"
        )
        heuristic = tuple(
            ModelPromptClassification(
                event_id=record.event_id,
                emitted_at=record.emitted_at,
                operator=self._operator(record.content, seen),
                method="heuristic",
            )
            for record in window
            if record.content_kind == "prompt"
        )
        return ModelPromptWindow(
            prompts=heuristic,
            operator_count=sum(1 for prompt in heuristic if prompt.operator),
            exact=False,
            heuristic_precision=HEURISTIC_PRECISION,
            heuristic_recall=HEURISTIC_RECALL,
        )

    @staticmethod
    def _operator(content: str, seen: Counter[str]) -> bool:
        if seen[content] >= TEMPLATE_MIN_REPEATS or len(content) > HEURISTIC_MAX_CHARS:
            return False
        return not content.startswith(("<", "/", "# Lane"))


class HandlerRepeatClassifier:
    """A repeat with no compaction between it and the first statement is a control."""

    def handle(self, request: ModelRepeatRequest) -> ModelRepeatVerdict:
        pairing = HandlerBoundaryPairing().handle(
            ModelBoundaryPairingRequest(
                events=tuple(
                    e for e in request.events if e.session_id == request.session_id
                )
            )
        )
        between = tuple(
            pair.pre_event_id
            for pair in pairing.pairs
            if pair.paired and request.first_at < pair.pre_at < request.repeat_at
        )
        return ModelRepeatVerdict(
            verdict=EnumRepeatClass.AFTER_BOUNDARY
            if between
            else EnumRepeatClass.NO_BOUNDARY_BETWEEN,
            boundaries_between=between,
        )


# ---------------------------------------------------------------------------------------------
# The manifest


def _chain_bytes(
    spec: ModelCaseSpec,
    last_pre: datetime,
    events: Iterable[ModelHookBoundaryEvent],
    records: Iterable[ModelCapturedRecord],
) -> tuple[bytes, list[ModelCapturedRecord]]:
    rows: list[tuple[datetime, str, int, dict[str, Any]]] = []
    kept: list[ModelCapturedRecord] = []
    for record in records:
        if (
            record.session_id == spec.session_id
            and record.content_kind in CHAIN_KINDS
            and record.emitted_at <= last_pre
        ):
            kept.append(record)
            rows.append(
                (
                    record.emitted_at,
                    record.event_id,
                    record.chunk_index,
                    {
                        "kind": record.content_kind,
                        "event_id": record.event_id,
                        "session_id": record.session_id,
                        "emitted_at": _iso(record.emitted_at),
                        "chunk_index": record.chunk_index,
                        "chunk_count": record.chunk_count,
                        "content": record.content,
                        "content_sha256": record.content_sha256,
                    },
                )
            )
    for event in events:
        if event.session_id == spec.session_id and event.emitted_at <= last_pre:
            rows.append(
                (
                    event.emitted_at,
                    event.event_id,
                    0,
                    {
                        "kind": f"hook:{event.hook_event_name.value}",
                        "event_id": event.event_id,
                        "session_id": event.session_id,
                        "emitted_at": _iso(event.emitted_at),
                        "trigger": event.trigger,
                        "lane": event.lane,
                        "model": event.model,
                        "summary_length": event.summary_length,
                        "summary_sha256": event.summary_sha256,
                        "summary_record_id": event.summary_record_id,
                    },
                )
            )
    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    lines = (
        json.dumps(row[3], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        for row in rows
    )
    text = "".join(f"{line}\n" for line in lines)
    return text.encode("utf-8"), kept


def _ledger_cut(content: bytes, boundary_second: str) -> tuple[int, str | None, int]:
    """Prefix length, last stamp and stamped-row count before the boundary's second."""
    cutoff = boundary_second.encode()
    last: bytes | None = None
    rows = 0
    for match in _STAMP_LINE.finditer(content):
        if match.group(1) >= cutoff:
            return match.start(), last.decode() if last else None, rows
        last = match.group(1)
        rows += 1
    return len(content), last.decode() if last else None, rows


def _normal(text: str) -> str:
    folded = text.translate(_QUOTE_FOLD)
    return " ".join(folded.casefold().split())


def _lanes(
    prefixes: Iterable[bytes], start_stamp: str, prompts: list[str]
) -> list[str]:
    """Lanes whose RULING quote matches a prompt of the session, more than one allowed."""
    normal_prompts = [_normal(prompt) for prompt in prompts]
    lanes: set[str] = set()
    for prefix in prefixes:
        for match in _RULING_LINE.finditer(prefix):
            if match.group(1).decode() < start_stamp:
                continue
            cells = match.group(0).decode("utf-8", errors="replace").split(" | ")
            lane = next((c[5:] for c in cells if c.startswith("lane=")), None)
            quote = next(
                (c[1:-1] for c in cells if len(c) > 2 and c[0] == c[-1] == '"'), None
            )
            if lane is None or quote is None:
                continue
            needle = _normal(quote)
            if len(needle) >= QUOTE_MIN_CHARS and any(
                needle in text for text in normal_prompts
            ):
                lanes.add(lane)
    return sorted(lanes)


def _ruling_in(stamp: str, prefixes: Iterable[bytes]) -> bool:
    needle = stamp.encode()
    return any(
        m.group(1) == needle
        for prefix in prefixes
        for m in _RULING_LINE.finditer(prefix)
    )


def _native_summary(
    pair: ModelBoundaryPair, records: tuple[ModelCapturedRecord, ...]
) -> bytes | None:
    """Resolve a summary from complete compact captures, bound by session, length and hash.

    Compact captures happen after PreCompact, outside the input chain's cutoff. They must
    be exported separately, never added to the package's pre-boundary context.
    """
    if pair.summary_record_id is None or pair.summary_sha256 is None:
        return None
    groups: dict[tuple[str, str], list[ModelCapturedRecord]] = {}
    for record in records:
        if record.session_id == pair.session_id and record.content_kind == "compact":
            # Projection event ids address individual chunks, not the captured item.
            # The producer stamps the full item's hash on every chunk.
            key = (
                ("sha256", record.content_sha256)
                if record.content_sha256 is not None
                else ("event", record.event_id)
            )
            groups.setdefault(key, []).append(record)
    for (kind, digest), chunks in groups.items():
        chunks.sort(key=lambda record: record.chunk_index)
        count = chunks[0].chunk_count
        if (
            count < 1
            or len(chunks) != count
            or [record.chunk_index for record in chunks] != list(range(count))
            or any(record.chunk_count != count for record in chunks)
        ):
            continue
        package = "".join(record.content for record in chunks)
        if kind == "sha256" and _sha(package.encode("utf-8")) != digest:
            continue
        try:
            rows = [json.loads(line) for line in package.splitlines() if line.strip()]
        except json.JSONDecodeError:
            continue
        if (
            not rows
            or not isinstance(rows[0], dict)
            or not (
                rows[0].get("type") == "system"
                and rows[0].get("subtype") == "compact_boundary"
            )
        ):
            continue
        for row in rows[1:]:
            if not isinstance(row, dict) or row.get("isCompactSummary") is not True:
                continue
            message = row.get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if isinstance(text, list):
                text = "\n".join(
                    block["text"]
                    for block in text
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
            if not isinstance(text, str):
                continue
            content = text.encode("utf-8")
            if (
                len(text) == pair.summary_length
                and _sha(content) == pair.summary_sha256
            ):
                return content
    return None


class HandlerReplayManifestBuilder:
    """Build the manifest and the bundle of its cases, or refuse naming the broken pin."""

    def handle(self, request: ModelBuildManifestRequest) -> ModelBuildManifestResult:
        pairing = HandlerBoundaryPairing().handle(
            ModelBoundaryPairingRequest(
                events=request.events,
                resolvable_record_ids=request.resolvable_record_ids,
            )
        )
        pairs = {pair.pre_event_id: pair for pair in pairing.pairs}
        bundle: dict[str, bytes] = {}
        cases = [self._case(spec, request, pairs, bundle) for spec in request.specs]
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "ticket": TICKET,
            "plan_task": "T0.2",
            "census_ref": request.census_ref,
            "source": {
                "kind": "lab_captured_events",
                "tables": [
                    "hook-event rows",
                    "session-content rows",
                ],
                "access": "read-only",
                "transcripts_read": False,
            },
            "replay_root_env": f"{REPLAY_ROOT_ENV} (required, no default)",
            "hash_rules": {
                "transcript_chain": (
                    "sha256 of the bundle file: one JSON object per line, keys sorted, UTF-8, "
                    "ordered by instant, event id and chunk index; kinds prompt, operator_prompt, "
                    "assistant_reply and the hook boundary events of the session, up to and "
                    "including the last PreCompact of the case"
                ),
                "ledger": (
                    "per file, the bytes before the first stamped line whose stamp is not "
                    "strictly before the boundary's second: byte length and sha256"
                ),
                "operator_prompt_count": (
                    f"exact from the `operator_prompt` kind where it exists; otherwise a "
                    f"heuristic of precision {HEURISTIC_PRECISION} and recall {HEURISTIC_RECALL}"
                ),
            },
            "profile": {
                "name": "compaction_orchestrator",
                "version": request.profile_version,
                "status": "pinned" if request.profile_version else "not_built",
            },
            "cases": cases,
        }
        text = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=100)
        return ModelBuildManifestResult(
            manifest_yaml=text,
            bundle_files=tuple(
                ModelBundleFile(path=path, content=bundle[path])
                for path in sorted(bundle)
            ),
            case_ids=tuple(spec.case_id for spec in request.specs),
        )

    def _case(
        self,
        spec: ModelCaseSpec,
        request: ModelBuildManifestRequest,
        pairs: Mapping[str, ModelBoundaryPair],
        bundle: dict[str, bytes],
    ) -> dict[str, Any]:
        chosen: list[ModelBoundaryPair] = []
        for event_id in spec.boundary_pre_event_ids:
            pair = pairs.get(event_id)
            if pair is None or pair.session_id != spec.session_id:
                raise ManifestRefusalError(
                    f"{spec.case_id}: boundary {event_id} is not a PreCompact of session "
                    f"{spec.session_id}"
                )
            chosen.append(pair)
        chosen.sort(key=lambda pair: pair.pre_at)
        last_pre = chosen[-1].pre_at
        chain, kept = _chain_bytes(spec, last_pre, request.events, request.records)
        chain_path = f"{spec.case_id}/transcript_chain.jsonl"
        bundle[chain_path] = chain

        by_id = {record.event_id: record for record in kept}
        ledger_files = self._ledger(request.ledger_files, chosen, bundle)
        prefixes = [bundle[entry["bundle_path"]] for entry in ledger_files["files"]]
        statements = [
            self._statement(spec, s, by_id, prefixes, first_pre=chosen[0].pre_at)
            for s in spec.expected_statements
        ]
        evidence = [
            self._evidence(spec, e, request.records, last_pre)
            for e in spec.post_boundary_evidence
        ]
        model_id = next(
            (e.model for e in request.events if e.event_id == chosen[-1].pre_event_id),
            None,
        )
        first_stamp = (
            _second(min(record.emitted_at for record in kept))
            if kept
            else _second(last_pre)
        )
        prompts = [
            r.content for r in kept if r.content_kind in {"prompt", "operator_prompt"}
        ]
        return {
            "case_id": spec.case_id,
            "finding": spec.finding,
            "census_status": spec.census_status,
            "census_ref": spec.census_ref,
            "session_id": spec.session_id,
            "lanes": _lanes(prefixes, first_stamp, prompts),
            "model_id": model_id,
            "profile_version": request.profile_version,
            "two_boundaries": len(chosen) >= 2,
            "unrecorded_instruction_before_first_boundary": any(
                item["not_recorded_at_first_boundary"] for item in statements
            ),
            "boundaries": [
                self._boundary(pair, request, ledger_files, bundle) for pair in chosen
            ],
            "transcript_chain": {
                "bundle_path": chain_path,
                "records": len(chain.splitlines()),
                "bytes": len(chain),
                "sha256": _sha(chain),
                "last_emitted_at": _iso(last_pre),
            },
            "ledger": {
                "rule": "bytes before the first stamped line not before the boundary second",
                "files": ledger_files["files"],
            },
            "checkpoint": {
                "status": "EXCLUDED",
                "factor": "ORCHESTRATION_STATE",
                "reason": CHECKPOINT_REASON,
            },
            "branch": {
                "status": "EXCLUDED",
                "factor": "REPO_STATE",
                "reason": BRANCH_REASON,
            },
            "scoring": {
                "expected_statements": statements,
                "post_boundary_evidence": evidence,
                "question": spec.question,
                "expected_answer": spec.expected_answer,
            },
        }

    @staticmethod
    def _ledger(
        files: tuple[ModelLedgerFile, ...],
        chosen: list[ModelBoundaryPair],
        bundle: dict[str, bytes],
    ) -> dict[str, Any]:
        """Pin each file at the last boundary; each boundary's own cut is a shorter prefix."""
        entries: list[dict[str, Any]] = []
        cuts: dict[str, list[dict[str, Any]]] = {
            pair.pre_event_id: [] for pair in chosen
        }
        for file in sorted(files, key=lambda f: f.name):
            for pair in chosen:
                end, _last, _rows = _ledger_cut(file.content, _second(pair.pre_at))
                cuts[pair.pre_event_id].append(
                    {
                        "name": file.name,
                        "prefix_bytes": end,
                        "prefix_sha256": _sha(file.content[:end]),
                    }
                )
            end, last, rows = _ledger_cut(file.content, _second(chosen[-1].pre_at))
            prefix = file.content[:end]
            digest = _sha(prefix)
            path = f"ledger/{digest}.prefix"
            bundle[path] = prefix
            entries.append(
                {
                    "name": file.name,
                    "bundle_path": path,
                    "prefix_bytes": end,
                    "prefix_sha256": digest,
                    "prefix_rows": rows,
                    "last_stamp": last,
                }
            )
        return {"files": entries, "cuts": cuts}

    @staticmethod
    def _boundary(
        pair: ModelBoundaryPair,
        request: ModelBuildManifestRequest,
        ledger_files: dict[str, Any],
        bundle: dict[str, bytes],
    ) -> dict[str, Any]:
        summary = {
            "status": (
                EnumSummaryStatus.UNAVAILABLE.value
                if pair.summary_record_id is not None
                else EnumSummaryStatus.NOT_CAPTURED.value
            ),
            "length": pair.summary_length,
            "sha256": pair.summary_sha256,
            "content_record_id": pair.summary_record_id,
        }
        native = _native_summary(pair, request.records)
        if native is not None:
            path = f"native/{_sha(native)}.txt"
            bundle[path] = native
            summary.update(
                status=EnumSummaryStatus.RESOLVED.value,
                bundle_path=path,
                bytes=len(native),
            )
        window = HandlerPromptWindow().handle(
            ModelPromptWindowRequest(
                records=tuple(
                    r for r in request.records if r.session_id == pair.session_id
                ),
                window_start=pair.window_start,
                window_end=pair.pre_at,
                operator_prompt_capture_start=min(
                    (
                        r.emitted_at
                        for r in request.records
                        if r.content_kind == "operator_prompt"
                    ),
                    default=pair.pre_at,
                ),
            )
        )
        return {
            "pre_event_id": pair.pre_event_id,
            "pre_at": _iso(pair.pre_at),
            "post_event_id": pair.post_event_id,
            "post_at": _iso(pair.post_at) if pair.post_at else None,
            "trigger": pair.trigger,
            "paired": pair.paired,
            "window_start": _iso(pair.window_start) if pair.window_start else None,
            "operator_prompts_in_window": window.operator_count,
            "operator_prompts_exact": window.exact,
            "summary": summary,
            "ledger_cuts": ledger_files["cuts"][pair.pre_event_id],
        }

    @staticmethod
    def _statement(
        spec: ModelCaseSpec,
        statement: Any,
        by_id: Mapping[str, ModelCapturedRecord],
        prefixes: list[bytes],
        *,
        first_pre: datetime,
    ) -> dict[str, Any]:
        record = by_id.get(statement.prompt_event_id)
        if record is None:
            raise ManifestRefusalError(
                f"{spec.case_id}: statement {statement.statement_id} names prompt "
                f"{statement.prompt_event_id}, which is not in the chain before the last boundary"
            )
        if statement.quote is None and not statement.required_spans:
            raise ManifestRefusalError(
                f"{spec.case_id}: {statement.statement_id} has no quote or span"
            )
        for words in ([statement.quote] if statement.quote else []) + list(
            statement.required_spans
        ):
            if words not in record.content:
                raise ManifestRefusalError(
                    f"{spec.case_id}: {statement.statement_id} words {words!r} are not verbatim "
                    f"in prompt {statement.prompt_event_id}"
                )
        if statement.ledger_stamp is not None and not _ruling_in(
            statement.ledger_stamp, prefixes
        ):
            raise ManifestRefusalError(
                f"{spec.case_id}: {statement.statement_id} lists {statement.ledger_stamp}, which "
                "is not a RULING row in the pinned ledger prefix"
            )
        return {
            "statement_id": statement.statement_id,
            "prompt_event_id": statement.prompt_event_id,
            "prompt_emitted_at": _iso(record.emitted_at),
            "prompt_sha256": _sha(record.content.encode("utf-8")),
            "ledger_stamp": statement.ledger_stamp,
            # True when the instruction came before the first boundary and no ledger row
            # recorded it by then (never recorded, or recorded only after that boundary).
            "not_recorded_at_first_boundary": record.emitted_at < first_pre
            and (
                statement.ledger_stamp is None
                or statement.ledger_stamp >= _second(first_pre)
            ),
            "quote": statement.quote,
            "required_spans": list(statement.required_spans),
            "withheld_reason": statement.withheld_reason,
        }

    @staticmethod
    def _evidence(
        spec: ModelCaseSpec,
        item: Any,
        records: tuple[ModelCapturedRecord, ...],
        last_pre: datetime,
    ) -> dict[str, Any]:
        record = next((r for r in records if r.event_id == item.event_id), None)
        if (
            record is None
            or record.session_id != spec.session_id
            or record.emitted_at <= last_pre
        ):
            raise ManifestRefusalError(
                f"{spec.case_id}: post-boundary evidence {item.event_id} is not a record of the "
                "session after the last boundary"
            )
        for words in item.required_spans:
            if words not in record.content:
                raise ManifestRefusalError(
                    f"{spec.case_id}: evidence {item.event_id} does not contain {words!r}"
                )
        return {
            "event_id": item.event_id,
            "emitted_at": _iso(record.emitted_at),
            "kind": item.kind,
            "required_spans": list(item.required_spans),
            "content_sha256": _sha(record.content.encode("utf-8")),
        }


# ---------------------------------------------------------------------------------------------
# Verification against a bundle


class HandlerReplayBundleVerifier:
    """Recompute every pin from the bundle files; list each mismatch."""

    def handle(self, request: ModelVerifyBundleRequest) -> ModelVerifyBundleResult:
        manifest = yaml.safe_load(request.manifest_yaml)
        files = {file.path: file.content for file in request.bundle_files}
        failures: list[str] = []
        cases = manifest["cases"]
        for case in cases:
            failures.extend(self._case(case, files))
        return ModelVerifyBundleResult(
            ok=not failures, cases_checked=len(cases), failures=tuple(failures)
        )

    def _case(self, case: dict[str, Any], files: Mapping[str, bytes]) -> list[str]:
        cid = case["case_id"]
        failures: list[str] = []
        chain_pin = case["transcript_chain"]
        chain = files.get(chain_pin["bundle_path"])
        contents: dict[str, str] = {}
        if chain is None:
            failures.append(
                f"{cid}: transcript chain {chain_pin['bundle_path']} missing"
            )
        else:
            if len(chain) != chain_pin["bytes"]:
                failures.append(
                    f"{cid}: transcript chain is {len(chain)} bytes, pinned {chain_pin['bytes']}"
                )
            if _sha(chain) != chain_pin["sha256"]:
                failures.append(f"{cid}: transcript chain sha256 mismatch")
            for line in chain.decode("utf-8").splitlines():
                row = json.loads(line)
                if "content" in row:
                    contents[row["event_id"]] = row["content"]
        prefixes: list[bytes] = []
        for entry in case["ledger"]["files"]:
            prefix = files.get(entry["bundle_path"])
            if prefix is None:
                failures.append(f"{cid}: ledger prefix for {entry['name']} missing")
                continue
            prefixes.append(prefix)
            if (
                len(prefix) != entry["prefix_bytes"]
                or _sha(prefix) != entry["prefix_sha256"]
            ):
                failures.append(
                    f"{cid}: ledger {entry['name']} prefix sha256 or length mismatch"
                )
        for boundary in case["boundaries"]:
            summary = boundary["summary"]
            if summary["status"] == EnumSummaryStatus.RESOLVED.value:
                native = files.get(summary.get("bundle_path", ""))
                if native is None:
                    failures.append(f"{cid}: resolved native summary file missing")
                elif len(native) != summary.get("bytes") or _sha(native) != summary.get(
                    "sha256"
                ):
                    failures.append(f"{cid}: native summary sha256 or length mismatch")
            for cut in boundary["ledger_cuts"]:
                entry = next(
                    (e for e in case["ledger"]["files"] if e["name"] == cut["name"]),
                    None,
                )
                prefix = files.get(entry["bundle_path"]) if entry else None
                if prefix is None:
                    continue
                if _sha(prefix[: cut["prefix_bytes"]]) != cut["prefix_sha256"]:
                    failures.append(
                        f"{cid}: ledger {cut['name']} cut at {boundary['pre_event_id']} "
                        "sha256 mismatch"
                    )
        for statement in case["scoring"]["expected_statements"]:
            stamp = statement["ledger_stamp"]
            if stamp is not None and not _ruling_in(stamp, prefixes):
                failures.append(
                    f"{cid}: ruling {stamp} not found in the pinned ledger prefix"
                )
            if chain is None:
                continue
            content = contents.get(statement["prompt_event_id"])
            words = ([statement["quote"]] if statement["quote"] else []) + statement[
                "required_spans"
            ]
            if content is None:
                failures.append(
                    f"{cid}: prompt {statement['prompt_event_id']} not in the chain"
                )
                continue
            if _sha(content.encode("utf-8")) != statement["prompt_sha256"]:
                failures.append(
                    f"{cid}: prompt {statement['prompt_event_id']} sha256 mismatch"
                )
            failures.extend(
                f"{cid}: words {text!r} not verbatim in prompt {statement['prompt_event_id']}"
                for text in words
                if text not in content
            )
        return failures
