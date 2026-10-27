# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure normalization and replace-by-key join of already-read receipts."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from typing import Any

from ..models.model_source_document import ModelReceiptDocument
from ..models.model_work_attempt_receipt import (
    ModelAttemptManifestRow,
    ModelJoinSummary,
    ModelLandingCrossCheck,
    ModelReceiptConflict,
    ModelReceiptJoinRequest,
    ModelReceiptJoinResult,
    ModelSourceReceipt,
    ModelWorkAttemptReceipt,
    ModelWorkUnjoinedReceipt,
)


def _source_key(source: ModelSourceReceipt) -> str:
    return json.dumps(
        source.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _source_id(source: ModelSourceReceipt) -> str:
    return hashlib.sha256(_source_key(source).encode()).hexdigest()


def _line(value: str | None, label: str) -> str | None:
    prefix = f"{label}: "
    return value[len(prefix) :].strip() if value and value.startswith(prefix) else None


def _known(value: Any) -> bool:
    return value is not None and value != "UNKNOWN"


def _normalise(family: str, doc: ModelReceiptDocument) -> dict[str, Any]:
    fields = (
        "engine",
        "model",
        "effort",
        "tokens_in",
        "tokens_out",
        "cache_read",
        "cache_write",
        "wall_s",
        "turns",
        "tool_calls",
        "retries",
    )
    result = {key: getattr(doc, key) for key in fields if _known(getattr(doc, key))}
    if "wall_s" not in result and _known(doc.duration_s):
        result["wall_s"] = doc.duration_s
    usage = doc.usage if family in {"harness_delegate", "remote_lane"} else doc.metrics
    if not usage and family == "delegate" and doc.receipt and doc.receipt.result:
        usage = doc.receipt.result.metrics
    if not usage:
        usage = doc
    if usage:
        for target, origin in (
            ("tokens_in", "input_tokens"),
            ("tokens_out", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("cache_write", "cache_creation_input_tokens"),
        ):
            if target not in result and _known(getattr(usage, origin)):
                result[target] = getattr(usage, origin)
        if (
            family == "harness_delegate"
            and "turns" not in result
            and _known(usage.num_turns)
        ):
            result["turns"] = usage.num_turns
    if (
        family == "remote_lane"
        and "turns" not in result
        and not any(
            "codex" in name.casefold() for name in (doc.engine or "", doc.model or "")
        )
        and _known(doc.num_turns)
    ):
        result["turns"] = doc.num_turns
    if doc.config and not result.get("effort") and doc.config.effort:
        result["effort"] = doc.config.effort
    telemetry = doc.measured or (
        doc.rubric_verdict.measured if doc.rubric_verdict else None
    )
    if telemetry and "tool_calls" not in result and _known(telemetry.tool_calls):
        result["tool_calls"] = telemetry.tool_calls
    rejected = tuple(
        sorted(
            {
                a.acceptance_reason
                for a in doc.attempts
                if a.acceptance_reason
                and a.acceptance_decision in {"reject", "rejected", "fail"}
            }
        )
    )
    if rejected:
        result["rejection_reasons"] = rejected
    if family == "delegate" and len(doc.attempts) == 1:
        attempt = doc.attempts[0]
        if _known(attempt.input_tokens_measured):
            result["tokens_in"] = attempt.input_tokens_measured
    # An aggregate delegate run cannot be allocated across internal attempts.
    # Its manifest correlation identifies this single Phase 1 model dispatch.
    cost = doc.total_cost_usd if family == "remote_lane" else doc.cost_usd
    measured = doc.usage_source == "MEASURED"
    if family == "delegate" and usage and _known(usage.cost_usd):
        cost, measured = usage.cost_usd, usage.cost_measured is True
    if (
        family == "delegate"
        and len(doc.attempts) == 1
        and _known(doc.attempts[0].cost_usd)
    ):
        cost = doc.attempts[0].cost_usd
        measured = doc.attempts[0].usage_source == "MEASURED"
    if family == "remote_lane":
        measured = (
            _known(cost) and Decimal(str(cost)) > 0 and doc.usage_source != "UNKNOWN"
        )
    if _known(cost) and measured:
        result["cost_usd"] = cost
    return result


class HandlerModelWorkReceiptJoin:
    def handle(self, request: ModelReceiptJoinRequest) -> ModelReceiptJoinResult:
        by_attempt = {row.attempt_id: row for row in request.manifest}
        by_correlation = {
            key: row for row in request.manifest for key in row.correlation_ids
        }
        by_lease = {
            key: row for row in request.manifest for key in row.landing_lease_ids
        }
        groups: dict[
            tuple[str, str], list[tuple[ModelSourceReceipt, ModelReceiptDocument]]
        ] = defaultdict(list)
        unjoined = {row.record_id: row for row in request.existing_unjoined}
        rows = {
            (row.contract_id, row.attempt_id): row for row in request.existing_receipts
        }
        conflicts: list[ModelReceiptConflict] = []
        landing: list[ModelLandingCrossCheck] = []

        def reject(
            source: ModelSourceReceipt,
            reason: str,
            identity: ModelAttemptManifestRow | None = None,
        ) -> None:
            record_id = _source_id(source)
            unjoined[record_id] = ModelWorkUnjoinedReceipt(
                record_id=record_id,
                source_refs=(source.source_ref,),
                reason=reason,
                contract_id=identity.contract_id if identity else "UNKNOWN",
                attempt_id=identity.attempt_id if identity else "UNKNOWN",
            )

        unique = {_source_key(source): source for source in request.sources}
        order = {"remote_lane": 0, "delegate": 1, "harness_delegate": 2, "landing": 3}
        for source in sorted(
            unique.values(),
            key=lambda s: (order[s.family], s.source_ref, _source_key(s)),
        ):
            # Replace the prior disposition of exactly this source on a rejoin.
            unjoined.pop(_source_id(source), None)
            doc = ModelReceiptDocument.model_validate(source.document)
            if source.family == "landing":
                identity = by_lease.get(doc.lease_id or "")
                if not identity:
                    reject(source, "unmapped_landing_lease")
                    continue
                wall = doc.duration_s
                if not _known(wall) and _known(doc.duration_ms):
                    wall = Decimal(str(doc.duration_ms)) / 1000
                landing.append(
                    ModelLandingCrossCheck(
                        contract_id=identity.contract_id,
                        attempt_id=identity.attempt_id,
                        source_ref=source.source_ref,
                        wall_s=wall if _known(wall) else "UNKNOWN",
                    )
                )
                continue
            if source.family == "remote_lane":
                attempt = _line(doc.attempt_line, "Attempt")
                contract = _line(doc.contract_line, "Contract") or doc.contract_id
                identity = by_attempt.get(attempt or "")
                if identity and (
                    contract != identity.contract_id
                    or (doc.contract_id and doc.contract_id != identity.contract_id)
                    or (doc.attempt_id and doc.attempt_id != identity.attempt_id)
                ):
                    reject(source, "identity_conflict")
                    continue
            else:
                identity = (
                    by_correlation.get(doc.correlation_id)
                    if doc.correlation_id
                    else None
                )
                if identity and (
                    (doc.contract_id and doc.contract_id != identity.contract_id)
                    or (doc.attempt_id and doc.attempt_id != identity.attempt_id)
                ):
                    reject(source, "identity_conflict")
                    continue
            if source.family == "delegate" and len(doc.attempts) > 1:
                reject(source, "ambiguous_delegate_attempts", identity)
                continue
            if not identity:
                reject(source, "not_in_manifest")
                continue
            groups[(identity.contract_id, identity.attempt_id)].append((source, doc))

        expectations = {c.contract_id: c for c in request.contracts}
        executions = {(c.contract_id, c.attempt_id): c for c in request.checks}
        for key, sources in sorted(groups.items()):
            identity = by_attempt[key[1]]
            expectation = expectations.get(key[0])
            execution = executions.get(key)
            output = request.outputs.get(key[1])
            reason = ""
            if expectation is None:
                reason = "missing_contract_check"
            elif execution is None:
                reason = "missing_check"
            elif execution.check_sha256 != expectation.check_sha256:
                reason = "check_spec_mismatch"
            elif (
                output is None
                or hashlib.sha256(output.encode()).hexdigest()
                != execution.output_sha256
            ):
                reason = "check_output_mismatch"
            if reason:
                rows.pop(key, None)
                for source, _ in sources:
                    reject(source, reason, identity)
                continue
            assert expectation is not None
            assert execution is not None
            candidates: dict[str, list[tuple[Any, str]]] = defaultdict(list)
            for source, doc in sources:
                for field, value in _normalise(source.family, doc).items():
                    candidates[field].append((value, source.source_ref))
            values: dict[str, Any] = {}
            for field, entries in sorted(candidates.items()):
                if any(value != entries[0][0] for value, _ in entries[1:]):
                    conflicts.append(
                        ModelReceiptConflict(
                            contract_id=key[0],
                            attempt_id=key[1],
                            field=field,
                            values=tuple(str(value) for value, _ in entries),
                            source_refs=tuple(ref for _, ref in entries),
                        )
                    )
                    values[field] = "UNKNOWN"
                else:
                    values[field] = entries[0][0]
            values["cost_basis"] = (
                "MEASURED" if _known(values.get("cost_usd")) else "UNKNOWN"
            )
            rows[key] = ModelWorkAttemptReceipt(
                contract_id=key[0],
                attempt_id=key[1],
                arm=identity.arm,
                draw=identity.draw,
                verdict="pass"
                if execution.exit_code == expectation.expected_exit
                else "fail",
                source_refs=tuple(
                    dict.fromkeys(source.source_ref for source, _ in sources)
                ),
                **values,
            )
        receipts = tuple(row for _, row in sorted(rows.items()))
        orphans = tuple(row for _, row in sorted(unjoined.items()))
        return ModelReceiptJoinResult(
            receipts=receipts,
            unjoined=orphans,
            conflicts=tuple(conflicts),
            landing_cross_checks=tuple(landing),
            summary=ModelJoinSummary(
                joined=len(receipts),
                unjoined=len(orphans),
                passed=sum(r.verdict == "pass" for r in receipts),
                failed=sum(r.verdict == "fail" for r in receipts),
                per_pass_eligible=sum(
                    r.verdict == "pass"
                    and all(
                        _known(getattr(r, f))
                        for f in ("tokens_in", "tokens_out", "wall_s")
                    )
                    for r in receipts
                ),
                conflicts=len(conflicts),
            ),
        )
