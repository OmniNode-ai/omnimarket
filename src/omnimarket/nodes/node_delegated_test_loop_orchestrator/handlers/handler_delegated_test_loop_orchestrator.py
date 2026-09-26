# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegatedTestLoopOrchestrator — the delegated test write-run-fix loop
(OMN-19362, task T6 of the first slice).

The sequence is the one the TLA+ model of plan task T1 checked (its verdict
file lists the consequences this code keeps):

    WRITE -> RUN(fixed) -> [DIGEST -> REPAIR -> RUN(fixed)]*  -> CONTROL(prefix)
          -> [CONTROL(mutation)] -> RESULT

* At most ``max_delegate_calls`` (3) WRITE/REPAIR calls. The bound is enforced
  on the failure branch, after the run: the Nth failed run is terminal. (The
  model showed a guard on WRITE alone is redundant, not sufficient.)
* Two identical failure fingerprints in a row stop the loop with
  ``no_progress``, checked before the bound.
* An infrastructure fault at any run is ``infra_error``, never accepted. A run
  the host refuses as busy is retried a bounded number of times, then the
  loop ends ``host_busy``.
* The control runs the SAME test at the pre-fix ref. When that fails only at
  collection and the request carries a mutation, the mutation of the fixed
  commit decides the headline (operator ruling 2026-09-23T21:52:08Z (3)).
* Exactly one terminal per correlation: a loop receipt that already exists for
  the correlation id refuses the run before any child is called.
* OMN-19527: a fixed-ref run that carried the repository's lint and type gates
  is digested by the gate port. When the test PASSED but the gates refused it,
  and the call bound leaves room, exactly one repair call carries the gate
  digest verbatim. The repair is a polish: if the repaired test no longer
  passes, or the reply is unusable, the loop keeps the passing test it had.
  An infrastructure fault in a gate is never repaired and never clean.

Every child is reached through ``ProtocolDelegatedTestLoopPorts``; this module
imports no other node. The result carries no test source and no log text
beyond a capped digest; the loop receipt holds the rest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Literal

from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    HEADLINE_STATUSES,
    EnumLoopStatus,
    ModelDelegatedTestLoopRequest,
    ModelDelegatedTestLoopResult,
    ModelDelegateReply,
    ModelFinalDigest,
    ModelGateDigestSeam,
    ModelLoopControl,
    ModelRunDigest,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.protocols.protocol_delegated_test_loop_ports import (
    ModelRunReceipt,
    ProtocolDelegatedTestLoopPorts,
)

#: How many times a busy host is retried before the loop ends ``host_busy``.
MAX_HOST_BUSY_TRIES = 6

_STATUS_BY_GRADE = {
    "accepted_call": EnumLoopStatus.ACCEPTED_CALL,
    "accepted_mutation": EnumLoopStatus.ACCEPTED_MUTATION,
    "accepted_collection": EnumLoopStatus.ACCEPTED_COLLECTION,
    "control_did_not_fail": EnumLoopStatus.CONTROL_DID_NOT_FAIL,
    "infra_error": EnumLoopStatus.INFRA_ERROR,
}


def excerpt_lines(text: str, ranges: tuple[tuple[int, int], ...], path: str) -> str:
    """The requested 1-based inclusive line ranges, each headed by its location."""
    if not ranges:
        return text
    lines = text.splitlines()
    parts = []
    for start, end in ranges:
        lo, hi = max(start, 1), min(end, len(lines))
        if lo > hi:
            continue
        parts.append(f"# --- {path} lines {lo}-{hi} ---")
        parts.extend(lines[lo - 1 : hi])
    return "\n".join(parts) + "\n"


@dataclass
class _GateTally:
    """What the gates said about the test the loop kept (OMN-19527)."""

    clean: bool | None = None
    findings: int = 0
    repairs: int = 0


class _TerminalError(Exception):
    """Internal: carries a terminal status out of the sequence."""

    def __init__(
        self,
        status: EnumLoopStatus,
        detail: str = "",
        last: ModelRunDigest | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.last = last


class HandlerDelegatedTestLoopOrchestrator:
    """ORCHESTRATOR: sequences the loop's children through injected ports."""

    def __init__(self, ports: ProtocolDelegatedTestLoopPorts | None = None) -> None:
        # Optional so the handler constructs at boot from defaults alone; a run
        # without bound ports is refused rather than half-wired.
        self._bound = ports

    @property
    def _ports(self) -> ProtocolDelegatedTestLoopPorts:
        if self._bound is None:
            raise RuntimeError(
                "no loop ports are bound; `onex test-loop run` (or the "
                "in-process runner scripts/dtl/run_delegated_test_loop.py) "
                "binds the children"
            )
        return self._bound

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    async def handle(
        self, request: ModelDelegatedTestLoopRequest
    ) -> ModelDelegatedTestLoopResult:
        return await asyncio.to_thread(self.run, request)

    # -- the sequence ---------------------------------------------------------

    def run(
        self, request: ModelDelegatedTestLoopRequest
    ) -> ModelDelegatedTestLoopResult:
        started = time.monotonic()
        loop_run_id = request.correlation_id
        self._ports.claim_loop_receipt(loop_run_id)

        steps: list[dict[str, object]] = []
        replies: list[ModelDelegateReply] = []
        receipts: list[str] = []
        test_source = ""
        last: ModelRunDigest | None = None
        control: ModelLoopControl | None = None
        gates = _GateTally()
        status: EnumLoopStatus
        detail = ""

        try:
            target = excerpt_lines(
                self._ports.read_target(
                    request.repo, request.fixed_ref, request.target_path
                ),
                request.target_line_ranges,
                request.target_path,
            )
            test_source, last = self._write_until_pass(
                request, target, steps, replies, receipts, gates
            )
            control, status = self._control(request, test_source, steps, receipts)
        except _TerminalError as terminal:
            status, detail = terminal.status, terminal.detail
            last = terminal.last or last

        result = ModelDelegatedTestLoopResult(
            loop_run_id=loop_run_id,
            status=status,
            headline=status in HEADLINE_STATUSES,
            test_path=request.test_path,
            test_source_sha256=(
                hashlib.sha256(test_source.encode("utf-8")).hexdigest()
                if test_source
                else ""
            ),
            attempts=len(replies),
            control=control,
            delegate_run_ids=tuple(r.run_id for r in replies if r.run_id),
            run_receipt_ids=tuple(receipts),
            final_digest=(
                None
                if status in HEADLINE_STATUSES or last is None
                else ModelFinalDigest(
                    outcome=last.outcome,
                    exception_type=last.exception_type,
                    message=last.message[:300],
                    top_frame=last.top_frame,
                    fingerprint=last.fingerprint,
                )
            ),
            local_tokens_in=sum(r.tokens_in for r in replies),
            local_tokens_out=sum(r.tokens_out for r in replies),
            wall_ms=int((time.monotonic() - started) * 1000),
            detail=detail[:300],
            gate_clean=gates.clean,
            gate_findings=gates.findings,
            gate_repairs=gates.repairs,
        )
        self._ports.write_loop_receipt(
            loop_run_id,
            {
                "loop_run_id": loop_run_id,
                "correlation_id": request.correlation_id,
                "request": request.model_dump(mode="json"),
                "delegate_run_ids": list(result.delegate_run_ids),
                "run_receipt_ids": list(result.run_receipt_ids),
                "steps": steps,
                "test_source": test_source,
                "result": result.model_dump(mode="json"),
            },
        )
        return result

    def _write_until_pass(
        self,
        request: ModelDelegatedTestLoopRequest,
        target: str,
        steps: list[dict[str, object]],
        replies: list[ModelDelegateReply],
        receipts: list[str],
        gates: _GateTally,
    ) -> tuple[str, ModelRunDigest | None]:
        previous_test = ""
        last: ModelRunDigest | None = None
        last_fingerprint = ""
        for attempt in range(1, request.max_delegate_calls + 1):
            try:
                prompt = self._ports.build_prompt(request, target, previous_test, last)
            except ValueError as exc:
                raise _TerminalError(
                    EnumLoopStatus.INFRA_ERROR, f"prompt refused: {exc}"
                ) from exc
            reply = self._ports.delegate(prompt, attempt)
            replies.append(reply)
            steps.append(
                {
                    "kind": "delegate",
                    "attempt": attempt,
                    **reply.model_dump(mode="json"),
                }
            )

            if reply.ok and reply.test_source.strip():
                previous_test = reply.test_source
                digest, receipt = self._run_digested(
                    request,
                    request.fixed_ref,
                    "fixed",
                    attempt,
                    reply.test_source,
                    steps,
                    receipts,
                )
                if digest.outcome == "passed":
                    source = reply.test_source
                    gate = self._gate(request, receipt, source, attempt, steps, gates)
                    if (
                        gate is not None
                        and not gate.clean
                        and not gate.infra_error
                        and attempt < request.max_delegate_calls
                    ):
                        source = self._gate_repair(
                            request,
                            target,
                            source,
                            gate,
                            attempt + 1,
                            steps,
                            replies,
                            receipts,
                            gates,
                        )
                    return source, digest
            else:
                # An unusable reply is a failed attempt with its own fingerprint,
                # fed back to the next call like any other failure.
                reason = (
                    reply.invalid_reason or "the delegate call returned no usable test"
                )
                digest = ModelRunDigest(
                    receipt_id="",
                    receipt_status="completed",
                    outcome="no_tests",
                    exception_type="InvalidDelegateReply",
                    message=reason[:500],
                    fingerprint=hashlib.sha256(
                        f"invalid_reply\0{reason[:80]}".encode()
                    ).hexdigest(),
                )
                previous_test = (
                    previous_test or reply.test_source or "# (no module returned)\n"
                )
                steps.append(
                    {
                        "kind": "invalid_reply",
                        "attempt": attempt,
                        "reason": reason[:500],
                    }
                )

            if digest.fingerprint and digest.fingerprint == last_fingerprint:
                last = digest
                raise _TerminalError(
                    EnumLoopStatus.NO_PROGRESS,
                    "the same failure twice in a row",
                    digest,
                )
            last, last_fingerprint = digest, digest.fingerprint
            if attempt >= request.max_delegate_calls:
                break
        raise _TerminalError(
            EnumLoopStatus.FAILED, "the attempt bound was reached without a pass", last
        )

    def _control(
        self,
        request: ModelDelegatedTestLoopRequest,
        test_source: str,
        steps: list[dict[str, object]],
        receipts: list[str],
    ) -> tuple[ModelLoopControl, EnumLoopStatus]:
        attempt = sum(1 for s in steps if s.get("kind") == "delegate")
        prefix, _ = self._run_digested(
            request, request.prefix_ref, "prefix", attempt, test_source, steps, receipts
        )
        verdict = self._ports.grade(
            prefix.outcome, None, bool(request.mutations), request.is_negative_control
        )
        steps.append({"kind": "grade", **verdict.model_dump(mode="json")})
        ref, role, outcome = request.prefix_ref, "prefix", prefix.outcome
        if verdict.status == "needs_mutation_control":
            mutation, _ = self._run_digested(
                request,
                request.fixed_ref,
                "mutation",
                attempt,
                test_source,
                steps,
                receipts,
            )
            verdict = self._ports.grade(prefix.outcome, mutation.outcome, True, False)
            steps.append({"kind": "grade", **verdict.model_dump(mode="json")})
            ref, role, outcome = request.fixed_ref, "mutation", mutation.outcome
        status = _STATUS_BY_GRADE.get(verdict.status)
        if status is None:
            raise _TerminalError(
                EnumLoopStatus.INFRA_ERROR, f"unknown grade {verdict.status!r}"
            )
        control = ModelLoopControl(
            ref=ref,
            ref_role="mutation" if role == "mutation" else "prefix",
            outcome=outcome,
            prefix_outcome=prefix.outcome,
        )
        return control, status

    def _gate(
        self,
        request: ModelDelegatedTestLoopRequest,
        receipt: ModelRunReceipt,
        source: str,
        attempt: int,
        steps: list[dict[str, object]],
        gates: _GateTally,
    ) -> ModelGateDigestSeam | None:
        """Digest the gates a passing fixed run carried; None when none ran."""
        if not request.run_code_gates or not receipt.gate_outputs:
            return None
        gate = self._ports.digest_gates(receipt, source)
        steps.append(
            {
                "kind": "gate",
                "attempt": attempt,
                "receipt_id": receipt.receipt_id,
                **gate.model_dump(mode="json"),
            }
        )
        gates.clean = gate.clean
        gates.findings = gate.finding_count
        return gate

    def _gate_repair(
        self,
        request: ModelDelegatedTestLoopRequest,
        target: str,
        passing_source: str,
        gate: ModelGateDigestSeam,
        attempt: int,
        steps: list[dict[str, object]],
        replies: list[ModelDelegateReply],
        receipts: list[str],
        gates: _GateTally,
    ) -> str:
        """The one repair call a gate refusal buys; returns the test to keep."""
        gates.repairs = 1
        steps.append({"kind": "gate_repair", "attempt": attempt})
        try:
            prompt = self._ports.build_prompt(
                request, target, passing_source, None, gate=gate
            )
        except ValueError as exc:
            steps.append(
                {"kind": "gate_repair_kept", "attempt": attempt, "reason": str(exc)}
            )
            return passing_source
        reply = self._ports.delegate(prompt, attempt)
        replies.append(reply)
        steps.append(
            {"kind": "delegate", "attempt": attempt, **reply.model_dump(mode="json")}
        )
        if not (reply.ok and reply.test_source.strip()):
            steps.append(
                {
                    "kind": "gate_repair_kept",
                    "attempt": attempt,
                    "reason": (reply.invalid_reason or "no usable test")[:500],
                }
            )
            return passing_source
        digest, receipt = self._run_digested(
            request,
            request.fixed_ref,
            "fixed",
            attempt,
            reply.test_source,
            steps,
            receipts,
        )
        if digest.outcome != "passed":
            steps.append(
                {
                    "kind": "gate_repair_kept",
                    "attempt": attempt,
                    "reason": f"the repaired test did not pass: {digest.outcome}",
                }
            )
            return passing_source
        self._gate(request, receipt, reply.test_source, attempt, steps, gates)
        return reply.test_source

    def _run_digested(
        self,
        request: ModelDelegatedTestLoopRequest,
        ref: str,
        role: Literal["fixed", "prefix", "mutation"],
        attempt: int,
        test_source: str,
        steps: list[dict[str, object]],
        receipts: list[str],
    ) -> tuple[ModelRunDigest, ModelRunReceipt]:
        receipt: ModelRunReceipt | None = None
        for tries in range(1, MAX_HOST_BUSY_TRIES + 1):
            receipt = self._ports.run(request, ref, role, attempt, test_source)
            if receipt.status != "host_busy":
                break
            steps.append(
                {"kind": "host_busy", "role": role, "attempt": attempt, "tries": tries}
            )
            if tries < MAX_HOST_BUSY_TRIES:
                self._ports.wait_for_host(tries)
        if receipt is None or receipt.status == "host_busy":
            raise _TerminalError(EnumLoopStatus.HOST_BUSY, "the lab host stayed busy")
        receipts.append(receipt.receipt_id)
        if receipt.status == "infra_error":
            steps.append(
                {
                    "kind": "run",
                    "role": role,
                    "attempt": attempt,
                    "receipt_id": receipt.receipt_id,
                    "status": receipt.status,
                    "detail": receipt.detail[:500],
                }
            )
            raise _TerminalError(
                EnumLoopStatus.INFRA_ERROR, f"{role} run: {receipt.detail[:200]}"
            )
        digest = self._ports.digest(receipt)
        steps.append(
            {
                "kind": "run",
                "role": role,
                "attempt": attempt,
                "receipt_id": receipt.receipt_id,
                "status": receipt.status,
                "exit_code": receipt.exit_code,
                "digest": digest.model_dump(mode="json"),
                "gated": bool(receipt.gate_outputs),
            }
        )
        if digest.outcome == "infra_error":
            raise _TerminalError(
                EnumLoopStatus.INFRA_ERROR, f"{role} run digest: infra_error", digest
            )
        return digest, receipt


def result_json_bytes(result: ModelDelegatedTestLoopResult) -> int:
    return len(
        json.dumps(result.model_dump(mode="json"), separators=(",", ":")).encode()
    )


__all__ = [
    "MAX_HOST_BUSY_TRIES",
    "HandlerDelegatedTestLoopOrchestrator",
    "excerpt_lines",
    "result_json_bytes",
]
