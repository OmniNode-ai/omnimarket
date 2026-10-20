# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which Claude review key can afford a call and keep its reserve (OMN-20967).

``HandlerClaudeReviewBudget.handle(ModelClaudeReviewBudgetRequest) -> ModelClaudeReviewBudgetResult``
(definition-B).

Each key has an allowance per cycle and keeps a reserve unspent (operator
ruling 2026-10-12T04:57:20Z). A key's spend is the cost of the calls recorded
on it since its current cycle started, so a cycle rollover starts the spend
again at zero. A call's worst case is its counted prompt tokens at the input
price plus ``max_tokens`` at the output price. Keys are tried in the order
given (fill-first, never round-robin): the first whose allowance less its
spend less the worst case still holds the reserve is chosen. When none does,
the result names no key, and the gateway answers 503 with no upstream call so
the voter falls through to its overflow endpoint.

Prices come from the pricing manifest; the allowance, reserve, cycle and cache
multiples come from the contract. The request or injected server config chooses
the model; the packaged contract carries no deployment model default. A model the manifest
does not carry chooses no key. No clock (``as_of`` is in the request), no I/O.
"""

from __future__ import annotations

from decimal import Decimal
from importlib.resources import files

import yaml
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.enums.enum_claude_review_budget import EnumClaudeReviewBudgetReason
from omnimarket.models.claude_review_budget.claude_review_budget_math import (
    ModelClaudeReviewTokenPrices,
    cycle_start,
    prices_from_manifest,
)
from omnimarket.models.claude_review_budget.model_claude_review_usage import (
    ModelClaudeReviewUsage,
)
from omnimarket.models.claude_review_budget.model_review_budget_admission import (
    ModelReviewBudgetQuote,
)
from omnimarket.nodes.node_claude_review_budget_compute.models.model_claude_review_budget import (
    ModelClaudeReviewBudgetConfig,
    ModelClaudeReviewBudgetRequest,
    ModelClaudeReviewBudgetResult,
    ModelClaudeReviewKeyBudget,
)

_NODE_PACKAGE = "omnimarket.nodes.node_claude_review_budget_compute"


def load_budget_config(
    contract_text: str | None = None,
) -> ModelClaudeReviewBudgetConfig:
    """The ``budget`` block of the node's packaged contract, or of ``contract_text``."""
    text = (
        contract_text
        if contract_text is not None
        else files(_NODE_PACKAGE).joinpath("contract.yaml").read_text(encoding="utf-8")
    )
    contract = yaml.safe_load(text)
    return ModelClaudeReviewBudgetConfig.model_validate(contract["budget"])


class HandlerClaudeReviewBudget:
    """Choose the fill-first key that can afford the call, or none."""

    def __init__(
        self,
        *,
        config: ModelClaudeReviewBudgetConfig | None = None,
        pricing_table: ModelPricingTable | None = None,
    ) -> None:
        self._config = config if config is not None else load_budget_config()
        self._table = (
            pricing_table
            if pricing_table is not None
            else ModelPricingTable.from_yaml()
        )

    def _prices(self, model_id: str) -> ModelClaudeReviewTokenPrices | None:
        return prices_from_manifest(
            self._table,
            model_id,
            cache_write_input_multiplier=self._config.cache_write_input_multiplier,
            cache_read_input_multiplier=self._config.cache_read_input_multiplier,
            cache_write_1h_input_multiplier=self._config.cache_write_1h_input_multiplier,
        )

    def quote(self, request: ModelReviewBudgetQuote) -> Decimal | None:
        """Maximum charge: each cacheable block can be a cold write at its own TTL."""
        prices = self._prices(request.model_id)
        if prices is None:
            return None
        rates = {
            "uncached": prices.input_usd,
            "5m": max(prices.input_usd, prices.cache_write_usd),
            "1h": max(prices.input_usd, prices.cache_write_1h_usd),
        }
        cost = sum((b.tokens * rates[b.cache_ttl] for b in request.blocks), Decimal(0))
        # Tools precede the cached prefix on Messages requests. Reserve their tokens
        # at the largest block write rate too, including a cold 1-hour cache write.
        schema_price = max(
            (rates[b.cache_ttl] for b in request.blocks), default=prices.input_usd
        )
        cost += (
            request.schema_tokens * schema_price
            + request.max_tokens * prices.output_usd
        )
        return cost * (
            self._config.batch_multiplier if request.mode == "batch" else Decimal(1)
        )

    def measured_cost(
        self, usage: ModelClaudeReviewUsage, *, batch: bool
    ) -> Decimal | None:
        """Price the provider's observed usage, including the cache creation TTL split."""
        prices = self._prices(usage.model_id)
        if prices is None:
            return None
        return prices.cost_of(usage) * (
            self._config.batch_multiplier if batch else Decimal(1)
        )

    def handle(
        self, request: ModelClaudeReviewBudgetRequest
    ) -> ModelClaudeReviewBudgetResult:
        cfg = self._config
        model_id = (
            request.quote.model_id
            if request.quote is not None
            else request.model_id or cfg.model_id
        )
        if model_id is None:
            return ModelClaudeReviewBudgetResult(
                decided_at=request.as_of,
                model_id=None,
                reason=EnumClaudeReviewBudgetReason.MODEL_NOT_PRICED,
                chosen_api_key_ref=None,
                allowance_usd=cfg.allowance_usd,
                reserve_usd=cfg.reserve_usd,
                worst_case_usd=None,
                keys=(),
                detail="no model selected by the request or server overlay",
            )
        models = {model_id} | {c.model_id for k in request.keys for c in k.calls}
        prices = {m: self._prices(m) for m in sorted(models)}
        unpriced = [m for m, p in prices.items() if p is None]
        call_prices = prices[model_id]
        worst = (
            self.quote(request.quote)
            if request.quote is not None
            else (
                call_prices.worst_case(
                    prompt_tokens=request.prompt_tokens, max_tokens=request.max_tokens
                )
                if call_prices is not None
                else None
            )
        )

        readback: list[ModelClaudeReviewKeyBudget] = []
        for key in request.keys:
            started = cycle_start(key.cycle_anchor_at, request.as_of, cfg.cycle_months)
            counted = [
                c for c in key.calls if started <= c.observed_at <= request.as_of
            ]
            spent = sum(
                (
                    p.cost_of(c)
                    for c in counted
                    if (p := prices[c.model_id]) is not None
                ),
                Decimal(0),
            )
            remaining = cfg.allowance_usd - spent
            readback.append(
                ModelClaudeReviewKeyBudget(
                    api_key_ref=key.api_key_ref,
                    cycle_started_at=started,
                    calls_counted=len(counted),
                    spent_usd=spent,
                    remaining_usd=remaining,
                    affordable=(
                        worst is not None
                        and not unpriced
                        and remaining - worst >= cfg.reserve_usd
                    ),
                )
            )

        chosen = next((k for k in readback if k.affordable), None)
        if unpriced:
            reason = EnumClaudeReviewBudgetReason.MODEL_NOT_PRICED
            detail = f"not in the pricing manifest: {', '.join(unpriced)}"
        elif not request.keys:
            reason = EnumClaudeReviewBudgetReason.NO_KEYS
            detail = "no key to spend"
        elif chosen is None:
            reason = EnumClaudeReviewBudgetReason.NO_KEY_ABOVE_RESERVE
            detail = (
                f"every key would fall below its {cfg.reserve_usd} USD reserve "
                f"at a worst case of {worst} USD"
            )
        else:
            reason = EnumClaudeReviewBudgetReason.KEY_CHOSEN
            detail = (
                f"{chosen.api_key_ref} keeps {chosen.remaining_usd - (worst or 0)} USD "
                f"after a worst case of {worst} USD"
            )
        return ModelClaudeReviewBudgetResult(
            decided_at=request.as_of,
            model_id=model_id,
            reason=reason,
            chosen_api_key_ref=(
                chosen.api_key_ref
                if reason is EnumClaudeReviewBudgetReason.KEY_CHOSEN and chosen
                else None
            ),
            allowance_usd=cfg.allowance_usd,
            reserve_usd=cfg.reserve_usd,
            worst_case_usd=worst,
            keys=tuple(readback),
            detail=detail,
        )


__all__ = ["HandlerClaudeReviewBudget", "load_budget_config"]
