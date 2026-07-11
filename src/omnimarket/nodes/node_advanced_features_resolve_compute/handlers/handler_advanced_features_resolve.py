"""Thin COMPUTE handler over the shared advanced-features resolver."""

from __future__ import annotations

from omnimarket.contract_assembly.advanced_features import resolve_advanced_features
from omnimarket.contract_assembly.models import (
    ModelAdvancedFeatures,
    ModelAdvancedFeaturesRequest,
)


class HandlerAdvancedFeaturesResolve:
    """Resolve the archetype-differentiated advanced-features block with overrides."""

    def handle(self, payload: ModelAdvancedFeaturesRequest) -> ModelAdvancedFeatures:
        return resolve_advanced_features(payload)


__all__ = ["HandlerAdvancedFeaturesResolve"]
