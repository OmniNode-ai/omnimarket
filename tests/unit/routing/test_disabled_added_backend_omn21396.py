# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rendered dark peer must not disable healthy delegation rungs.

Both the lane renderer and host-drift renderer represent ``serving: false``
as an explicit ``endpoint_url: null``. The overlay validator rejected this
for added backends, before any rung could be dispatched. Keep that backend
declared, but unreachable, exactly as for a dark base-declared backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    OverlayBackendIncompleteError,
    load_bifrost_delegation_config,
    load_bifrost_delegation_config_payload,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.delegation_backend_resolution import (
    BIFROST_OVERLAY_STORE_KEY,
    load_bifrost_backends,
)
from tests.unit.routing.test_bifrost_overlay_added_backend_validation_omn17099 import (
    _BIFROST_CONFIG_PATH,
    _complete_entry,
    _MockStore,
    _write_overlay,
)
from tests.unit.routing.test_inprocess_spread_omn19432 import (
    _port,
    _resolvable_bifrost_yaml,
)
from tests.unit.routing.test_same_model_spread_omn19215 import _bind, _reset

pytestmark = pytest.mark.unit


def test_file_loaders_keep_a_complete_dark_backend_declared(tmp_path: Path) -> None:
    overlay = _write_overlay(tmp_path, [_complete_entry(endpoint_url=None)])

    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay)
    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay
    )

    assert config.backends[-1].backend_id == "site-added-coder"
    assert config.backends[-1].endpoint_url is None
    assert backends[-1]["backend_id"] == "site-added-coder"
    assert backends[-1]["endpoint_url"] is None


def test_payload_and_store_loaders_agree_on_a_dark_backend() -> None:
    overlay = yaml.safe_dump({"backends": [_complete_entry(endpoint_url=None)]})
    config = load_bifrost_delegation_config_payload(
        _BIFROST_CONFIG_PATH.read_bytes(),
        overlay.encode(),
        contract_source="contract.yaml",
        overlay_source="overlay.yaml",
    )
    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH,
        store=_MockStore({BIFROST_OVERLAY_STORE_KEY: overlay}),
    )

    assert config.backends[-1].endpoint_url is None
    assert backends[-1]["endpoint_url"] is None


@pytest.mark.parametrize("task_type", ["document", "code_generation"])
def test_inprocess_ladder_routes_around_a_dark_spread_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_type: str
) -> None:
    base = yaml.safe_load(_resolvable_bifrost_yaml())
    peer = next(
        backend
        for backend in base["backends"]
        if backend["backend_id"] == "local-omnipc2-chat"
    )
    base["backends"].remove(peer)
    _bind(tmp_path, monkeypatch, yaml.safe_dump(base))
    peer.update(endpoint_url=None, secret_ref=None)
    overlay = _write_overlay(tmp_path, [peer])
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    try:
        expected = "local-heavy-reasoning" if task_type == "document" else "local-coder"
        port = _port(tmp_path)
        for key in range(20):
            resolved = port._resolve_initial_backend(task_type, spread_key=str(key))
            assert resolved.backend_id == expected
        assert "local-omnipc2-chat" not in routing._load_bifrost_endpoints()
    finally:
        _reset()


@pytest.mark.parametrize("bad_field", ["provider", "model_name", "timeout_ms"])
def test_dark_backend_still_requires_a_complete_declaration(
    tmp_path: Path, bad_field: str
) -> None:
    entry = _complete_entry(endpoint_url=None)
    del entry[bad_field]
    overlay = _write_overlay(tmp_path, [entry])

    with pytest.raises(OverlayBackendIncompleteError, match=bad_field):
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay)


def test_dark_backend_still_validates_its_model(tmp_path: Path) -> None:
    overlay = _write_overlay(
        tmp_path, [_complete_entry(endpoint_url=None, max_tokens=0)]
    )
    with pytest.raises(OverlayBackendIncompleteError) as refused:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay)
    assert any(
        field.startswith("max_tokens")
        for field in refused.value.missing_fields_by_backend["site-added-coder"]
    )
