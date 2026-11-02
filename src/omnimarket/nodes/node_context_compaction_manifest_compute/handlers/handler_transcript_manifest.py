# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Case manifests from transcript files: the `transcript_jsonl` source (plan task CM.1).

A case is one compaction boundary of one session. The transcript is one JSON object per line; a
boundary is a `system` record with subtype `compact_boundary`, and its native summary is the next
`isCompactSummary` user record. For each boundary the handler hashes three things: the boundary
record's bytes, the summary record's bytes and the prefix (the file's bytes before the boundary
record). The case key is a uuid5 over session id, boundary uuid and prefix sha256, so the same
bytes give the same key.

Every boundary is either a case or carries one typed exclusion, checked in this order:
RECOMPACTION (a previous boundary of the file under ten minutes earlier, or a pre-compaction
window under 100,000 tokens), SUMMARY_UNVERIFIED (no summary record, or its length or sha256
differs from the pin the hook capture recorded), LEDGER_UNPINNABLE (no ledger row stamped before
the boundary second), NO_OBLIGATION (a supplied must-survive count under 5) and SECRET_IN_PREFIX
(a credential-shaped string in the prefix bytes). A boundary that passes all five and is not
picked by the seeded per-session draw is NOT_DRAWN, so the manifest never loses a boundary.

A summary with no pin is a case marked `UNPINNED`: without a pin there is nothing to compare its
length and hash against, and the manifest says so rather than calling it verified. The handler
reads no file and no variable; `omnimarket.handlers.context_compaction_bundle` reads
`ONEX_TRANSCRIPT_ROOT`, writes under `ONEX_CONTEXT_REPLAY_ROOT` and fails loudly when either is
unset.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    _RULING_LINE,
    QUOTE_MIN_CHARS,
    ManifestRefusalError,
    _ledger_cut,
    _normal,
    _second,
    _sha,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_transcript_manifest import (
    EnumCaseExclusion,
    ModelLaneAttribution,
    ModelLaneEvidence,
    ModelLedgerCut,
    ModelTranscriptCase,
    ModelTranscriptCounts,
    ModelTranscriptExclusion,
    ModelTranscriptFile,
    ModelTranscriptManifestRequest,
    ModelTranscriptManifestResult,
)

SCHEMA = "omnimarket/context_compaction_manifest/transcript_jsonl/1"
TRANSCRIPT_ROOT_ENV = "ONEX_TRANSCRIPT_ROOT"
REPLAY_ROOT_ENV = "ONEX_CONTEXT_REPLAY_ROOT"
RECOMPACTION_GAP = timedelta(minutes=10)
RECOMPACTION_TOKEN_FLOOR = 100_000
MIN_OBLIGATION_ITEMS = 5
_KEY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, SCHEMA)
LANE_RULE = (
    "RULING quote: the lane of every RULING or OPERATOR-CONSENT row stamped inside the session "
    f"window whose quoted operator words ({QUOTE_MIN_CHARS} or more characters) appear in a "
    "user prompt of the prefix; more than one lane is kept"
)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("sk-key", re.compile(rb"\bsk-[A-Za-z0-9]{20,}")),
    ("pk-key", re.compile(rb"\bpk_[A-Za-z0-9]{20,}")),
    ("aws-key", re.compile(rb"\bAKIA[A-Z0-9]{16}\b")),
    ("github-token", re.compile(rb"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{36}\b")),
    ("github-pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private-key", re.compile(rb"-----BEGIN\s+(?:[A-Z\s]+\s+)?PRIVATE KEY-----")),
    ("bearer", re.compile(rb"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
    (
        "assigned-secret",
        re.compile(
            rb"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)"
            rb"\s*[:=]\s*['\"]?[A-Za-z0-9\-._~+/]{16,}",
            re.IGNORECASE,
        ),
    ),
)

__all__ = [
    "REPLAY_ROOT_ENV",
    "TRANSCRIPT_ROOT_ENV",
    "HandlerTranscriptManifest",
    "MissingRequiredVariableError",
]


class MissingRequiredVariableError(ManifestRefusalError):
    """A required variable is unset or empty. `variable` names it."""

    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} is required and has no default")
        self.variable = variable


@dataclass
class _Boundary:
    path: str
    session_id: str
    uuid: str
    offset: int
    index: int
    moment: datetime | None
    trigger: str | None
    pre_tokens: int | None
    post_tokens: int | None
    boundary_sha: str
    prefix_sha: str
    prefix_records: int
    previous: _Boundary | None
    summary_text: str | None
    summary_record_sha: str | None


@dataclass(frozen=True)
class _Scan:
    boundaries: tuple[_Boundary, ...]
    records: tuple[dict[str, Any], ...]
    counts: tuple[int, int, int]
    first_secret_end: int | None
    first_secret_kind: str | None


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_boundary(record: dict[str, Any]) -> bool:
    return (
        record.get("type") == "system" and record.get("subtype") == "compact_boundary"
    )


def _text_of(record: dict[str, Any]) -> str | None:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return content if isinstance(content, str) else None


def _first_secret(content: bytes) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for kind, pattern in _SECRET_PATTERNS:
        match = pattern.search(content)
        if match is not None and (best is None or match.end() < best[0]):
            best = (match.end(), kind)
    return best


def _scan(file: ModelTranscriptFile) -> _Scan:
    """Walk the file once: boundaries with their running prefix hash, and the line counts."""
    content = file.content
    hasher = hashlib.sha256()
    raw_lines = content.split(b"\n")
    parsed: list[tuple[int, bytes, dict[str, Any]]] = []
    boundaries: list[_Boundary] = []
    mentions = 0
    unparseable = 0
    offset = 0
    records_before = 0
    stem = file.relative_path.rsplit("/", 1)[-1].removesuffix(".jsonl")
    previous: _Boundary | None = None
    open_boundary: _Boundary | None = None
    for number, raw in enumerate(raw_lines):
        is_last = number == len(raw_lines) - 1
        if is_last and not raw:
            break
        whole = raw if is_last else raw + b"\n"
        if b"compact_boundary" in raw:
            mentions += 1
        record: dict[str, Any] | None = None
        if raw.strip():
            try:
                loaded = json.loads(raw)
            except ValueError:
                unparseable += 1
            else:
                if isinstance(loaded, dict):
                    record = loaded
                else:
                    unparseable += 1
        if record is not None:
            if _is_boundary(record):
                metadata = record.get("compactMetadata")
                meta = metadata if isinstance(metadata, dict) else {}
                trigger = meta.get("trigger")
                session = record.get("sessionId")
                built = _Boundary(
                    path=file.relative_path,
                    session_id=session
                    if isinstance(session, str) and session
                    else stem,
                    uuid=str(record.get("uuid") or f"boundary-{number}"),
                    offset=offset,
                    index=len(parsed),
                    moment=_moment(record.get("timestamp")),
                    trigger=trigger if isinstance(trigger, str) else None,
                    pre_tokens=_int(meta.get("preTokens")),
                    post_tokens=_int(meta.get("postTokens")),
                    boundary_sha=_sha(whole),
                    prefix_sha=hasher.copy().hexdigest(),
                    prefix_records=records_before,
                    previous=previous,
                    summary_text=None,
                    summary_record_sha=None,
                )
                boundaries.append(built)
                open_boundary = built
                previous = built
            elif open_boundary is not None and record.get("isCompactSummary") is True:
                text = _text_of(record) if record.get("type") == "user" else None
                if text:
                    open_boundary.summary_text = text
                    open_boundary.summary_record_sha = _sha(whole)
                    open_boundary = None
            parsed.append((offset, whole, record))
        if raw.strip():
            records_before += 1
        hasher.update(whole)
        offset += len(whole)
    secret = _first_secret(content)
    return _Scan(
        boundaries=tuple(boundaries),
        records=tuple(item[2] for item in parsed),
        counts=(mentions, unparseable, len(boundaries)),
        first_secret_end=secret[0] if secret else None,
        first_secret_kind=secret[1] if secret else None,
    )


def _recompaction_detail(boundary: _Boundary) -> str | None:
    previous = boundary.previous
    if previous is None:
        return None
    if (
        boundary.moment is not None
        and previous.moment is not None
        and boundary.moment - previous.moment < RECOMPACTION_GAP
    ):
        minutes = (boundary.moment - previous.moment).total_seconds() / 60
        return f"previous boundary {previous.uuid} is {minutes:.1f} minutes earlier"
    if (
        boundary.pre_tokens is not None
        and boundary.pre_tokens < RECOMPACTION_TOKEN_FLOOR
    ):
        return f"window of {boundary.pre_tokens} tokens is under {RECOMPACTION_TOKEN_FLOOR}"
    return None


def _lane_attribution(
    cuts: list[bytes], records: tuple[dict[str, Any], ...], boundary: _Boundary
) -> ModelLaneAttribution:
    prompts: list[str] = []
    start: datetime | None = None
    for record in records[: boundary.index]:
        moment = _moment(record.get("timestamp"))
        if moment is not None and (start is None or moment < start):
            start = moment
        if (
            record.get("type") == "user"
            and record.get("isMeta") is not True
            and record.get("isCompactSummary") is not True
        ):
            text = _text_of(record)
            if text:
                prompts.append(_normal(text))
    start_stamp = _second(start) if start is not None else ""
    evidence: dict[tuple[str, str], ModelLaneEvidence] = {}
    for prefix in cuts:
        for match in _RULING_LINE.finditer(prefix):
            stamp = match.group(1).decode()
            if stamp < start_stamp:
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
                needle in text for text in prompts
            ):
                evidence[(stamp, lane)] = ModelLaneEvidence(
                    lane=lane,
                    ruling_stamp=stamp,
                    quote_sha256=_sha(quote.encode("utf-8")),
                )
    ordered = tuple(evidence[key] for key in sorted(evidence))
    return ModelLaneAttribution(
        lanes=tuple(sorted({item.lane for item in ordered})),
        rule=LANE_RULE,
        evidence=ordered,
    )


def _draw(
    candidates: list[_Boundary], seed: str, session_id: str, cap: int
) -> list[_Boundary]:
    """Up to `cap` boundaries, spread over trigger and size strata, fixed by the seed."""
    if len(candidates) <= cap:
        return list(candidates)
    sizes = sorted(c.pre_tokens for c in candidates if c.pre_tokens is not None)
    median = sizes[len(sizes) // 2] if sizes else 0

    def stratum(item: _Boundary) -> tuple[str, str]:
        if item.pre_tokens is None:
            return (item.trigger or "unknown", "unknown")
        return (
            item.trigger or "unknown",
            "large" if item.pre_tokens >= median else "small",
        )

    def rank(*parts: object) -> str:
        return _sha("|".join([seed, session_id, *map(str, parts)]).encode("utf-8"))

    groups: dict[tuple[str, str], list[_Boundary]] = {}
    for item in candidates:
        groups.setdefault(stratum(item), []).append(item)
    for members in groups.values():
        members.sort(key=lambda item: rank(item.path, item.uuid))
    order = sorted(groups, key=lambda key: rank(*key))
    picked: list[_Boundary] = []
    while len(picked) < cap and any(groups.values()):
        for key in order:
            if groups[key] and len(picked) < cap:
                picked.append(groups[key].pop(0))
    return picked


class HandlerTranscriptManifest:
    """Build the case manifest of a set of transcript files, with every boundary accounted for."""

    def handle(
        self, request: ModelTranscriptManifestRequest
    ) -> ModelTranscriptManifestResult:
        files = sorted(request.transcripts, key=lambda f: f.relative_path)
        ledger = sorted(request.ledger_files, key=lambda f: f.name)
        pins = {(p.session_id, p.boundary_uuid): p for p in request.summary_pins}
        obligations = {
            o.boundary_uuid: o.must_survive_items for o in request.obligation_counts
        }
        scans = {file.relative_path: _scan(file) for file in files}

        exclusions: list[ModelTranscriptExclusion] = []
        eligible: dict[str, list[_Boundary]] = {}
        checks: dict[tuple[str, str], str] = {}

        def exclude(item: _Boundary, reason: EnumCaseExclusion, detail: str) -> None:
            exclusions.append(
                ModelTranscriptExclusion(
                    session_id=item.session_id,
                    boundary_uuid=item.uuid,
                    transcript_path=item.path,
                    reason=reason,
                    detail=detail,
                )
            )

        for path in sorted(scans):
            scan = scans[path]
            for item in scan.boundaries:
                detail = _recompaction_detail(item)
                if detail is not None:
                    exclude(item, EnumCaseExclusion.RECOMPACTION, detail)
                    continue
                pin = pins.get((item.session_id, item.uuid))
                if item.summary_text is None:
                    exclude(
                        item,
                        EnumCaseExclusion.SUMMARY_UNVERIFIED,
                        "no isCompactSummary record after the boundary",
                    )
                    continue
                digest = _sha(item.summary_text.encode("utf-8"))
                if pin is not None and (
                    pin.summary_length != len(item.summary_text)
                    or pin.summary_sha256 != digest
                ):
                    exclude(
                        item,
                        EnumCaseExclusion.SUMMARY_UNVERIFIED,
                        f"summary is {len(item.summary_text)} characters with sha256 {digest}; "
                        f"pinned {pin.summary_length} characters with sha256 {pin.summary_sha256}",
                    )
                    continue
                checks[(item.path, item.uuid)] = (
                    "PINNED" if pin is not None else "UNPINNED"
                )
                if item.moment is None or not any(
                    _ledger_cut(f.content, _second(item.moment))[2] for f in ledger
                ):
                    exclude(
                        item,
                        EnumCaseExclusion.LEDGER_UNPINNABLE,
                        "no ledger row is stamped before the boundary second"
                        if item.moment is not None
                        else "the boundary record has no timestamp",
                    )
                    continue
                count = obligations.get(item.uuid)
                if count is not None and count < MIN_OBLIGATION_ITEMS:
                    exclude(
                        item,
                        EnumCaseExclusion.NO_OBLIGATION,
                        f"{count} must-survive items, fewer than {MIN_OBLIGATION_ITEMS}",
                    )
                    continue
                if (
                    scan.first_secret_end is not None
                    and scan.first_secret_end <= item.offset
                ):
                    exclude(
                        item,
                        EnumCaseExclusion.SECRET_IN_PREFIX,
                        f"credential-shaped string ({scan.first_secret_kind}) in the prefix bytes",
                    )
                    continue
                eligible.setdefault(item.session_id, []).append(item)

        drawn: list[_Boundary] = []
        for session_id in sorted(eligible):
            chosen = _draw(
                eligible[session_id], request.seed, session_id, request.per_session_cap
            )
            chosen_ids = {(c.path, c.uuid) for c in chosen}
            drawn.extend(chosen)
            for item in eligible[session_id]:
                if (item.path, item.uuid) not in chosen_ids:
                    exclude(
                        item,
                        EnumCaseExclusion.NOT_DRAWN,
                        f"not in the seeded draw of {request.per_session_cap} for the session",
                    )

        cases = tuple(
            self._case(item, scans[item.path], ledger, checks)
            for item in sorted(drawn, key=lambda b: (b.path, b.offset))
        )
        exclusions.sort(
            key=lambda e: (e.transcript_path, e.boundary_uuid, e.reason.value)
        )
        case_counts = Counter(case.transcript_path for case in cases)
        excluded_counts = Counter(e.transcript_path for e in exclusions)
        transcripts = tuple(
            ModelTranscriptCounts(
                relative_path=file.relative_path,
                sha256=_sha(file.content),
                bytes=len(file.content),
                boundary_records=scans[file.relative_path].counts[2],
                lines_mentioning_boundary=scans[file.relative_path].counts[0],
                unparseable_lines=scans[file.relative_path].counts[1],
                cases=case_counts[file.relative_path],
                exclusions=excluded_counts[file.relative_path],
            )
            for file in files
        )
        reason_counts = Counter(e.reason.value for e in exclusions)
        counts = tuple(sorted(reason_counts.items()))
        document = {
            "schema": SCHEMA,
            "source": "transcript_jsonl",
            "required_variables": [TRANSCRIPT_ROOT_ENV, REPLAY_ROOT_ENV],
            "seed": request.seed,
            "per_session_cap": request.per_session_cap,
            "rules": {
                "recompaction_gap_minutes": RECOMPACTION_GAP.total_seconds() / 60,
                "recompaction_token_floor": RECOMPACTION_TOKEN_FLOOR,
                "min_obligation_items": MIN_OBLIGATION_ITEMS,
            },
            "transcripts": [t.model_dump(mode="json") for t in transcripts],
            "cases": [c.model_dump(mode="json") for c in cases],
            "exclusions": [e.model_dump(mode="json") for e in exclusions],
            "exclusion_counts": dict(counts),
        }
        return ModelTranscriptManifestResult(
            cases=cases,
            exclusions=tuple(exclusions),
            transcripts=transcripts,
            exclusion_counts=counts,
            manifest_yaml=yaml.safe_dump(document, sort_keys=True, allow_unicode=True),
        )

    @staticmethod
    def _case(
        item: _Boundary,
        scan: _Scan,
        ledger: list[Any],
        checks: dict[tuple[str, str], str],
    ) -> ModelTranscriptCase:
        assert item.moment is not None
        assert item.summary_text is not None
        assert item.summary_record_sha is not None
        second = _second(item.moment)
        cuts: list[ModelLedgerCut] = []
        prefixes: list[bytes] = []
        for file in ledger:
            end, _last, rows = _ledger_cut(file.content, second)
            prefixes.append(file.content[:end])
            cuts.append(
                ModelLedgerCut(
                    name=file.name,
                    prefix_bytes=end,
                    prefix_sha256=_sha(file.content[:end]),
                    prefix_rows=rows,
                )
            )
        key = uuid.uuid5(
            _KEY_NAMESPACE, f"{item.session_id}\n{item.uuid}\n{item.prefix_sha}"
        )
        return ModelTranscriptCase(
            case_key=str(key),
            session_id=item.session_id,
            boundary_uuid=item.uuid,
            transcript_path=item.path,
            trigger=item.trigger,
            pre_tokens=item.pre_tokens,
            post_tokens=item.post_tokens,
            boundary_at=item.moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            boundary_sha256=item.boundary_sha,
            summary_record_sha256=item.summary_record_sha,
            summary_length=len(item.summary_text),
            summary_sha256=_sha(item.summary_text.encode("utf-8")),
            summary_check="PINNED"
            if checks[(item.path, item.uuid)] == "PINNED"
            else "UNPINNED",
            prefix_bytes=item.offset,
            prefix_sha256=item.prefix_sha,
            prefix_records=item.prefix_records,
            ledger_cuts=tuple(cuts),
            lane_attribution=_lane_attribution(prefixes, scan.records, item),
        )
