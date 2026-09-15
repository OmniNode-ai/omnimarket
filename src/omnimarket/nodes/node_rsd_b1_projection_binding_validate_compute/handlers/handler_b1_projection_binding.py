"""Pure validation of supplied B1 projection-binding evidence."""

from typing import Literal

from omnimarket.nodes.node_rsd_b1_projection_binding_validate_compute.models.model_b1_projection_binding import (
    ModelB1ProjectionBindingValidationInput,
    ModelB1ProjectionBindingValidationOutput,
)
from omnimarket.rsd.b1_projection_binding import (
    parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json,
    parse_target_delivery_map_projection_binding_v1_canonical_json,
    validate_target_delivery_map_projection_binding_v1,
)
from omnimarket.rsd.historical_target_delivery_map_v1 import (
    parse_historical_target_delivery_map_v1_canonical_json,
)
from omnimarket.rsd.v4_static_profile import (
    parse_container_bootstrap_static_delivery_projection_v4_canonical_json,
    parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json,
)


def validate_b1_projection_binding(
    request: ModelB1ProjectionBindingValidationInput,
) -> ModelB1ProjectionBindingValidationOutput:
    """Verify only supplied historical evidence; no delivery authority is granted."""

    acceptance = validate_target_delivery_map_projection_binding_v1(
        delivery_map=parse_historical_target_delivery_map_v1_canonical_json(
            request.historical_target_delivery_map_canonical_json
        ),
        static_delivery_projection=parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
            request.static_delivery_projection_canonical_json
        ),
        profile_envelope=parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            request.profile_envelope_canonical_json
        ),
        binding=parse_target_delivery_map_projection_binding_v1_canonical_json(
            request.binding_canonical_json
        ),
        trust_policy=parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
            request.trust_policy_canonical_json
        ),
    )
    return ModelB1ProjectionBindingValidationOutput(
        target_delivery_map_sha256=acceptance.target_delivery_map_sha256,
        static_delivery_projection_sha256=acceptance.static_delivery_projection_sha256,
        binding_sha256=acceptance.binding_sha256,
        verification_context_sha256=acceptance.verification_context_sha256,
    )


class HandlerB1ProjectionBinding:
    """Effect-free COMPUTE handler for canonical B1 relation verification."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self, request: ModelB1ProjectionBindingValidationInput
    ) -> ModelB1ProjectionBindingValidationOutput:
        return validate_b1_projection_binding(request)
