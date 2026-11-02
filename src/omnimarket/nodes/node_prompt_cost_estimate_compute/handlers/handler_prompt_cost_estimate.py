# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Price each prompt of an uploaded export as it ran and as the delegate routes it.

Nothing here carries a price, a model id or a routing choice of its own. The
task class is the one the delegation router would give the prompt. The
recommended model is the first model the router's own tier ladder for that
class names, past its local tier, that the pricing manifest prices. Both costs
come from the manifest through the savings estimate's ``counterfactual_cost_usd``.
When delegating would not have been cheaper, the prompt is recommended to stay
where it ran, with a saving of zero. A prompt whose model the manifest does not
carry, or that names no model, is counted as unpriced and given no row; it is
never priced at another model's rate.

The CSV import schema and the saving range spreads come from the contract
through an injected provider (``contract_config.load_config`` by default). The
saving is a range: each manifest entry's ``confidence`` and each row's token
method select a spread from ``config.saving_range``, and ``pricing_basis``
names every manifest entry that priced a row, with its source and whether its
evidence is checked against invoices.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from functools import cache

import yaml
from omnibase_infra.models.pricing.model_pricing_entry import ModelPricingEntry
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config_payload,
)
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    TaskClassSelectionError,
    load_task_class_authority,
)
from omnimarket.models.delegation.model_packaged_routing_tiers import (
    ModelPackagedRoutingTiers,
)
from omnimarket.nodes.node_metering_summary_compute import (
    ModelCounterfactualBaseline,
    counterfactual_cost_usd,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import (
    resolve_baseline,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.contract_config import (
    ConfigProvider,
    load_config,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.export_parsers import (
    ParsedPrompt,
    parse_export,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    EnumModelSource,
    EnumUnpricedReason,
    ModelPricingBasis,
    ModelPromptCostEstimateRequest,
    ModelPromptCostEstimateResult,
    ModelPromptCostRow,
    EnumTokenMethod,
    ModelSavingRangeSpread,
    ModelTaskTypeSavings,
    ModelUnpricedModel,
)
from omnimarket.routing.routing_tiers_path import ROUTING_TIERS_PACKAGED_DEFAULT_PATH

# The tier that runs on the operator's own GPUs. A visitor has none, so the
# ladder is read from the first tier past it.
_LOCAL_TIER = "local"
# The recommended tier of a prompt whose cheapest route is where it already ran.
NOT_DELEGATED = "not_delegated"
# The task class of a refused prompt no class claims and no fallback names.
_UNCLASSIFIED = "unclassified"
_ZERO = Decimal("0")

BaselineResolver = Callable[[str], ModelCounterfactualBaseline | None]
PricingEntryResolver = Callable[[str], ModelPricingEntry | None]


def manifest_entry(model_id: str) -> ModelPricingEntry | None:
    """The canonical pricing manifest's entry for ``model_id``, or ``None``."""
    return ModelPricingTable.from_yaml().get_entry(model_id)


@cache
def _routing_config() -> ModelPackagedRoutingTiers:
    """The packaged tier ladder: what the product routes, not one deployment's."""
    return ModelPackagedRoutingTiers.model_validate(
        yaml.safe_load(ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8"))
    )


@cache
def _authority() -> ModelTaskClassAuthority:
    return load_task_class_authority()


@cache
def _backend_models() -> dict[str, str]:
    """Backend id to the provider model it serves, from the committed contract.

    The committed contract alone: an operator's local overlay must not change
    what a public estimate recommends.
    """
    path = ROUTING_TIERS_PACKAGED_DEFAULT_PATH.parent / "bifrost_delegation.yaml"
    config = load_bifrost_delegation_config_payload(
        path.read_bytes(), None, contract_source=str(path)
    )
    return {
        backend.backend_id: backend.model_name
        for backend in config.backends
        if backend.model_name
    }


def classify(prompt_text: str) -> tuple[str, bool]:
    """The router's task class for ``prompt_text`` and whether it would route it."""
    authority = _authority()
    try:
        return authority.resolve_task_type(prompt_text, explicit=None).task_type, True
    except TaskClassSelectionError:
        # A class the router claims but will not route: the prompt keeps its
        # model. Name the class it claimed so the visitor sees why.
        lowered = prompt_text.lower()
        for name, entry in authority.task_classes.items():
            if entry.selection.matching_phrase(lowered) is not None:
                return name, False
        fallback = authority.selection_fallback
        return (fallback.task_class if fallback else _UNCLASSIFIED), False


def delegate_candidates(task_type: str) -> tuple[tuple[str, str], ...]:
    """(tier, model) pairs the router would try for ``task_type``, past local."""
    entry = _authority().task_classes.get(task_type)
    config = _routing_config()
    tiers = {tier.name: tier for tier in config.tiers}
    policy = entry.escalation_policy if entry is not None else None
    order = (
        policy.tier_order
        if policy is not None and policy.tier_order is not None
        else tuple(tier.name for tier in config.tiers)
    )
    served = _backend_models()
    candidates: list[tuple[str, str]] = []
    for tier_name in order:
        tier = tiers.get(tier_name)
        if tier is None or tier_name == _LOCAL_TIER:
            continue
        candidates.extend(
            (tier_name, served.get(model.backend_id, model.id))
            for model in tier.models
            if not model.use_for or task_type in model.use_for
        )
    return tuple(candidates)


class HandlerPromptCostEstimate:
    """Estimate one uploaded export, prompt by prompt."""

    def __init__(
        self,
        baseline_resolver: BaselineResolver | None = None,
        config_provider: ConfigProvider | None = None,
        entry_resolver: PricingEntryResolver | None = None,
    ) -> None:
        self._resolve = (
            resolve_baseline if baseline_resolver is None else baseline_resolver
        )
        self._config = load_config if config_provider is None else config_provider
        self._entry = manifest_entry if entry_resolver is None else entry_resolver

    def handle(
        self, request: ModelPromptCostEstimateRequest
    ) -> ModelPromptCostEstimateResult:
        config = self._config()
        prompts = parse_export(
            request.export_format, request.content, config.csv_import_schema
        )
        baselines: dict[str, ModelCounterfactualBaseline | None] = {}
        entries: dict[str, ModelPricingEntry] = {}

        def entry(model: str) -> ModelPricingEntry:
            if model not in entries:
                found = self._entry(model)
                if found is None:
                    raise ValueError(
                        f"{model} has a price but no pricing manifest entry"
                    )
                entries[model] = found
            return entries[model]

        def price(model: str) -> ModelCounterfactualBaseline | None:
            if model not in baselines:
                baselines[model] = self._resolve(model)
            return baselines[model]

        rows: list[ModelPromptCostRow] = []
        priced: list[ParsedPrompt] = []
        unpriced: Counter[tuple[str, EnumUnpricedReason]] = Counter()
        versions: set[str] = set()
        for index, prompt in enumerate(prompts):
            model, source = self._model_for(prompt, request.declared_model)
            if not model:
                unpriced[("", EnumUnpricedReason.NO_MODEL_NAMED)] += 1
                continue
            baseline = price(model)
            if baseline is None:
                unpriced[(model, EnumUnpricedReason.MODEL_NOT_IN_MANIFEST)] += 1
                continue
            versions.add(baseline.pricing_manifest_version)
            priced.append(prompt)
            rows.append(
                self._row(
                    index,
                    prompt,
                    model,
                    source,
                    baseline,
                    price,
                    entry,
                    config.saving_range,
                )
            )

        tokens_total = sum(prompt.tokens_in + prompt.tokens_out for prompt in priced)
        from_text = sum(prompt.tokens_from_text_length for prompt in priced)
        return ModelPromptCostEstimateResult(
            pricing_manifest_version=self._manifest_version(versions),
            export_format=request.export_format,
            rows=tuple(rows),
            unpriced=tuple(
                ModelUnpricedModel(model=model, reason=reason, prompts=count)
                for (model, reason), count in sorted(unpriced.items())
            ),
            by_task_type=_by_task_type(rows),
            prompts_total=len(prompts),
            prompts_priced=len(rows),
            prompts_unpriced=sum(unpriced.values()),
            prompts_tokens_estimated=sum(1 for row in rows if row.tokens_estimated),
            tokens_total=tokens_total,
            tokens_from_text_length=from_text,
            tokens_estimated_share=(
                Decimal(from_text) / Decimal(tokens_total) if tokens_total else _ZERO
            ),
            actual_cost_usd=sum((row.actual_cost_usd for row in rows), _ZERO),
            delegated_cost_usd=sum((row.delegated_cost_usd for row in rows), _ZERO),
            saving_usd=sum((row.saving_usd for row in rows), _ZERO),
            saving_usd_low=sum((row.saving_usd_low for row in rows), _ZERO),
            saving_usd_high=sum((row.saving_usd_high for row in rows), _ZERO),
            pricing_basis=_pricing_basis(rows, entry, config.saving_range),
        )

    @staticmethod
    def _model_for(
        prompt: ParsedPrompt, declared_model: str | None
    ) -> tuple[str, EnumModelSource]:
        if prompt.model:
            return prompt.model, EnumModelSource.EXPORT
        return declared_model or "", EnumModelSource.DECLARED

    @staticmethod
    def _row(
        index: int,
        prompt: ParsedPrompt,
        model: str,
        source: EnumModelSource,
        baseline: ModelCounterfactualBaseline,
        price: BaselineResolver,
        entry: Callable[[str], ModelPricingEntry],
        spread: ModelSavingRangeSpread,
    ) -> ModelPromptCostRow:
        task_type, routable = classify(prompt.prompt_text)
        actual = counterfactual_cost_usd(
            baseline=baseline, tokens_in=prompt.tokens_in, tokens_out=prompt.tokens_out
        )
        recommended, tier, delegated = model, NOT_DELEGATED, actual
        if routable:
            for candidate_tier, candidate in delegate_candidates(task_type):
                candidate_baseline = price(candidate)
                if candidate_baseline is None:
                    continue
                cost = counterfactual_cost_usd(
                    baseline=candidate_baseline,
                    tokens_in=prompt.tokens_in,
                    tokens_out=prompt.tokens_out,
                )
                if cost < actual:
                    recommended, tier, delegated = candidate, candidate_tier, cost
                break
        low, high = _saving_range(
            actual=actual,
            delegated=delegated,
            delegated_away=tier != NOT_DELEGATED,
            source_spread=spread.confidence[entry(model).confidence],
            delegate_spread=spread.confidence[entry(recommended).confidence],
            token_spread=spread.token_method[prompt.token_method],
        )
        return ModelPromptCostRow(
            prompt_index=index,
            conversation_id=prompt.conversation_id,
            occurred_at=prompt.occurred_at,
            prompt_text=prompt.prompt_text,
            task_type=task_type,
            source_model=model,
            model_source=source,
            tokens_in=prompt.tokens_in,
            tokens_out=prompt.tokens_out,
            tokens_estimated=prompt.token_method is not EnumTokenMethod.REPORTED,
            token_method=prompt.token_method,
            actual_cost_usd=actual,
            recommended_model=recommended,
            recommended_tier=tier,
            delegated_cost_usd=delegated,
            saving_usd=actual - delegated,
            saving_usd_low=low,
            saving_usd_high=high,
        )

    @staticmethod
    def _manifest_version(versions: set[str]) -> str:
        if len(versions) > 1:
            raise ValueError(f"prices came from more than one manifest: {versions}")
        if versions:
            return versions.pop()
        return ModelPricingTable.from_yaml().schema_version


def _by_task_type(rows: list[ModelPromptCostRow]) -> tuple[ModelTaskTypeSavings, ...]:
    groups: dict[str, list[ModelPromptCostRow]] = {}
    for row in rows:
        groups.setdefault(row.task_type, []).append(row)
    return tuple(
        ModelTaskTypeSavings(
            task_type=task_type,
            prompts=len(group),
            actual_cost_usd=sum((row.actual_cost_usd for row in group), _ZERO),
            delegated_cost_usd=sum((row.delegated_cost_usd for row in group), _ZERO),
            saving_usd=sum((row.saving_usd for row in group), _ZERO),
            saving_usd_low=sum((row.saving_usd_low for row in group), _ZERO),
            saving_usd_high=sum((row.saving_usd_high for row in group), _ZERO),
        )
        for task_type, group in sorted(groups.items())
    )


def _saving_range(
    *,
    actual: Decimal,
    delegated: Decimal,
    delegated_away: bool,
    source_spread: Decimal,
    delegate_spread: Decimal,
    token_spread: Decimal,
) -> tuple[Decimal, Decimal]:
    """The saving's low and high ends.

    Each model's price moves by its confidence spread, against the saving at the
    low end and for it at the high end; the token method's spread then scales
    the saving, since both costs share the row's tokens. A prompt that stays
    where it ran saves nothing whatever the prices are.
    """
    if not delegated_away:
        return _ZERO, _ZERO
    low = (actual * (1 - source_spread) - delegated * (1 + delegate_spread)) * (
        1 - token_spread
    )
    high = (actual * (1 + source_spread) - delegated * (1 - delegate_spread)) * (
        1 + token_spread
    )
    return max(low, _ZERO), high


def _pricing_basis(
    rows: list[ModelPromptCostRow],
    entry: Callable[[str], ModelPricingEntry],
    spread: ModelSavingRangeSpread,
) -> tuple[ModelPricingBasis, ...]:
    """Every manifest entry that priced a row: the answering model and any delegate."""
    used: Counter[str] = Counter()
    for row in rows:
        used[row.source_model] += 1
        if row.recommended_tier != NOT_DELEGATED:
            used[row.recommended_model] += 1
    basis: list[ModelPricingBasis] = []
    for model, count in sorted(used.items()):
        found = entry(model)
        authoritative = found.evidence.get("authoritative") is True
        basis.append(
            ModelPricingBasis(
                model=model,
                confidence=found.confidence,
                source=found.source,
                evidence_authoritative=authoritative,
                sample_count=found.sample_count,
                checked_against_invoices=authoritative and found.sample_count > 0,
                spread=spread.confidence[found.confidence],
                rows_priced=count,
            )
        )
    return tuple(basis)
