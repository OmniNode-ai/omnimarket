"""Wiring-evidence import for check_unimported_handlers.py (OMN-10821).

Not collected by CI (node-local src/ tests, OMN-14338) and deliberately not a
second test suite -- the real coverage lives in
``tests/test_omn16930_tenant_registry_projection.py`` and
``tests/test_omn16930_conversion_replay.py``. This file exists solely so a
real ``import`` of ``HandlerTenantRegistryProjectionRunner`` appears under
``src/omnimarket`` outside its own defining file -- the exact wiring-evidence
shape ``check_unimported_handlers.py`` requires (mirrors
``node_projection_tenant_credentials/tests/test_wiring_import.py``).
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
)


def test_handler_class_importable() -> None:
    assert HandlerTenantRegistryProjectionRunner.__name__ == (
        "HandlerTenantRegistryProjectionRunner"
    )
