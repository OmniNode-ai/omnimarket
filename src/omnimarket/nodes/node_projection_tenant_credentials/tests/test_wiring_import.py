"""Wiring-evidence import for check_unimported_handlers.py (OMN-10821).

Not collected by CI (node-local src/ tests, OMN-14338) and deliberately not a
second test suite -- the real coverage lives in
``tests/test_omn16316_tenant_credentials_projection.py``,
``tests/nodes/node_projection_tenant_credentials/test_golden_chain_projection_tenant_credentials.py``,
and ``tests/test_omn16316_real_postgres_tenant_credentials_write_path.py``.
This file exists solely so a real ``import`` of
``HandlerTenantCredentialsProjectionRunner`` appears under ``src/omnimarket``
outside its own defining file -- the exact wiring-evidence shape
``check_unimported_handlers.py`` requires (mirrors
``node_projection_live_events/tests/test_handler_live_events.py``).
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)


def test_handler_class_importable() -> None:
    assert HandlerTenantCredentialsProjectionRunner.__name__ == (
        "HandlerTenantCredentialsProjectionRunner"
    )
