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
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from functools import cache

import yaml
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
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.export_parsers import (
    ParsedPrompt,
    parse_export,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    EnumModelSource,
    EnumUnpricedReason,
    ModelPromptCostEstimateRequest,
    ModelPromptCostEstimateResult,
    ModelPromptCostRow,
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

    def __init__(self, baseline_resolver: BaselineResolver | None = None) -> None:
        self._resolve = (
            resolve_baseline if baseline_resolver is None else baseline_resolver
        )

    def handle(
        self, request: ModelPromptCostEstimateRequest
    ) -> ModelPromptCostEstimateResult:
        prompts = parse_export(request.export_format, request.content)
        baselines: dict[str, ModelCounterfactualBaseline | None] = {}

        def price(model: str) -> ModelCounterfactualBaseline | None:
            if model not in baselines:
                baselines[model] = self._resolve(model)
            return baselines[model]

        rows: list[ModelPromptCostRow] = []
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
            rows.append(self._row(index, prompt, model, source, baseline, price))

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
            actual_cost_usd=sum((row.actual_cost_usd for row in rows), _ZERO),
            delegated_cost_usd=sum((row.delegated_cost_usd for row in rows), _ZERO),
            saving_usd=sum((row.saving_usd for row in rows), _ZERO),
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
            tokens_estimated=prompt.tokens_estimated,
            actual_cost_usd=actual,
            recommended_model=recommended,
            recommended_tier=tier,
            delegated_cost_usd=delegated,
            saving_usd=actual - delegated,
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
        )
        for task_type, group in sorted(groups.items())
    )
