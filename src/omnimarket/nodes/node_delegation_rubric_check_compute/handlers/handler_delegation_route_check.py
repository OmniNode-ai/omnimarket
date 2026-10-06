# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure receipt route oracle. Projection and answer checks remain caller evidence."""

import re

from pydantic import JsonValue

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_route_check_outcome import (
    EnumRouteCheckOutcome,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_route_check_request import (
    ModelRouteCheckRequest,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_route_check_verdict import (
    ModelRouteCheckVerdict,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_route_observation import (
    ModelRouteObservation,
)


def _terminal(doc: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    envelope = doc.get("receipt")
    if not isinstance(envelope, dict):
        return None
    result = envelope.get("result")
    if not isinstance(result, dict):
        return None
    if "ModelDelegateSkill" in str(envelope.get("result_model") or ""):
        return result
    for field in ("terminal_payload", "handler_result"):
        carrier = result.get(field)
        if not isinstance(carrier, dict):
            continue
        payload = carrier.get("payload")
        if (
            "envelope_id" in carrier
            and isinstance(payload, dict)
            and "attempts" in payload
        ):
            return payload
        if "attempts" in carrier:
            return carrier
    return None


class HandlerDelegationRouteCheck:
    """Inspect supplied JSON without executing a delegation or touching its files."""

    def handle(self, request: ModelRouteCheckRequest) -> ModelRouteCheckVerdict:
        doc = request.receipt
        terminal = _terminal(doc)
        if terminal is None:
            envelope = doc.get("receipt")
            refused = (
                isinstance(envelope, dict)
                and "result" in envelope
                and envelope["result"] is None
            )
            reason = doc.get("failure_reason")
            failure_reason = reason if isinstance(reason, str) else None
            detail = (
                f"CLI refused before a delegation terminal: {failure_reason or 'failure_reason not recorded'}"
                if refused
                else "no resolvable delegation terminal (a REFUSED or dropped-reply run)"
            )
            return ModelRouteCheckVerdict(
                outcome=EnumRouteCheckOutcome.REFUSED
                if refused
                else EnumRouteCheckOutcome.NO_TERMINAL,
                run_directory=request.run_directory,
                route=request.route,
                pin=request.pin,
                draw_class="REFUSED",
                failure_reason=failure_reason,
                failures=(detail,),
            )

        failures: list[str] = []
        observations: list[ModelRouteObservation] = []
        notes: list[str] = []

        def observe(name: str, value: JsonValue) -> None:
            observations.append(ModelRouteObservation(field=name, value=value))

        def expect(name: str, got: JsonValue, want: JsonValue) -> None:
            observe(name, got)
            if got != want:
                failures.append(f"{name}: read {got!r}, expected {want!r}")

        attempts = terminal.get("attempts")
        accepted = (
            [
                attempt
                for attempt in attempts
                if isinstance(attempt, dict)
                and attempt.get("acceptance_decision") == "accept"
            ]
            if isinstance(attempts, list)
            else []
        )
        acc = accepted[0] if len(accepted) == 1 else {}
        if len(accepted) != 1:
            failures.append(
                f"expected exactly one accepted attempt, found {len(accepted)}"
            )
        raw_metrics = terminal.get("metrics")
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}

        expect("requested_backend_id", doc.get("requested_backend_id"), request.pin)
        expect("backend_pin_honoured", doc.get("backend_pin_honoured"), True)
        if request.route == "L":
            expect("bus", doc.get("bus"), "kafka")
            expect("locus", doc.get("locus"), "deployed-lane")
            if request.lane:
                expect("lane", doc.get("lane"), request.lane)
            expect("backend_id", doc.get("backend_id"), request.pin)
            expect("model", terminal.get("model_name"), request.model or "Qwen3.8-27B")
            expect("routing_tier", doc.get("routing_tier"), "local")
            expect("accepted attempt backend_id", acc.get("backend_id"), request.pin)
            expect("secret_source", terminal.get("secret_source"), None)
            expect("secret_ref", terminal.get("secret_ref"), None)
            expect("metrics.cost_usd (calculated)", metrics.get("cost_usd"), 0.0)
        else:
            expect("bus", doc.get("bus"), "inmemory")
            expect("locus", doc.get("locus"), "in-process")
            expect("lane", doc.get("lane"), None)
            expect("backend_id", doc.get("backend_id"), "byok-gemini")
            expect(
                "model",
                terminal.get("model_name"),
                request.model or "gemini-2.5-flash-lite",
            )
            expect("routing_tier", doc.get("routing_tier"), "cheap_cloud")
            expect(
                "accepted attempt substituted_from_backend_id",
                acc.get("substituted_from_backend_id"),
                request.pin,
            )
            expect("secret_source", terminal.get("secret_source"), "store")
            ref = terminal.get("secret_ref")
            observe("secret_ref", ref)
            if not request.secret_ref:
                failures.append(
                    "secret_ref is required for route C: the reference printed at registration"
                )
            elif ref != request.secret_ref:
                failures.append(
                    f"secret_ref: read {ref!r}, the registered reference is {request.secret_ref!r}"
                )
            if isinstance(ref, str) and ref.startswith("llm."):
                failures.append(
                    "secret_ref is a house reference (llm.*): a house credential answered customer work"
                )
            if not isinstance(ref, str) or not re.fullmatch(
                r"cred_localinstall_gemini_[0-9a-f]{32}", ref
            ):
                failures.append(
                    "secret_ref is not shaped cred_localinstall_gemini_<32 hex>"
                )

        tenant = terminal.get("tenant_id")
        observe("tenant_id", tenant)
        if request.route == "C":
            if not tenant or tenant == "omninode":
                failures.append(
                    f"tenant_id: read {tenant!r}, expected the walker's own tenant, not the house default"
                )
            if request.tenant and tenant != request.tenant:
                failures.append(
                    f"tenant_id: read {tenant!r}, the walker's tenant is {request.tenant!r}"
                )
        else:
            if tenant == "omninode":
                failures.append("tenant_id is the house default 'omninode'")
            if request.tenant and tenant not in (None, request.tenant):
                failures.append(
                    f"tenant_id: read {tenant!r}, the walker's tenant is {request.tenant!r}"
                )
            if tenant is None:
                notes.append(
                    "tenant_id is null on this deployed-lane terminal: read it from the projection row"
                )
        for name in ("input_tokens", "output_tokens", "cost_usd"):
            observe(f"metrics.{name}", metrics.get(name))
        notes.append(
            "Token counts are measured; cost_usd is CALCULATED from tier pricing, not a provider charge."
        )
        return ModelRouteCheckVerdict(
            outcome=EnumRouteCheckOutcome.INVALID_ROUTE
            if failures
            else EnumRouteCheckOutcome.VALID,
            run_directory=request.run_directory,
            route=request.route,
            pin=request.pin,
            observations=tuple(observations),
            failures=tuple(failures),
            notes=tuple(notes),
        )
