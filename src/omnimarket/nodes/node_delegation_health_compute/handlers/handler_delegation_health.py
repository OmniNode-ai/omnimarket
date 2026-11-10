# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge delegation health from counts, by the rules the contract declares (OMN-21927)."""

from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_health_compute.models.model_delegation_health import (
    EnumDelegationHealthFindingKind,
    EnumDelegationHealthVerdict,
    EnumExpectedRungRule,
    ModelBackendCount,
    ModelDelegationHealthFinding,
    ModelDelegationHealthRequest,
    ModelDelegationHealthRules,
    ModelDelegationHealthVerdict,
    ModelModelCount,
    ModelRung,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"
_HARNESS_PREFIX = "harness:"


def load_rules(contract: Path = _CONTRACT) -> ModelDelegationHealthRules:
    """Read the verdict_rules of a contract; an unknown or missing rule is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("verdict_rules"), dict):
        raise ValueError("contract verdict_rules must be a mapping")
    return ModelDelegationHealthRules.model_validate(data["verdict_rules"])


def rung_evidence(count: ModelBackendCount) -> str:
    """PASS or FAIL with the rung's attempts and accepted answers in 24h."""
    accepted_tail = (
        f", newest accepted {count.last_accept}"
        if count.accepted > 0 and count.last_accept and count.last_accept != count.last
        else ""
    )
    return (
        f"{'PASS' if count.accepted > 0 else 'FAIL'} attempts={count.counts.h24} "
        f"accepted={count.accepted} in 24h, newest {count.last or '-'}{accepted_tail}"
    )


class HandlerDelegationHealth:
    """Pure compute over caller-read counts and the contract's verdict_rules."""

    def __init__(self, rules: ModelDelegationHealthRules | None = None) -> None:
        self._rules = rules if rules is not None else load_rules()

    def handle(
        self, request: ModelDelegationHealthRequest
    ) -> ModelDelegationHealthVerdict:
        """Return OK, DEGRADED with a reason and one FIX each, UNREADABLE or UNTRUSTED."""
        if request.read_failure.strip():
            return self._verdict(
                request,
                EnumDelegationHealthVerdict.UNREADABLE,
                f"VERDICT: UNREADABLE reason={request.read_failure}",
            )
        if request.total_rows == 0:
            return self._verdict(
                request,
                EnumDelegationHealthVerdict.UNTRUSTED,
                "VERDICT: UNTRUSTED reason=the table read 0 rows in total; "
                "a zero with no positive control is not a measurement",
            )
        by_backend = {c.backend_id: c for c in request.backends}
        not_judged: list[str] = []
        findings = [
            *self._unresolved(request),
            *self._pin_only(request, by_backend),
            *self._harness(by_backend),
            *self._timeouts(request, not_judged),
            *self._expected(request, by_backend, not_judged),
        ]
        evidence = [
            *self._pin_only_lines(request, by_backend),
            *self._harness_lines(by_backend),
            *self._outcome_lines(request),
        ]
        if findings:
            reasons = "; ".join(f.reason for f in findings)
            return self._verdict(
                request,
                EnumDelegationHealthVerdict.DEGRADED,
                f"VERDICT: DEGRADED reason={reasons}",
                findings,
                not_judged,
                evidence,
            )
        suffix = f" (not judged: {'; '.join(not_judged)})" if not_judged else ""
        return self._verdict(
            request,
            EnumDelegationHealthVerdict.OK,
            f"VERDICT: OK{suffix}",
            findings,
            not_judged,
            evidence,
        )

    def _verdict(
        self,
        request: ModelDelegationHealthRequest,
        verdict: EnumDelegationHealthVerdict,
        summary: str,
        findings: list[ModelDelegationHealthFinding] | None = None,
        not_judged: list[str] | None = None,
        evidence: list[str] | None = None,
    ) -> ModelDelegationHealthVerdict:
        return ModelDelegationHealthVerdict(
            correlation_id=request.correlation_id,
            verdict=verdict,
            exit_code=self._rules.exit_codes[verdict],
            findings=tuple(findings or ()),
            not_judged=tuple(not_judged or ()),
            evidence=tuple(evidence or ()),
            summary=summary,
        )

    def _finding(
        self,
        kind: EnumDelegationHealthFindingKind,
        reason: str,
        **fix_fields: str,
    ) -> ModelDelegationHealthFinding:
        fix = self._rules.fixes[kind].format(
            smoke=self._rules.smoke_command, **fix_fields
        )
        return ModelDelegationHealthFinding(kind=kind, reason=reason, fix=fix)

    def _unresolved(
        self, request: ModelDelegationHealthRequest
    ) -> list[ModelDelegationHealthFinding]:
        return [
            self._finding(
                EnumDelegationHealthFindingKind.EXPECTED_TIER_UNRESOLVED,
                f"expected tier {tier} has no model with a live endpoint in the rendered contract",
            )
            for tier in request.unresolved_tiers
        ]

    def _pin_only(
        self,
        request: ModelDelegationHealthRequest,
        by_backend: dict[str, ModelBackendCount],
    ) -> list[ModelDelegationHealthFinding]:
        out = []
        for rung in request.pin_only_rungs:
            count = by_backend.get(rung.backend_id)
            if count is None or count.counts.h24 == 0 or count.accepted != 0:
                continue
            out.append(
                self._finding(
                    EnumDelegationHealthFindingKind.PIN_ONLY_RUNG,
                    f"pin-only rung {rung.backend_id} ({rung.provider}/{rung.model}, "
                    f"{rung.tier}) had {count.counts.h24} attempts and 0 accepted "
                    f"answers in 24h, newest {count.last or '-'}",
                    backend_id=rung.backend_id,
                )
            )
        return out

    def _harness(
        self, by_backend: dict[str, ModelBackendCount]
    ) -> list[ModelDelegationHealthFinding]:
        out = []
        for backend_id, count in sorted(by_backend.items()):
            if not backend_id.startswith(_HARNESS_PREFIX):
                continue
            if count.counts.h24 == 0 or count.accepted != 0:
                continue
            _, harness, model = (*backend_id.split(":", 2), "", "")[:3]
            out.append(
                self._finding(
                    EnumDelegationHealthFindingKind.HARNESS_RUNG,
                    f"harness rung {backend_id} had {count.counts.h24} attempts and 0 "
                    f"accepted answers in 24h, newest {count.last or '-'}",
                    harness=harness,
                    model=model or "<model>",
                )
            )
        return out

    def _timeout_totals(
        self, request: ModelDelegationHealthRequest
    ) -> tuple[int, int, int, int] | None:
        """(timeouts in the window, runs in the window, timeouts in 1h, runs in 1h)."""
        if request.outcomes is None:
            return None
        timeouts = [
            c.counts
            for c in request.outcomes.causes
            if c.cause == self._rules.timeout_failure_cause
        ]
        return (
            sum(c.window for c in timeouts),
            request.outcomes.runs.window,
            sum(c.h1 for c in timeouts),
            request.outcomes.runs.h1,
        )

    def _timeouts(
        self, request: ModelDelegationHealthRequest, not_judged: list[str]
    ) -> list[ModelDelegationHealthFinding]:
        rules = self._rules
        totals = self._timeout_totals(request)
        if totals is None:
            not_judged.append(
                "timeouts: the table has no terminal_failure_cause column"
            )
            return []
        timed_out, runs, timed_out_1h, runs_1h = totals
        if runs < rules.timeout_min_events:
            not_judged.append(
                f"timeouts: {runs} real runs in {request.quiet_minutes}m is below the "
                f"minimum of {rules.timeout_min_events}"
            )
            return []
        if timed_out * 100 < runs * rules.timeout_share_percent:
            return []
        return [
            self._finding(
                EnumDelegationHealthFindingKind.TIMEOUTS_DOMINATE,
                f"timeouts dominate: {timed_out} of {runs} delegations "
                f"({round(100 * timed_out / runs)}%) in {request.quiet_minutes}m were "
                f"not routed ({rules.timeout_failure_cause}), {timed_out_1h} of {runs_1h} "
                f"in 1h (threshold {rules.timeout_share_percent}% of at least "
                f"{rules.timeout_min_events})",
            )
        ]

    def _expected(
        self,
        request: ModelDelegationHealthRequest,
        by_backend: dict[str, ModelBackendCount],
        not_judged: list[str],
    ) -> list[ModelDelegationHealthFinding]:
        if not request.expected_rungs:
            return []
        if request.lanes_running == 0:
            not_judged.append("expected rungs: no lanes running")
            return []
        by_model = {m.model: m for m in request.models}
        out = []
        first = request.expected_rungs[0]
        for rung in request.expected_rungs:
            seen = self._rung_events(rung, by_backend, by_model)
            if seen is not None and seen[0] > 0:
                continue
            last = f", last {seen[1]}" if seen and seen[1] else ", none in 24h"
            pin = "" if rung is first else f" --backend-id {rung.backend_id}"
            out.append(
                self._finding(
                    EnumDelegationHealthFindingKind.EXPECTED_RUNG_SILENT,
                    f"delegation {rung.provider}/{rung.model} ({rung.tier} rung "
                    f"{rung.backend_id}) had 0 events in {request.quiet_minutes}m while "
                    f"{request.lanes_running} lanes ran{last}",
                    pin=pin,
                )
            )
        return out

    def _rung_events(
        self,
        rung: ModelRung,
        by_backend: dict[str, ModelBackendCount],
        by_model: dict[str, ModelModelCount],
    ) -> tuple[int, str] | None:
        """(events in the window, newest) by the contract's expected-rung rule."""
        if (
            self._rules.expected_rung_rule
            is EnumExpectedRungRule.BACKEND_ROW_THEN_MODEL
        ):
            backend = by_backend.get(rung.backend_id)
            if backend is not None:
                return backend.counts.window, backend.last
        model = by_model.get(rung.model)
        if model is None:
            return None
        return model.counts.window, model.last

    @staticmethod
    def _pin_only_lines(
        request: ModelDelegationHealthRequest,
        by_backend: dict[str, ModelBackendCount],
    ) -> list[str]:
        out = []
        for rung in request.pin_only_rungs:
            count = by_backend.get(rung.backend_id)
            seen = (
                rung_evidence(count)
                if count and count.counts.h24
                else "no smoke or pinned run in 24h"
            )
            out.append(
                f"pin-only rung {rung.backend_id} ({rung.provider}/{rung.model}, "
                f"{rung.tier}; ordinary routing excludes it): {seen}"
            )
        return out

    @staticmethod
    def _harness_lines(by_backend: dict[str, ModelBackendCount]) -> list[str]:
        return [
            f"harness rung {backend_id}: {rung_evidence(count)}"
            for backend_id, count in sorted(by_backend.items())
            if backend_id.startswith(_HARNESS_PREFIX)
        ]

    def _outcome_lines(self, request: ModelDelegationHealthRequest) -> list[str]:
        totals = self._timeout_totals(request)
        if totals is None or request.outcomes is None:
            return [
                "outcomes: not judged, the table has no terminal_failure_cause column"
            ]
        timed_out, runs, timed_out_1h, runs_1h = totals
        timeouts_24h = sum(
            c.counts.h24
            for c in request.outcomes.causes
            if c.cause == self._rules.timeout_failure_cause
        )
        share = f"{round(100 * timed_out / runs)}%" if runs else "-"
        return [
            "outcomes (real runs, terminal_failure_cause): "
            f"timeouts 1h={timed_out_1h}/{runs_1h} {request.quiet_minutes}m={timed_out}/{runs} "
            f"({share}) 24h={timeouts_24h}/{request.outcomes.runs.h24}; DEGRADED at "
            f"{self._rules.timeout_share_percent}% of at least "
            f"{self._rules.timeout_min_events} runs in {request.quiet_minutes}m"
        ]
