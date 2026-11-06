# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic evidence only: no actual refusal, network or ledger write."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from omnimarket.nodes.node_ledger_refusal_disposition_compute import (
    HandlerRefusalDisposition,
    ModelRefusalDispositionRequest,
    ModelRefusalDispositionResult,
)

pytestmark = pytest.mark.unit
RID = "11111111-2222-4333-8444-555555555555"
NAME = "01792872595913136000_" + RID + ".json"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PAYLOAD = f"2026-11-04T09:00:00Z | STATUS | lane=source | req={RID} | via=bus:h-test | observed"


def digest(value: bytes | str) -> str:
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def record(
    kind: str,
    body: dict[str, object],
    stamp: str,
    row_type: str = "STATUS",
    rid: str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
) -> str:
    scopes = (
        ' | "Approve this exact disposition"'
        " | APPROVED SCOPE: this request and digest only"
        " | OUT OF SCOPE: other requests"
        if row_type == "OPERATOR-CONSENT"
        else ""
    )
    cells = (
        stamp,
        row_type,
        "lane=operator",
        "principal=operator-test",
        f"req={rid}",
        "via=bus:h-test",
        f"kind={kind}",
        f"data={json.dumps(body, sort_keys=True, separators=(',', ':'))}",
    )
    return " | ".join(cells) + scopes


def fixture(
    reason: str = "duplicate_delivery",
) -> tuple[bytes, list[str], dict[str, object], dict[str, object]]:
    raw = json.dumps(
        {
            "request_id": RID,
            "payload": PAYLOAD,
            "reason": "no answer",
            "queued_at": "2026-11-04T09:01:00Z",
            "refusal": "final refusal",
        },
        sort_keys=True,
    ).encode()
    binding = {
        "artifact_name": NAME,
        "request_id": RID,
        "refusal_sha256": digest(raw),
        "reason": reason,
    }
    grant: dict[str, object] = {
        "schema_version": "ledger-refusal-approval/v1",
        **binding,
        "valid_until": "2026-11-05T00:00:00Z",
    }
    authority = record(
        "refusal-disposition-approval",
        grant,
        "2026-11-04T10:00:00Z",
        "OPERATOR-CONSENT",
    )
    evidence = PAYLOAD.replace(" | STATUS |", " | STATUS | principal=operator-test |")
    if reason == "superseded":
        evidence = (
            "2026-11-04T10:01:00Z | CORRECTION | lane=source | principal=operator-test"
            " | req=bbbbbbbb-cccc-4ddd-8eee-ffffffffffff | via=bus:h-test"
            f" | supersedes=2026-11-04T09:00:00Z | supersedes-request={RID}"
            f" | supersedes-refusal-sha256={digest(raw)} | replacement recorded"
        )
    elif reason == "operator_abandoned":
        evidence = authority
    data: dict[str, object] = {
        "schema_version": "ledger-refusal-disposition/v1",
        **binding,
        "approval_sha256": digest(authority),
        "evidence_sha256": [digest(evidence)],
    }
    disposition = record(
        "refusal-disposition",
        data,
        "2026-11-04T11:00:00Z",
        rid="cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa",
    )
    return raw, list(dict.fromkeys([evidence, authority, disposition])), grant, data


def evaluate(
    raw: bytes | None,
    rows: list[str],
    extras: tuple[tuple[str, bytes | None], ...] = (),
) -> ModelRefusalDispositionResult:
    return HandlerRefusalDisposition().handle(
        ModelRefusalDispositionRequest(
            artifacts=((NAME, raw), *extras), canonical_rows=tuple(rows), now=NOW
        )
    )


@pytest.mark.parametrize(
    "reason", ["duplicate_delivery", "superseded", "operator_abandoned"]
)
def test_exact_separate_operator_approval_and_reason_evidence(reason: str) -> None:
    raw, rows, _, _ = fixture(reason)
    before = raw
    result = evaluate(raw, rows)
    assert result.settled == (NAME,)
    assert result.problems == ()
    assert raw == before


def test_absent_disposition_or_authority_never_clears_refusal() -> None:
    raw, rows, _, _ = fixture()
    assert evaluate(raw, []).problems
    assert evaluate(raw, [rows[0], rows[-1]]).problems
    assert not evaluate(raw, []).settled


@pytest.mark.parametrize(
    "change",
    [
        "bytes",
        "unreadable",
        "bad_json",
        "duplicate_json_key",
        "unknown_key",
        "request_mismatch",
        "bad_time",
        "continuation",
    ],
)
def test_malformed_or_changed_original_remains_blocked(change: str) -> None:
    original, rows, _, _ = fixture()
    raw: bytes | None = original
    body = json.loads(original)
    if change == "bytes":
        raw = original + b" "
    elif change == "unreadable":
        raw = None
    elif change == "bad_json":
        raw = b"{"
    elif change == "duplicate_json_key":
        raw = original[:-1] + b',"request_id":"' + RID.encode() + b'"}'
    elif change == "unknown_key":
        body["extra"] = 1
        raw = json.dumps(body).encode()
    elif change == "request_mismatch":
        body["request_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        raw = json.dumps(body).encode()
    elif change == "bad_time":
        body["queued_at"] = "yesterday"
        raw = json.dumps(body).encode()
    elif change == "continuation":
        body["payload"] += "\ncontinuation"
        raw = json.dumps(body).encode()
    assert evaluate(raw, rows).problems
    assert not evaluate(raw, rows).settled


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "v99"),
        ("reason", "too_old"),
        ("request_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        ("refusal_sha256", "0" * 64),
        ("artifact_name", "another.json"),
        ("evidence_sha256", []),
        ("approval_sha256", "0" * 64),
        ("unexpected", True),
    ],
)
def test_bad_disposition_schema_or_binding(field: str, value: object) -> None:
    raw, rows, _, data = fixture()
    data[field] = value
    rows[-1] = record("refusal-disposition", data, "2026-11-04T11:00:00Z")
    result = evaluate(raw, rows)
    assert result.problems
    assert not result.settled


@pytest.mark.parametrize(
    "change",
    [
        "wrong_type",
        "duplicate_field",
        "expired",
        "future",
        "reason",
        "same_writer_status",
    ],
)
def test_unqualified_or_stale_approval_cannot_supply_authority(change: str) -> None:
    raw, rows, grant, data = fixture()
    if change == "expired":
        grant["valid_until"] = "2026-11-04T11:30:00Z"
    if change == "reason":
        grant["reason"] = "operator_abandoned"
    kind = (
        "STATUS"
        if change in ("wrong_type", "same_writer_status")
        else "OPERATOR-CONSENT"
    )
    stamp = "2026-11-04T12:30:00Z" if change == "future" else "2026-11-04T10:00:00Z"
    authority = record("refusal-disposition-approval", grant, stamp, kind)
    if change == "duplicate_field":
        authority += " | principal=another"
    data["approval_sha256"] = digest(authority)
    rows[1] = authority
    rows[-1] = record("refusal-disposition", data, "2026-11-04T11:00:00Z")
    assert evaluate(raw, rows).problems
    assert not evaluate(raw, rows).settled


def test_current_request_identity_alone_is_not_duplicate_delivery() -> None:
    raw, rows, _, _ = fixture()
    rows[0] = rows[0].replace("observed", "different event")
    assert evaluate(raw, rows).problems


def test_extra_or_duplicate_rows_under_original_request_block_proof() -> None:
    raw, rows, _, _ = fixture()
    for extra in [PAYLOAD, PAYLOAD.replace("observed", "extra event")]:
        assert evaluate(raw, [*rows, extra]).problems
        assert not evaluate(raw, [*rows, extra]).settled


def test_additional_unadjudicated_artifact_and_duplicate_request_fail_closed() -> None:
    raw, rows, _, _ = fixture()
    for name, data in [
        ("unrecognized.txt", b"x"),
        ("01792872595913136001_" + RID + ".json", raw),
        ("directory", None),
    ]:
        result = evaluate(raw, rows, ((name, data),))
        assert result.problems


def test_conflicting_duplicate_or_retracted_records_block_regardless_of_order() -> None:
    raw, rows, _, _ = fixture()
    amendment = (
        "2026-11-04T11:30:00Z | CORRECTION | lane=operator | principal=operator-test"
        " | req=dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb | via=bus:h-test"
        " | supersedes=2026-11-04T10:00:00Z | approval withdrawn"
    )
    for extra in [rows[-1], amendment]:
        assert evaluate(raw, [*rows, extra]).problems
        assert evaluate(raw, list(reversed([*rows, extra]))).problems


def test_no_disposition_for_unknown_artifact_is_accepted() -> None:
    _, rows, _, _ = fixture()
    result = HandlerRefusalDisposition().handle(
        ModelRefusalDispositionRequest(
            artifacts=(), canonical_rows=tuple(rows), now=NOW
        )
    )
    assert result.problems


@pytest.mark.parametrize(
    "case", ["conflicting_grant", "hidden_revocation", "unrelated_correction"]
)
def test_independent_review_false_admission_regressions(case: str) -> None:
    reason = "superseded" if case == "unrelated_correction" else "duplicate_delivery"
    raw, rows, grant, data = fixture(reason)
    if case == "conflicting_grant":
        grant["reason"] = "operator_abandoned"
        rows.append(
            record(
                "refusal-disposition-approval",
                grant,
                "2026-11-04T10:30:00Z",
                "OPERATOR-CONSENT",
                "dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb",
            )
        )
    elif case == "hidden_revocation":
        rows.append(
            "2026-11-04T11:30:00Z | STATUS | lane=operator | revokes=unrelated"
            f" | revokes={data['approval_sha256']} | withdrawn"
        )
    else:
        rows[0] = rows[0].replace(
            "supersedes=2026-11-04T09:00:00Z", "corrects=2025-01-01T00:00:00Z"
        )
        data["evidence_sha256"] = [digest(rows[0])]
        rows[-1] = record("refusal-disposition", data, "2026-11-04T11:00:00Z")
    assert evaluate(raw, rows).problems
    assert evaluate(raw, list(reversed(rows))).problems


@pytest.mark.parametrize(
    "reference",
    ["2026-11-04T10:00:00Z", "2026-11-04T10:00:00Z-operator", "hash", "sha256hash"],
)
def test_all_targeted_revocation_references_and_malformed_rows_block(
    reference: str,
) -> None:
    raw, rows, _, data = fixture()
    if reference == "hash":
        reference = str(data["approval_sha256"])
    elif reference == "sha256hash":
        reference = "sha256:" + str(data["approval_sha256"])
    for prefix in [
        "2026-11-04T11:30:00Z | CORRECTION | lane=operator",
        "malformed record",
    ]:
        withdrawn = prefix + " | revokes=unrelated | revokes=" + reference
        assert evaluate(raw, [*rows, withdrawn]).problems


def test_duplicate_specific_supersession_evidence_is_ambiguous() -> None:
    raw, rows, _, _ = fixture("superseded")
    assert evaluate(raw, [*rows, rows[0]]).problems


@pytest.mark.parametrize(
    "mutation", ["unknown_schema", "unknown_reason", "extra_field", "bad_time"]
)
def test_unreferenced_malformed_matching_approval_is_a_blocker(mutation: str) -> None:
    raw, rows, grant, _ = fixture()
    if mutation == "unknown_schema":
        grant["schema_version"] = "approval/v99"
    elif mutation == "unknown_reason":
        grant["reason"] = "old"
    elif mutation == "extra_field":
        grant["unknown"] = True
    else:
        grant["valid_until"] = "not a date"
    rows.append(
        record(
            "refusal-disposition-approval",
            grant,
            "2026-11-04T10:30:00Z",
            "OPERATOR-CONSENT",
        )
    )
    assert evaluate(raw, rows).problems


@pytest.mark.parametrize(
    "row_type", ["CLAIM", "TERMINAL", "DONE", "CLOSEOUT", "UNKNOWN"]
)
def test_state_dependent_and_legacy_rows_block_before_grammar_reads(
    row_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.handlers import ledger_row_grammar

    real = ledger_row_grammar.row_refusals

    def guarded(line: str) -> list[str]:
        row = ledger_row_grammar.parse_row(line)
        assert row is not None
        assert row.row_type not in {"CLAIM", "TERMINAL", "DONE", "CLOSEOUT", "UNKNOWN"}
        return real(line)

    monkeypatch.setattr(ledger_row_grammar, "row_refusals", guarded)
    raw, rows, _, data = fixture()
    original = json.loads(raw)
    original["payload"] = PAYLOAD.replace("| STATUS |", f"| {row_type} |")
    assert evaluate(json.dumps(original).encode(), rows).problems
    selected_evidence = rows[0].replace("| STATUS |", f"| {row_type} |")
    data["evidence_sha256"] = [digest(selected_evidence)]
    selected_disposition = record("refusal-disposition", data, "2026-11-04T11:00:00Z")
    assert evaluate(raw, [selected_evidence, rows[1], selected_disposition]).problems
    # Unrelated full-history rows do not require validating local receipt state.
    unrelated = selected_evidence.replace(RID, "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")
    assert not evaluate(raw, [*rows, unrelated]).problems


@pytest.mark.parametrize(
    "reason", ["duplicate_delivery", "superseded", "operator_abandoned"]
)
def test_handle_performs_no_environment_or_filesystem_reads(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, rows, _, _ = fixture(reason)

    def no_io(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("pure validator attempted external state access")

    with monkeypatch.context() as guard:
        guard.setattr(os.environ, "get", no_io)
        guard.setattr(os, "getenv", no_io)
        guard.setattr(Path, "read_text", no_io)
        guard.setattr(Path, "read_bytes", no_io)
        guard.setattr(Path, "exists", no_io)
        guard.setattr(Path, "open", no_io)
        guard.setattr("builtins.open", no_io)
        result = evaluate(raw, rows)
    assert not result.problems
    assert result.settled == (NAME,)
