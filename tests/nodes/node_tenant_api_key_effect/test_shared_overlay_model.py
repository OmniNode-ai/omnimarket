"""The node reads its overlay through the shared overlay-document model."""

from omnimarket.models.overlay_documents.model_tenant_api_key_overlay import (
    ModelTenantApiKeyOverlay as SharedOverlay,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers import (
    handler_tenant_api_key_register as register_module,
)


def test_handler_uses_shared_overlay_model() -> None:
    assert register_module.ModelTenantApiKeyOverlay is SharedOverlay


def test_node_models_define_no_overlay_of_their_own() -> None:
    from omnimarket.nodes.node_tenant_api_key_effect import models

    assert not hasattr(models, "ModelTenantApiKeyOverlay")
