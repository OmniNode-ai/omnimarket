# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure admission evidence check. No records are created, altered, archived or replayed.

A canonical STATUS's data= JSON names the exact artifact/UUID/bytes and reason.
Its approval is a separately recorded OPERATOR-CONSENT with kind=refusal-disposition-approval.
Both formats are closed schemas; callers must supply a complete canonical snapshot, never a
query page or file-fallback disguised as the database. All uncertainty remains a blocker.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from omnimarket.handlers import ledger_row_grammar as ledger_grammar

from ..models.model_refusal_approval import ModelRefusalApproval
from ..models.model_refusal_disposition import ModelRefusalDisposition
from ..models.model_refusal_disposition_request import ModelRefusalDispositionRequest
from ..models.model_refusal_disposition_result import ModelRefusalDispositionResult

# Other canonical types consult operator environment or local receipt state in
# the shared grammar. Their selected evidence stays blocked in this pure node.
_STATE_INDEPENDENT_TYPES = frozenset(
    {
        "STATUS",
        "CORRECTION",
        "OPERATOR-CONSENT",
        "RULING",
        "MSG",
        "ACK",
        "HOLD",
        "RELEASE",
        "FRICTION",
    }
)


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(
        value.encode("utf-8") if isinstance(value, str) else value
    ).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(text: str | bytes) -> Any:
    return json.loads(text, object_pairs_hook=_unique)


def _time(value: str) -> datetime:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        raise ValueError("timestamp is not canonical UTC seconds")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _row(text: str) -> ledger_grammar.Row:
    if "\n" in text or "\r" in text:
        raise ValueError("continuation rows cannot be proved by the first-line reader")
    row = ledger_grammar.parse_row(text)
    if (
        row is None
        or row.row_type not in _STATE_INDEPENDENT_TYPES
        or ledger_grammar.row_refusals(text)
    ):
        raise ValueError("canonical evidence row has invalid grammar")
    if any(len(values) != 1 for values in row.fields.values()):
        raise ValueError("duplicate canonical binding fields")
    principal, request, via = row.token("principal"), row.token("req"), row.token("via")
    if (
        not principal
        or not request
        or str(UUID(request)) != request
        or not via
        or not via.startswith("bus:")
        or not via[4:]
    ):
        raise ValueError("canonical evidence lacks authenticated bus identity")
    return row


def _artifact(name: str, raw: bytes | None) -> dict[str, str]:
    if raw is None:
        raise ValueError("unreadable or nonregular refusal artifact")
    body = _json(raw)
    expected = {"request_id", "payload", "reason", "queued_at", "refusal"}
    if (
        not isinstance(body, dict)
        or set(body) != expected
        or any(type(v) is not str for v in body.values())
    ):
        raise ValueError("unknown or malformed host refusal schema")
    rid = body["request_id"]
    if str(UUID(rid)) != rid or not re.fullmatch(
        r"[0-9]{20}_" + re.escape(rid) + r"\.json", name
    ):
        raise ValueError("refusal filename and request identity disagree")
    _time(body["queued_at"])
    lines = body["payload"].split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise ValueError("refusal contains no payload rows")
    for line in lines:
        row = ledger_grammar.parse_row(line)
        if (
            row is None
            or row.row_type not in _STATE_INDEPENDENT_TYPES
            or ledger_grammar.row_refusals(line)
            or row.token("req") != rid
        ):
            raise ValueError("unprovable payload or mismatched request identity")
        if any(len(values) != 1 for values in row.fields.values()) or "\r" in line:
            raise ValueError("ambiguous payload fields or continuation")
    return cast("dict[str, str]", body)


def _without_added_principal(canonical: str, payload: str) -> str:
    # The authenticated serve may add exactly one principal header. No other cell changes.
    if canonical == payload:
        return canonical
    original = ledger_grammar.parse_row(payload)
    _row(canonical)
    if original is not None and original.token("principal") is None:
        return re.sub(r" \| principal=[^|\s]+(?= \||$)", "", canonical, count=1)
    return canonical


def _retraction_refs(lines: tuple[str, ...]) -> set[str]:
    refs: set[str] = set()
    # Inspect every cell, including duplicate fields and malformed targeted rows.
    for line in lines:
        for cell in line.split("|"):
            match = re.fullmatch(
                r"\s*(?:corrects|amends|supersedes|revokes)=(.*?)\s*",
                cell,
                re.IGNORECASE,
            )
            if match:
                refs.update(part.strip() for part in match.group(1).split(","))
    return refs


def _retracted(target: str, withdrawn: set[str]) -> bool:
    row = _row(target)
    refs = {row.stamp, _digest(target), "sha256:" + _digest(target)}
    if row.token("lane"):
        refs.add(f"{row.stamp}-{row.token('lane')}")
    if row.token("id"):
        refs.add(row.token("id") or "")
    return bool(refs.intersection(withdrawn))


class HandlerRefusalDisposition:
    def handle(
        self, request: ModelRefusalDispositionRequest
    ) -> ModelRefusalDispositionResult:
        problems: list[str] = []
        artifacts: dict[str, tuple[bytes, dict[str, str]]] = {}
        request_names: dict[str, list[str]] = defaultdict(list)
        for name, raw in request.artifacts:
            try:
                body = _artifact(name, raw)
                assert raw is not None
                if name in artifacts:
                    raise ValueError("duplicate artifact name")
                artifacts[name] = (raw, body)
                request_names[body["request_id"]].append(name)
            except (ValueError, TypeError, UnicodeError) as exc:
                problems.append(f"{name}: {exc}")
        duplicate_names = {
            name
            for names in request_names.values()
            if len(names) != 1
            for name in names
        }
        for name in sorted(duplicate_names):
            problems.append(
                f"{name}: request identity occurs in additional refusal artifacts"
            )
        by_hash: dict[str, list[str]] = defaultdict(list)
        by_request: dict[str, list[str]] = defaultdict(list)
        withdrawn = _retraction_refs(request.canonical_rows)
        dispositions: dict[str, list[tuple[str, ModelRefusalDisposition]]] = (
            defaultdict(list)
        )
        approvals_by_name: dict[str, list[tuple[str, ModelRefusalApproval]]] = (
            defaultdict(list)
        )
        for line in request.canonical_rows:
            by_hash[_digest(line)].append(line)
            parsed = ledger_grammar.parse_row(line)
            if parsed is None:
                continue
            for rid in set(parsed.fields.get("req", [])):
                by_request[rid].append(line)
            kinds = parsed.fields.get("kind", [])
            if any(
                value.startswith("refusal-disposition")
                and value not in {"refusal-disposition", "refusal-disposition-approval"}
                for value in kinds
            ):
                problems.append(f"disposition {_digest(line)}: unknown record kind")
            if "refusal-disposition-approval" in kinds:
                try:
                    approval_row = _row(line)
                    if approval_row.row_type != "OPERATOR-CONSENT":
                        raise ValueError("approval is not canonical operator consent")
                    grant = ModelRefusalApproval.model_validate_json(
                        json.dumps(_json(approval_row.value("data") or ""))
                    )
                    _time(grant.valid_until)
                    approvals_by_name[grant.artifact_name].append((line, grant))
                except (ValueError, TypeError, ValidationError):
                    problems.append(
                        f"approval {_digest(line)}: invalid approval schema or identity"
                    )
            if "refusal-disposition" not in kinds:
                continue
            try:
                row = _row(line)
                if row.row_type != "STATUS":
                    raise ValueError("disposition is not a canonical STATUS")
                data = ModelRefusalDisposition.model_validate_json(
                    json.dumps(_json(row.value("data") or ""))
                )
                dispositions[data.artifact_name].append((line, data))
                if data.artifact_name not in artifacts:
                    raise ValueError("disposition names an unknown refusal artifact")
            except (ValueError, TypeError, ValidationError) as exc:
                problems.append(
                    f"disposition {_digest(line)}: invalid schema"
                    if isinstance(exc, ValidationError)
                    else f"disposition {_digest(line)}: {exc}"
                )
        settled: list[str] = []
        for name, (raw, artifact) in artifacts.items():
            if name in duplicate_names:
                continue
            candidates = dispositions.get(name, [])
            try:
                if len(candidates) != 1:
                    raise ValueError("requires exactly one recorded disposition")
                line, data = candidates[0]
                binding = (name, artifact["request_id"], _digest(raw))
                if (
                    data.artifact_name,
                    data.request_id,
                    data.refusal_sha256,
                ) != binding:
                    raise ValueError(
                        "disposition binding does not match exact refusal bytes"
                    )
                grants = approvals_by_name.get(name, [])
                if len(grants) != 1 or _digest(grants[0][0]) != data.approval_sha256:
                    raise ValueError(
                        "requires exactly one unambiguous scoped operator approval"
                    )
                approvals = by_hash.get(data.approval_sha256, [])
                if len(approvals) != 1:
                    raise ValueError("approval reference is absent or ambiguous")
                authority = approvals[0]
                approval_row = _row(authority)
                if (
                    approval_row.row_type != "OPERATOR-CONSENT"
                    or approval_row.value("kind") != "refusal-disposition-approval"
                ):
                    raise ValueError(
                        "authority is not a separate explicit operator consent"
                    )
                approval = ModelRefusalApproval.model_validate_json(
                    json.dumps(_json(approval_row.value("data") or ""))
                )
                if (
                    approval.artifact_name,
                    approval.request_id,
                    approval.refusal_sha256,
                    approval.reason,
                ) != (*binding, data.reason):
                    raise ValueError(
                        "operator approval does not bind this exact refusal and reason"
                    )
                if (
                    not _time(artifact["queued_at"])
                    <= _time(approval_row.stamp)
                    <= _time(_row(line).stamp)
                    <= request.now
                    <= _time(approval.valid_until)
                ):
                    raise ValueError(
                        "future, stale or out-of-order approval/disposition"
                    )
                evidence: list[str] = []
                for key in data.evidence_sha256:
                    found = by_hash.get(key, [])
                    if not found or (
                        data.reason != "duplicate_delivery" and len(found) != 1
                    ):
                        raise ValueError(
                            "missing or ambiguous independent reason evidence"
                        )
                    evidence.append(found[0])
                for value in [authority, line, *evidence]:
                    parsed = _row(value)
                    if _time(parsed.stamp) > _time(_row(line).stamp) or _retracted(
                        value, withdrawn
                    ):
                        raise ValueError(
                            "evidence or grant is future, amended, superseded or revoked"
                        )
                if data.reason == "duplicate_delivery":
                    payload = artifact["payload"].removesuffix("\n").split("\n")
                    current = by_request.get(data.request_id, [])
                    for candidate in current:
                        _row(candidate)
                    if Counter(_digest(v) for v in current) != Counter(
                        data.evidence_sha256
                    ):
                        raise ValueError(
                            "duplicate proof does not cover every canonical row "
                            "and multiplicity of the request"
                        )
                    normalized = [
                        next(
                            (
                                _without_added_principal(v, p)
                                for p in payload
                                if _without_added_principal(v, p) == p
                            ),
                            v,
                        )
                        for v in evidence
                    ]
                    if Counter(normalized) != Counter(payload):
                        raise ValueError(
                            "duplicate delivery payload bytes are not equivalent"
                        )
                elif data.reason == "superseded":
                    if len(evidence) != 1:
                        raise ValueError(
                            "supersession requires one specific correction event"
                        )
                    correction = _row(evidence[0])
                    payload_stamps = {
                        parsed.stamp
                        for value in artifact["payload"].removesuffix("\n").split("\n")
                        if (parsed := ledger_grammar.parse_row(value)) is not None
                    }
                    if (
                        correction.row_type != "CORRECTION"
                        or set((correction.token("supersedes") or "").split(","))
                        != payload_stamps
                        or correction.token("supersedes-request") != data.request_id
                        or correction.token("supersedes-refusal-sha256")
                        != data.refusal_sha256
                        or _time(correction.stamp) < _time(artifact["queued_at"])
                    ):
                        raise ValueError(
                            "correction does not explicitly supersede this exact refusal"
                        )
                elif evidence != [authority]:
                    raise ValueError(
                        "abandonment requires the explicit operator grant itself as evidence"
                    )
                settled.append(name)
            except (ValueError, TypeError, ValidationError) as exc:
                problems.append(
                    f"{name}: invalid schema"
                    if isinstance(exc, ValidationError)
                    else f"{name}: {exc}"
                )
        return ModelRefusalDispositionResult(
            settled=tuple(sorted(settled)), problems=tuple(problems)
        )
