# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which tickets a bounded run examines, and whether an examined ticket is Done (OMN-20675).

Ported from the live closer (the dod-closeout-sweep workflow), rule for rule. A ticket is Done
only when every criterion it carries has a check that the script did not refuse, that a lane
other than its author accepted, and that dod_verify verified, and the dod_verify run as a whole
verified with no error code. Anything short leaves the ticket open and names every short
criterion. No read, no clock, no write.
"""

from __future__ import annotations

import json
import re
from typing import Any

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_binding_rules import (
    refusal_reason,
    same_actor,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumTicketDecision,
    ModelAcceptanceState,
    ModelRotationRecord,
    ModelUnmetCriterion,
)

COMMENT_MARKER = "dod-closeout-sweep"
# The actor-line guard refuses a Linear comment that does not open with this shape.
ACTOR_LINE = f"actor: {COMMENT_MARKER} (sonnet)"
STATE_MODEL = (
    "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state.ModelDodVerifyState"
)
RUNTIME_MODEL = (
    "omnibase_infra.cli.model_receipt_runtime_summary.ModelReceiptRuntimeSummary"
)
_TEXT_SHA = re.compile(r"[0-9a-f]{16}")
_BINDING_LABEL = re.compile(r"(AC|DOD)[-_ .]?(\d+)([a-zA-Z]?)", re.IGNORECASE)
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]+")
_NOT_VERIFIED = "no verdict for this criterion"


def _js_text(value: Any) -> str:
    """A scalar the way a JavaScript template literal prints it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _held_for_amendment(text_sha: str, held_text_sha: str) -> bool:
    """A ticket whose description has not changed since the closer recorded a rejection only
    a text amendment can clear is not re-adjudicated. An empty hash never holds."""
    return bool(_TEXT_SHA.fullmatch(text_sha)) and text_sha == held_text_sha


def parse_text_state(raw: str) -> list[dict[str, Any]]:
    """The text-state command's rows; garbled or missing output yields none, so nothing is held."""
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [
        r
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]
    ]


def select_candidates(
    *, records: list[ModelRotationRecord], text_state: str, max_candidates: int
) -> tuple[list[str], list[str], list[str]]:
    """Least recently examined first, never examined before everything, then by id, so a
    bounded run walks the whole sprint over successive runs. A ticket held for a text
    amendment is not selected at all and is reported as held. Returns selected, deferred, held."""
    by_id = {row["id"]: row for row in parse_text_state(text_state)}
    seen: set[str] = set()
    rows: list[tuple[str, str, str, str]] = []
    for record in records:
        if not record.id or record.id in seen:
            continue
        seen.add(record.id)
        row = by_id.get(record.id) or {}
        read = row.get("last_closeout_comment_at")
        at = (
            read if isinstance(read, str) and read else ""
        ) or record.last_closeout_comment_at
        rows.append(
            (
                record.id,
                at,
                str(row.get("text_sha") or ""),
                str(row.get("held_text_sha") or ""),
            )
        )
    held = sorted(i for i, _, now, kept in rows if _held_for_amendment(now, kept))
    live = [r for r in rows if not _held_for_amendment(r[2], r[3])]
    live.sort(key=lambda r: (r[1], r[0]))
    ids = [r[0] for r in live]
    return ids[:max_candidates], ids[max_candidates:], held


def chunk_list(ids: list[str], size: int) -> list[list[str]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def binding_label(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    match = _BINDING_LABEL.fullmatch(raw.strip())
    return (
        f"{match.group(1).upper()}{int(match.group(2))}{match.group(3)}"
        if match
        else ""
    )


def read_dod_verify(receipt_json: str) -> dict[str, Any]:
    """The verifier's declared result arm, never a model-authored verdict map.

    An unknown arm or missing checks holds the ticket.
    """
    refused: dict[str, Any] = {
        "verify_status": "not_run",
        "error_code": "VERIFY_RECEIPT_UNREADABLE",
        "checks": [],
    }
    try:
        receipt = json.loads(receipt_json)
    except ValueError:
        return refused
    if not isinstance(receipt, dict):
        return refused
    result = receipt.get("result")
    verdict: Any
    if receipt.get("result_model") == STATE_MODEL:
        verdict = result
    elif receipt.get("result_model") == RUNTIME_MODEL:
        verdict = result.get("terminal_payload") if isinstance(result, dict) else None
    else:
        return refused
    if not isinstance(verdict, dict):
        return refused
    total = verdict.get("total_checks")
    if isinstance(total, float) and total.is_integer():
        total = int(total)
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 1
        or not isinstance(verdict.get("checks"), list)
    ):
        return refused
    raw_message = verdict.get("error_message")
    message = raw_message.strip() if isinstance(raw_message, str) else ""
    code = ""
    if message:
        found = _ERROR_CODE.match(message)
        code = found.group(0) if found else "VERIFY_ERROR"
    return {
        "verify_status": verdict.get("status"),
        "error_code": code,
        "checks": verdict["checks"],
    }


def ac_verdicts(dod_verify: dict[str, Any]) -> dict[str, Any]:
    """Per criterion label: 'verified' when one accepted check verified it, else the status."""
    verdicts: dict[str, Any] = {}
    checks = dod_verify.get("checks")
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        evidence = check.get("evidence_id")
        binds = check.get("binds_ac")
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or not isinstance(binds, list)
        ):
            continue
        # A malformed draft list cannot be interpreted as independent acceptance.
        if "draft_binds_ac" in check and not isinstance(check["draft_binds_ac"], list):
            continue
        drafts = {binding_label(x) for x in check.get("draft_binds_ac") or []}
        for raw in binds:
            label = binding_label(raw)
            if not label or label in drafts:
                continue
            # One verified, accepted check can discharge a criterion; no other status is proof.
            if check.get("status") == "verified":
                verdicts[label] = "verified"
            elif verdicts.get(label) != "verified":
                verdicts[label] = check.get("status") or "unbound"
    return verdicts


def _amendment(text: str) -> str:
    return text.strip()


def _unmet(label: str, reason: str, amendment: str = "") -> ModelUnmetCriterion:
    return ModelUnmetCriterion(
        label=label, reason=reason, amendment=_amendment(amendment)
    )


def decide_ticket(
    *,
    acs: list[ModelAcceptanceState],
    dod_verify: dict[str, Any],
    criteria_note: str,
    criteria_amendment: str,
) -> tuple[EnumTicketDecision, list[ModelUnmetCriterion]]:
    unmet: list[ModelUnmetCriterion] = []
    verdicts = ac_verdicts(dod_verify)
    if not acs:
        unmet.append(
            _unmet(
                "criteria",
                criteria_note
                or "no acceptance criterion could be read from the ticket, so nothing can be bound",
                criteria_amendment,
            )
        )
    for ac in acs:
        label = ac.label or "?"
        if not ac.proposed:
            reason = ac.reason or "the binder found no honest check"
            unmet.append(_unmet(label, f"no check authored: {reason}", ac.amendment))
        elif ac.refused:
            unmet.append(_unmet(label, refusal_reason(ac.refused)))
        elif not ac.accepted:
            reason = ac.reason or "no reason given"
            unmet.append(
                _unmet(
                    label, f"binding rejected by the acceptor: {reason}", ac.amendment
                )
            )
        elif same_actor(ac.accepted_by, ac.proposed_by):
            unmet.append(
                _unmet(
                    label, "binding was not accepted by a lane other than its author"
                )
            )
        else:
            verdict = verdicts.get(binding_label(label))
            if verdict != "verified":
                shown = _js_text(verdict) if verdict else _NOT_VERIFIED
                unmet.append(
                    _unmet(
                        label, f"dod_verify did not verify the bound check ({shown})"
                    )
                )
    status = dod_verify.get("verify_status")
    error = dod_verify.get("error_code") or ""
    if acs and (status != "verified" or error):
        shown_status = _js_text(status) if status else "not run"
        suffix = f" error={error}" if error else ""
        unmet.append(_unmet("dod_verify", f"verify_status={shown_status}{suffix}"))
    return (EnumTicketDecision.OPEN if unmet else EnumTicketDecision.DONE), unmet


def _unmet_signature(unmet: list[ModelUnmetCriterion]) -> str:
    return ",".join(
        sorted(f"{u.label}:{re.split(r'[:(]', u.reason)[0].strip()}" for u in unmet)
    )


def comment_signature(unmet: list[ModelUnmetCriterion], text_sha: str) -> str:
    """The dedupe key of an open comment. One that records an amendment hold also carries the
    text hash, so the first comment that records the hold is posted even when an older comment
    carried the same gaps without one."""
    held = any(u.amendment for u in unmet) and bool(_TEXT_SHA.fullmatch(text_sha))
    return f"{_unmet_signature(unmet)}{f' text={text_sha}' if held else ''}"


def open_comment(
    ticket_id: str, run_key: str, unmet: list[ModelUnmetCriterion], text_sha: str
) -> str:
    """The comment a ticket left open receives. It states facts and names the criteria; it asks
    nobody anything. A criterion that needs a text amendment is named with the hash of the
    description it was made against, which holds the ticket out of later runs."""
    amendments = [u for u in unmet if u.amendment]
    sha = text_sha if _TEXT_SHA.fullmatch(text_sha) else ""
    lines = [
        ACTOR_LINE,
        f"{COMMENT_MARKER} run {run_key}: {ticket_id} stays open. These acceptance criteria are not proven by a check another lane accepted and dod_verify verified:",
        *(f"- {u.label}: {u.reason}" for u in unmet),
    ]
    if amendments:
        lines.append(
            "Amendment needed. These criteria cannot be proven as written, so no run can close the ticket until its text changes:"
        )
        lines.extend(f"- {u.label}: {u.amendment}" for u in amendments)
        lines.append(
            "The closer skips this ticket until its description changes, then examines it again."
            if sha
            else "The closer re-examines this ticket on a later run."
        )
        if sha:
            lines.append(f"amendment-text-sha={sha}")
    else:
        lines.append(
            "The closer re-examines this ticket on a later run. It moves to Done when every criterion has an accepted, verified check."
        )
    lines.append(f"signature={comment_signature(unmet, sha)}")
    return "\n".join(lines).replace("?", "")
