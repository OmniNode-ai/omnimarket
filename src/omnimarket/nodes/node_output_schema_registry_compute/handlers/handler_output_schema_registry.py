"""Output schema registry — maps schema keys to Pydantic model JSON schemas. Pure, deterministic, no I/O."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from omnimarket.nodes.node_output_schema_registry_compute.models.model_review_output import (
    ModelReviewOutput,
)
from omnimarket.nodes.node_output_schema_registry_compute.models.model_schema_registry_request import (
    ModelSchemaRegistryRequest,
)
from omnimarket.nodes.node_output_schema_registry_compute.models.model_schema_registry_result import (
    EnumSchemaRegistryStatus,
    ModelSchemaRegistryResult,
)

# Registry: schema_key -> Pydantic BaseModel subclass
_REGISTRY: dict[str, type[BaseModel]] = {}


def _register() -> None:
    # Deferred imports to avoid circular deps at module load time
    from omnibase_core.models.plan.model_plan_document import ModelPlanDocument

    _REGISTRY["review_output"] = ModelReviewOutput
    _REGISTRY["plan_document"] = ModelPlanDocument


_register()


def _get_schema(key: str) -> dict[str, Any] | None:
    model_cls = _REGISTRY.get(key)
    if model_cls is None:
        return None
    return model_cls.model_json_schema()


class HandlerOutputSchemaRegistry:
    """Resolve a schema_key to its model_json_schema(). Pure, deterministic, no I/O."""

    def handle(self, request: ModelSchemaRegistryRequest) -> ModelSchemaRegistryResult:
        schema = _get_schema(request.schema_key)
        if schema is None:
            return ModelSchemaRegistryResult(
                status=EnumSchemaRegistryStatus.ERROR,
                schema_key=request.schema_key,
                json_schema=None,
                error=f"Unknown schema key: {request.schema_key!r}. "
                f"Known keys: {sorted(_REGISTRY)}",
            )
        return ModelSchemaRegistryResult(
            status=EnumSchemaRegistryStatus.OK,
            schema_key=request.schema_key,
            json_schema=schema,
        )


def known_schema_keys() -> list[str]:
    """Return all registered schema keys, sorted."""
    return sorted(_REGISTRY)


__all__ = ["HandlerOutputSchemaRegistry", "known_schema_keys"]
