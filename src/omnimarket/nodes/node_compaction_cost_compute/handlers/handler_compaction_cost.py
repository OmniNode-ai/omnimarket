# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compaction cost accounting (OMN-21966).

Four lines per arm, each ``measured``, ``estimated`` or ``unavailable``:

* compaction tokens, input plus output. Stock: the boundary's ``preTokens`` is the size
  of the context that was summarized, a proxy for the summarizer's input and not its
  recorded request usage, so it is ``estimated``; the output is estimated from the
  summary text, because the transcript does not record the call's usage. A summarizer
  usage record, where one is found, replaces each count it carries and that line becomes
  ``measured``. Mod: the pack build and any routed model call from the caller's usage
  records, ``measured``; the pack build record is required.
* the context size after (``postTokens``).
* recovery: the input tokens of the results of recovery calls in the first N turns
  after the boundary, a recovery call being one that matches the recovery-call list.
* net: ``preTokens - postTokens``.

The accounting equations are fixed by the plan and are the only formulas here::

    cost(arm) = compaction tokens (input plus output) + recovery tokens
    net saved context = preTokens - postTokens
    ratio = cost(mod) / cost(stock)

A ratio with any estimated line is ``ESTIMATED`` and carries the bound with every
estimated line perturbed by 50 percent in the unfavourable direction (mod lines up,
stock lines down); its safeguard holds only if the bound holds. The token estimate of
a summary text is an injected estimator, not an accounting equation. A count that is
absent is ``unavailable`` and never zero. The handler performs no I/O beyond reading
its packaged default recovery-call list, and writes nothing anywhere.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Protocol

import yaml

from omnimarket.nodes.node_compaction_cost_compute.models import (
    PERTURBATION_FRACTION,
    EnumProvenance,
    EnumRatioStatus,
    EnumSafeguard,
    EnumUsageKind,
    ModelArmCost,
    ModelArmInput,
    ModelCompactionCostRequest,
    ModelCompactionCostResult,
    ModelCostLine,
    ModelCostRatio,
    ModelCountedCall,
    ModelRecoveryPattern,
    ModelToolCall,
    ModelUsageRecord,
)

__all__ = [
    "CharsPerTokenEstimator",
    "HandlerCompactionCost",
    "ProtocolTokenEstimator",
    "load_default_recovery_patterns",
]

_PATTERNS_FILE = (
    Path(__file__).resolve().parent.parent / "data" / ("recovery_call_patterns.yaml")
)
_UNAVAILABLE = EnumProvenance.UNAVAILABLE
_ESTIMATED = EnumProvenance.ESTIMATED
_MEASURED = EnumProvenance.MEASURED


class ProtocolTokenEstimator(Protocol):
    def estimate_tokens(self, text: str) -> int: ...


class CharsPerTokenEstimator:
    """Estimates a text's tokens at a fixed count of characters per token."""

    def __init__(self, chars_per_token: int = 4) -> None:
        self._chars_per_token = chars_per_token

    def estimate_tokens(self, text: str) -> int:
        return math.ceil(len(text) / self._chars_per_token)


def load_default_recovery_patterns() -> tuple[ModelRecoveryPattern, ...]:
    document = yaml.safe_load(_PATTERNS_FILE.read_text())
    return tuple(ModelRecoveryPattern.model_validate(p) for p in document["patterns"])


def _line(
    name: str, value: int | None, provenance: EnumProvenance, basis: str
) -> ModelCostLine:
    if value is None:
        return ModelCostLine(
            name=name, value=None, provenance=_UNAVAILABLE, basis=basis
        )
    return ModelCostLine(name=name, value=value, provenance=provenance, basis=basis)


def _unavailable(name: str, basis: str) -> ModelCostLine:
    return _line(name, None, _UNAVAILABLE, basis)


def _combine(*provenances: EnumProvenance) -> EnumProvenance:
    if _UNAVAILABLE in provenances:
        return _UNAVAILABLE
    if _ESTIMATED in provenances:
        return _ESTIMATED
    return _MEASURED


def _sum_lines(
    name: str, first: ModelCostLine, second: ModelCostLine, basis: str
) -> ModelCostLine:
    if first.value is None or second.value is None:
        return _unavailable(name, f"{basis}: a component is unavailable")
    return _line(
        name,
        first.value + second.value,
        _combine(first.provenance, second.provenance),
        basis,
    )


def _sources(records: list[ModelUsageRecord]) -> str:
    return ", ".join(sorted({f"{r.kind.value} from {r.source}" for r in records}))


class HandlerCompactionCost:
    """Account one boundary's compaction cost per arm and compare the arms."""

    def __init__(self, *, estimator: ProtocolTokenEstimator | None = None) -> None:
        self._estimator = (
            estimator if estimator is not None else CharsPerTokenEstimator()
        )

    def handle(self, request: ModelCompactionCostRequest) -> ModelCompactionCostResult:
        patterns = (
            request.recovery_patterns
            if request.recovery_patterns is not None
            else load_default_recovery_patterns()
        )
        stock = (
            self._stock(request.stock, request.window_turns, patterns)
            if request.stock is not None
            else None
        )
        mod = (
            self._mod(request.mod, request.window_turns, patterns)
            if request.mod is not None
            else None
        )
        ratio = (
            self._ratio(stock, mod, request.safeguard_upper_bound)
            if stock is not None and mod is not None
            else None
        )
        return ModelCompactionCostResult(stock=stock, mod=mod, ratio=ratio)

    # -- compaction tokens ------------------------------------------------

    def _stock_compaction(
        self, arm: ModelArmInput
    ) -> tuple[ModelCostLine, ModelCostLine]:
        records = [
            r for r in arm.usage_records if r.kind is EnumUsageKind.SUMMARIZER_CALL
        ]
        recorded_in = next((r for r in records if r.input_tokens is not None), None)
        recorded_out = next((r for r in records if r.output_tokens is not None), None)
        if recorded_in is not None and recorded_in.input_tokens is not None:
            tokens_in = _line(
                "compaction_input",
                recorded_in.input_tokens,
                _MEASURED,
                f"summarizer request usage from {recorded_in.source}",
            )
        elif arm.boundary.pre_tokens is not None:
            tokens_in = _line(
                "compaction_input",
                arm.boundary.pre_tokens,
                _ESTIMATED,
                "boundary preTokens, the size of the summarized context: "
                "a proxy for the summarizer input, not its recorded usage",
            )
        else:
            tokens_in = _unavailable(
                "compaction_input", "no summarizer usage record and no preTokens"
            )
        if recorded_out is not None and recorded_out.output_tokens is not None:
            tokens_out = _line(
                "compaction_output",
                recorded_out.output_tokens,
                _MEASURED,
                f"summarizer request usage from {recorded_out.source}",
            )
        elif arm.summary_text is not None:
            tokens_out = _line(
                "compaction_output",
                self._estimator.estimate_tokens(arm.summary_text),
                _ESTIMATED,
                "estimated from the summary text; the transcript does not record "
                "the summarizer call's usage",
            )
        else:
            tokens_out = _unavailable(
                "compaction_output", "no summarizer usage record and no summary text"
            )
        return tokens_in, tokens_out

    @staticmethod
    def _mod_compaction(arm: ModelArmInput) -> tuple[ModelCostLine, ModelCostLine]:
        records = [r for r in arm.usage_records if r.kind in _MOD_RECORD_KINDS]
        if not any(r.kind is EnumUsageKind.PACK_BUILD for r in records):
            basis = "no pack build usage record"
            return (
                _unavailable("compaction_input", basis),
                _unavailable("compaction_output", basis),
            )
        basis = f"usage records: {_sources(records)}"
        in_counts = [r.input_tokens for r in records]
        out_counts = [r.output_tokens for r in records]
        tokens_in = (
            _unavailable("compaction_input", f"{basis}; a record has no input count")
            if any(c is None for c in in_counts)
            else _line(
                "compaction_input",
                sum(c for c in in_counts if c is not None),
                _MEASURED,
                basis,
            )
        )
        tokens_out = (
            _unavailable("compaction_output", f"{basis}; a record has no output count")
            if any(c is None for c in out_counts)
            else _line(
                "compaction_output",
                sum(c for c in out_counts if c is not None),
                _MEASURED,
                basis,
            )
        )
        return tokens_in, tokens_out

    # -- recovery ---------------------------------------------------------

    @staticmethod
    def _matching(
        call: ModelToolCall, patterns: tuple[ModelRecoveryPattern, ...]
    ) -> ModelRecoveryPattern | None:
        for pattern in patterns:
            if call.tool_name not in pattern.tool_names:
                continue
            if pattern.target_regex is None or re.search(
                pattern.target_regex, call.target
            ):
                return pattern
        return None

    def _recovery(
        self,
        arm: ModelArmInput,
        window_turns: int,
        patterns: tuple[ModelRecoveryPattern, ...],
    ) -> tuple[ModelCostLine, tuple[ModelCountedCall, ...], int]:
        window = sorted(arm.turns, key=lambda t: t.index)[:window_turns]
        counted: list[ModelCountedCall] = []
        for turn in window:
            for call in turn.tool_calls:
                pattern = self._matching(call, patterns)
                if pattern is not None:
                    counted.append(
                        ModelCountedCall(
                            pattern_id=pattern.pattern_id,
                            kind=pattern.kind,
                            tool_name=call.tool_name,
                            target=call.target,
                            turn_index=turn.index,
                            tokens=call.result_input_tokens,
                            provenance=call.provenance,
                        )
                    )
        if not window:
            return (
                _unavailable("recovery", "no turns observed after the boundary"),
                (),
                0,
            )
        if any(c.tokens is None for c in counted):
            return (
                _unavailable(
                    "recovery", "a recovery call has no result input token count"
                ),
                tuple(counted),
                len(window),
            )
        basis = (
            f"{len(counted)} recovery calls in {len(window)} turns "
            "matched against the recovery-call list"
        )
        line = _line(
            "recovery",
            sum(c.tokens for c in counted if c.tokens is not None),
            _combine(*(c.provenance for c in counted)),
            basis,
        )
        return line, tuple(counted), len(window)

    # -- arm --------------------------------------------------------------

    def _arm(
        self,
        name: str,
        arm: ModelArmInput,
        tokens: tuple[ModelCostLine, ModelCostLine],
        window_turns: int,
        patterns: tuple[ModelRecoveryPattern, ...],
    ) -> ModelArmCost:
        tokens_in, tokens_out = tokens
        compaction = _sum_lines(
            "compaction", tokens_in, tokens_out, "compaction input plus output"
        )
        post = arm.boundary.post_tokens
        post_line = (
            _line("post_size", post, _MEASURED, "boundary postTokens")
            if post is not None
            else _unavailable("post_size", "the boundary records no postTokens")
        )
        recovery, counted, observed = self._recovery(arm, window_turns, patterns)
        pre = arm.boundary.pre_tokens
        net = (
            _line(
                "net",
                pre - post,
                _MEASURED,
                "boundary preTokens minus postTokens",
            )
            if pre is not None and post is not None
            else _unavailable("net", "the boundary lacks preTokens or postTokens")
        )
        cost = _sum_lines(
            "cost", compaction, recovery, "compaction tokens plus recovery tokens"
        )
        return ModelArmCost(
            arm=name,
            session_id=arm.session_id,
            boundary_uuid=arm.boundary.uuid,
            compaction_input=tokens_in,
            compaction_output=tokens_out,
            compaction=compaction,
            post_size=post_line,
            recovery=recovery,
            net=net,
            cost=cost,
            counted_calls=counted,
            window_turns=window_turns,
            turns_observed=observed,
            window_complete=observed >= window_turns,
        )

    def _stock(
        self,
        arm: ModelArmInput,
        window_turns: int,
        patterns: tuple[ModelRecoveryPattern, ...],
    ) -> ModelArmCost:
        return self._arm(
            "stock", arm, self._stock_compaction(arm), window_turns, patterns
        )

    def _mod(
        self,
        arm: ModelArmInput,
        window_turns: int,
        patterns: tuple[ModelRecoveryPattern, ...],
    ) -> ModelArmCost:
        return self._arm("mod", arm, self._mod_compaction(arm), window_turns, patterns)

    # -- ratio ------------------------------------------------------------

    @staticmethod
    def _ratio(
        stock: ModelArmCost, mod: ModelArmCost, bound: float | None
    ) -> ModelCostRatio:
        if stock.cost.value is None or mod.cost.value is None:
            return ModelCostRatio(
                ratio=None,
                status=EnumRatioStatus.UNAVAILABLE,
                basis="a cost line is unavailable, so no ratio is computed",
            )
        if stock.cost.value == 0:
            return ModelCostRatio(
                ratio=None,
                status=EnumRatioStatus.UNAVAILABLE,
                basis="the stock cost is zero, so the ratio is undefined",
            )
        ratio = mod.cost.value / stock.cost.value
        parts = [
            (f"{arm.arm}.{line.name}", arm.arm, line)
            for arm in (stock, mod)
            for line in (arm.compaction_input, arm.compaction_output, arm.recovery)
        ]
        estimated = tuple(
            label for label, _, line in parts if line.provenance is _ESTIMATED
        )
        if not estimated:
            return ModelCostRatio(
                ratio=ratio,
                status=EnumRatioStatus.MEASURED,
                perturbed_upper_ratio=ratio,
                safeguard_upper_bound=bound,
                safeguard=_verdict(ratio, bound),
                basis="cost(mod) / cost(stock), every line measured",
            )
        stock_cost = 0.0
        mod_cost = 0.0
        for _, arm_name, line in parts:
            value = float(line.value or 0)
            if line.provenance is _ESTIMATED:
                value *= (
                    1 - PERTURBATION_FRACTION
                    if arm_name == "stock"
                    else 1 + PERTURBATION_FRACTION
                )
            if arm_name == "stock":
                stock_cost += value
            else:
                mod_cost += value
        perturbed = mod_cost / stock_cost
        return ModelCostRatio(
            ratio=ratio,
            status=EnumRatioStatus.ESTIMATED,
            estimated_lines=estimated,
            perturbed_upper_ratio=perturbed,
            safeguard_upper_bound=bound,
            safeguard=_verdict(perturbed, bound),
            basis=(
                "cost(mod) / cost(stock) with an estimated line; the bound perturbs "
                "every estimated line by 50 percent, mod up and stock down"
            ),
        )


_MOD_RECORD_KINDS = frozenset({EnumUsageKind.PACK_BUILD, EnumUsageKind.ROUTED_CALL})


def _verdict(tested: float, bound: float | None) -> EnumSafeguard | None:
    if bound is None:
        return None
    return (
        EnumSafeguard.ESTABLISHED if tested <= bound else EnumSafeguard.NOT_ESTABLISHED
    )
