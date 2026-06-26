# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed parser and validator for the canonical model cost-pricing contract.

OMN-13621: routed from the SEA hackathon repo
(``onex-self-extending-agent/src/contracts/cost_pricing.py`` + ``cost_pricing.yaml``)
as the canonical contract-sourced pricing surface. Pricing data is declared in
``cost_pricing.yaml`` (a contract) and validated/loaded here — it is NEVER
hardcoded in handler source. The generation cost recorded in the canonical cost
projection is computed from this contract.

Uses the canonical omnimarket cost enums (``EnumCostBasis`` / ``EnumUsageSource``)
rather than redeclaring them, so the cost surface has a single source of truth.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource

COST_PRICING_CONTRACT_PATH = Path(__file__).with_name("cost_pricing.yaml")


class MissingCostPricingError(ValueError):
    """Raised when a caller requests a priced model without an explicit entry."""


class ModelCostPricingEntry(BaseModel):
    """One explicit model pricing entry.

    Prices are USD per token. UNKNOWN entries must keep price fields null so
    missing pricing is never accidentally interpreted as a free or zero-cost
    route.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    provider: str
    model_id: str
    input_token_price: Decimal | None = Field(default=None, ge=0)
    output_token_price: Decimal | None = Field(default=None, ge=0)
    currency: str
    provenance: str
    usage_source: EnumUsageSource
    cost_basis: EnumCostBasis

    @field_validator("provider", "model_id", "currency", "provenance")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cost pricing fields must be non-empty")
        return value

    @field_validator("currency")
    @classmethod
    def _currency_is_iso_code(cls, value: str) -> str:
        if value != value.upper() or len(value) != 3:
            raise ValueError("currency must be a three-letter uppercase ISO code")
        return value

    @model_validator(mode="after")
    def _validate_cost_basis(self) -> ModelCostPricingEntry:
        if self.cost_basis == EnumCostBasis.UNKNOWN:
            if (
                self.input_token_price is not None
                or self.output_token_price is not None
            ):
                raise ValueError("UNKNOWN cost basis must use null prices")
            if self.usage_source != EnumUsageSource.UNKNOWN:
                raise ValueError("UNKNOWN cost basis must use UNKNOWN usage_source")
            return self

        if self.input_token_price is None or self.output_token_price is None:
            raise ValueError(
                "priced cost entries require input and output token prices"
            )

        if self.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST:
            if self.input_token_price != Decimal(
                "0"
            ) or self.output_token_price != Decimal("0"):
                raise ValueError(
                    "ZERO_MARGINAL_API_COST entries must have zero token prices"
                )
            provenance = self.provenance.lower()
            if (
                "marginal api cost" not in provenance
                or "not total infra cost" not in provenance
            ):
                raise ValueError(
                    "ZERO_MARGINAL_API_COST provenance must document marginal API "
                    "cost and not total infra cost"
                )

        if (
            self.cost_basis == EnumCostBasis.CLOUD_API_COST
            and self.input_token_price == Decimal("0")
            and self.output_token_price == Decimal("0")
        ):
            raise ValueError(
                "CLOUD_API_COST entries cannot silently zero both token prices"
            )

        return self


class ModelCostPricingContract(BaseModel):
    """Validated model pricing manifest with a deterministic content hash."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: str
    price_unit: str = "USD_PER_TOKEN"
    entries: tuple[ModelCostPricingEntry, ...]

    @model_validator(mode="after")
    def _validate_contract(self) -> ModelCostPricingContract:
        if self.price_unit != "USD_PER_TOKEN":
            raise ValueError(
                "cost pricing contract currently supports only USD_PER_TOKEN"
            )
        keys = [(entry.provider, entry.model_id) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "cost pricing entries must be unique by provider and model_id"
            )
        if not any(
            entry.cost_basis == EnumCostBasis.CLOUD_API_COST for entry in self.entries
        ):
            raise ValueError(
                "cost pricing contract must include at least one cloud API cost entry"
            )
        if not any(
            entry.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST
            for entry in self.entries
        ):
            raise ValueError(
                "cost pricing contract must include at least one zero marginal API "
                "cost entry"
            )
        return self

    @property
    def cost_pricing_hash(self) -> str:
        payload = _canonical_pricing_payload(self)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _entry_to_hash_payload(entry: ModelCostPricingEntry) -> dict[str, Any]:
    data = entry.model_dump(mode="json")
    return {
        key: data[key]
        for key in (
            "provider",
            "model_id",
            "input_token_price",
            "output_token_price",
            "currency",
            "provenance",
            "usage_source",
            "cost_basis",
        )
    }


def _canonical_pricing_payload(contract: ModelCostPricingContract) -> dict[str, Any]:
    return {
        "schema_version": contract.schema_version,
        "price_unit": contract.price_unit,
        "entries": [_entry_to_hash_payload(entry) for entry in contract.entries],
    }


def load_cost_pricing(
    path: Path = COST_PRICING_CONTRACT_PATH,
) -> ModelCostPricingContract:
    """Load and validate a cost pricing contract from YAML."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("cost pricing YAML must parse to a mapping")
    return ModelCostPricingContract.model_validate(data)


def lookup_cost_pricing(
    contract: ModelCostPricingContract,
    provider: str,
    model_id: str,
    *,
    allow_unknown: bool = False,
) -> ModelCostPricingEntry:
    """Return the explicit pricing entry for provider/model_id.

    By default, missing pricing fails. Callers that can safely carry an explicit
    unknown cost may set ``allow_unknown=True``; the returned entry has null
    prices and UNKNOWN basis/source, never a silent zero.
    """
    for entry in contract.entries:
        if entry.provider == provider and entry.model_id == model_id:
            return entry

    if allow_unknown:
        return ModelCostPricingEntry(
            provider=provider,
            model_id=model_id,
            input_token_price=None,
            output_token_price=None,
            currency="USD",
            provenance="No matching cost_pricing.yaml entry; explicit UNKNOWN pricing.",
            usage_source=EnumUsageSource.UNKNOWN,
            cost_basis=EnumCostBasis.UNKNOWN,
        )

    raise MissingCostPricingError(
        f"missing cost pricing for provider={provider!r} model_id={model_id!r}"
    )


def calculate_inference_cost(
    entry: ModelCostPricingEntry, input_tokens: int, output_tokens: int
) -> Decimal:
    """Calculate inference cost from an explicit, priced entry."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if entry.cost_basis == EnumCostBasis.UNKNOWN:
        raise MissingCostPricingError("cannot calculate cost from UNKNOWN pricing")
    if entry.input_token_price is None or entry.output_token_price is None:
        raise MissingCostPricingError(
            "cannot calculate cost without explicit token prices"
        )
    return entry.input_token_price * Decimal(
        input_tokens
    ) + entry.output_token_price * Decimal(output_tokens)


def validate_cost_pricing(
    path: Path = COST_PRICING_CONTRACT_PATH,
) -> tuple[bool, tuple[str, ...]]:
    """Validate pricing YAML and return a small gate-friendly result."""
    try:
        load_cost_pricing(path)
    except (OSError, ValueError, ValidationError) as exc:
        return False, (str(exc),)
    return True, ()


__all__ = [
    "COST_PRICING_CONTRACT_PATH",
    "MissingCostPricingError",
    "ModelCostPricingContract",
    "ModelCostPricingEntry",
    "calculate_inference_cost",
    "load_cost_pricing",
    "lookup_cost_pricing",
    "validate_cost_pricing",
]
