# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23871: the routing overlay's local endpoints bind onto the contract's local backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

import omnimarket
from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    OverlayBackendIncompleteError,
    load_bifrost_delegation_config,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    _select_backend_by_id,
    load_bifrost_backends,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(omnimarket.__file__).resolve().parent / "configs" / "bifrost_delegation.yaml"
)
GPU_A = "http://gpu-a.example:8000/v1/chat/completions"
GPU_B = "http://gpu-b.example:8000/v1/chat/completions"
STALE_URL = "http://stale.example:8001/v1/chat/completions"
STALE_MODEL = "stale-model"
MODEL = "served-model-x"
_KEY = "DELEGATION_ROUTING_OVERLAY_PATH"


def _local_endpoints() -> list[dict[str, Any]]:
    return [
        {
            "backend_id": "local-coder",
            "provider": "local",
            "tier": "local",
            "endpoint_url": GPU_A,
            "model_name": MODEL,
        },
        {
            "backend_id": "local-heavy-reasoning",
            "provider": "local",
            "tier": "local",
            "endpoint_url": GPU_A,
            "model_name": MODEL,
        },
        {
            "backend_id": "local-second-chat",
            "provider": "local",
            "tier": "local",
            "endpoint_url": GPU_B,
            "model_name": MODEL,
            "timeout_ms": 300000,
            "max_tokens": 16384,
            "secret_ref": None,
            "capabilities": ["documentation"],
        },
    ]


def _overlay(local_endpoints: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"schema_version": "delegation_routing_overlay.v1"}
    if local_endpoints is not None:
        data["local_endpoints"] = {"backends": local_endpoints}
    return data


def _write_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_endpoints: list[dict[str, Any]] | None,
) -> Path:
    path = tmp_path / "routing_overlay.yaml"
    path.write_text(yaml.safe_dump(_overlay(local_endpoints)), encoding="utf-8")
    monkeypatch.setenv(_KEY, str(path))
    return path


@pytest.fixture
def bound_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "routing_overlay.yaml"
    path.write_text(yaml.safe_dump(_overlay(_local_endpoints())), encoding="utf-8")
    monkeypatch.setenv(_KEY, str(path))
    return path


def _backends(tmp_path: Path) -> list[dict[str, Any]]:
    return load_bifrost_backends(
        config_path=CONTRACT, overlay_path=tmp_path / "no_bifrost_overlay.yaml"
    )


def _config(tmp_path: Path) -> Any:
    return load_bifrost_delegation_config(
        config_path=CONTRACT, overlay_path=tmp_path / "no_bifrost_overlay.yaml"
    )


def _contract_row(backend_id: str) -> dict[str, Any]:
    rows = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["backends"]
    return next(b for b in rows if b["backend_id"] == backend_id)


def test_failing_control_no_declaration_leaves_the_local_rung_unbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no overlay bound, the local rung keeps the contract's null endpoint and model."""
    monkeypatch.delenv(_KEY, raising=False)
    row = next(b for b in _backends(tmp_path) if b["backend_id"] == "local-coder")
    assert row["endpoint_url"] is None
    assert row["model_name"] is None


@pytest.mark.usefixtures("bound_overlay")
def test_a_declared_local_endpoint_binds_onto_the_contract_backend(
    tmp_path: Path,
) -> None:
    """A declared local endpoint sets the contract backend's endpoint and model, keeping its own keys."""
    backends = _backends(tmp_path)
    row = next(b for b in backends if b["backend_id"] == "local-coder")
    assert row["endpoint_url"] == GPU_A
    assert row["model_name"] == MODEL
    contract = _contract_row("local-coder")
    for key in (
        "timeout_ms",
        "max_tokens",
        "capabilities",
        "inline_reasoning_terminator",
    ):
        assert row[key] == contract[key]
    picked = _select_backend_by_id(backends, "local-coder")
    assert picked is not None
    assert picked["endpoint_url"] == GPU_A
    assert picked["model_name"] == MODEL
    config = _config(tmp_path)
    bound = next(b for b in config.backends if b.backend_id == "local-coder")
    assert bound.endpoint_url == GPU_A
    assert bound.model_name == MODEL


@pytest.mark.usefixtures("bound_overlay")
def test_a_declared_new_local_backend_is_added_complete(tmp_path: Path) -> None:
    """A declared local endpoint with a new id is added with its declared endpoint and model."""
    row = next(b for b in _backends(tmp_path) if b["backend_id"] == "local-second-chat")
    assert row["endpoint_url"] == GPU_B
    assert row["model_name"] == MODEL
    assert row["timeout_ms"] == 300000
    assert row["max_tokens"] == 16384


def test_the_overlay_wins_over_a_stale_machine_local_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing overlay's local endpoint wins over a stale machine-local bifrost overlay."""
    stale = tmp_path / "bifrost_overrides.yaml"
    stale.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": "local-coder",
                        "endpoint_url": STALE_URL,
                        "model_name": STALE_MODEL,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_overlay(tmp_path, monkeypatch, _local_endpoints())
    row = next(b for b in _backends(tmp_path) if b["backend_id"] == "local-coder")
    assert row["endpoint_url"] == GPU_A
    assert row["model_name"] == MODEL


def test_an_absent_section_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay with only schema_version changes nothing over an unbound overlay."""
    monkeypatch.delenv(_KEY, raising=False)
    unbound = _backends(tmp_path)
    _write_overlay(tmp_path, monkeypatch, None)
    assert _backends(tmp_path) == unbound


def test_a_declared_section_leaves_every_other_backend_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declaration for one local backend leaves the other contract backends as they were."""
    monkeypatch.delenv(_KEY, raising=False)
    unbound = {b["backend_id"]: b for b in _backends(tmp_path)}
    _write_overlay(tmp_path, monkeypatch, _local_endpoints_one())
    bound = {b["backend_id"]: b for b in _backends(tmp_path)}
    assert set(bound) == set(unbound)
    for backend_id, row in unbound.items():
        if backend_id != "local-coder":
            assert bound[backend_id] == row


def _local_endpoints_one(**overrides: Any) -> list[dict[str, Any]]:
    row: dict[str, Any] = {
        "backend_id": "local-coder",
        "provider": "local",
        "tier": "local",
        "endpoint_url": GPU_A,
        "model_name": MODEL,
    }
    row.update(overrides)
    return [row]


def test_a_local_endpoint_missing_its_model_name_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local endpoint without a model_name refuses, naming the id and the key."""
    _write_overlay(tmp_path, monkeypatch, _local_endpoints_one(model_name=None))
    with pytest.raises(ProtocolConfigurationError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "local-coder" in msg
    assert "needs model_name" in msg
    assert GPU_A not in msg


def test_a_local_endpoint_missing_its_endpoint_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local endpoint without an endpoint_url refuses, naming the id and the key."""
    _write_overlay(tmp_path, monkeypatch, _local_endpoints_one(endpoint_url=None))
    with pytest.raises(ProtocolConfigurationError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "local-coder" in msg
    assert "needs endpoint_url" in msg
    assert MODEL not in msg


def test_a_local_endpoint_naming_a_credential_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local GPU endpoint has no provider key: a row naming a credential refuses, naming the id."""
    _write_overlay(
        tmp_path, monkeypatch, _local_endpoints_one(secret_ref="llm.hosted.api_key")
    )
    with pytest.raises(ProtocolConfigurationError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "local-coder" in msg
    assert "must not name a credential" in msg
    assert "llm.hosted.api_key" not in msg
    assert GPU_A not in msg


def test_a_local_endpoint_pinned_only_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local endpoint that is explicit_pin_only refuses, naming the id."""
    _write_overlay(tmp_path, monkeypatch, _local_endpoints_one(explicit_pin_only=True))
    with pytest.raises(ProtocolConfigurationError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "local-coder" in msg
    assert "must not be explicit_pin_only" in msg
    assert GPU_A not in msg


def test_two_local_endpoints_with_one_id_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two local endpoints sharing a backend_id refuse, naming the id."""
    rows = _local_endpoints_one()
    rows.append(dict(rows[0]))
    _write_overlay(tmp_path, monkeypatch, rows)
    with pytest.raises(ProtocolConfigurationError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "local-coder" in msg
    assert "duplicate local endpoint backend_id" in msg
    assert GPU_A not in msg


def test_a_local_endpoint_reusing_a_harness_backend_id_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local endpoint reusing a harness backend_id declared in the same overlay refuses."""
    data = _overlay(_local_endpoints_one(backend_id="harness-arm-a"))
    data["harness_backends"] = [
        {
            "backend_id": "harness-arm-a",
            "provider": "hosted",
            "kind": "harness",
            "harness": "claude",
            "surface": "internal",
            "tenant_scope": "house",
            "explicit_pin_only": True,
            "model_name": "vendor/model-a",
            "tier": "harness",
            "timeout_ms": 300000,
            "max_tokens": 32768,
            "capabilities": ["document"],
        }
    ]
    path = tmp_path / "routing_overlay.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv(_KEY, str(path))
    with pytest.raises(ProtocolConfigurationError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "harness-arm-a" in msg
    assert "reuses a harness, eval or paid backend_id" in msg
    assert GPU_A not in msg


def test_a_new_local_backend_missing_its_timeout_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new local backend missing timeout_ms refuses, naming the id and the key."""
    row = {
        "backend_id": "local-second-chat",
        "provider": "local",
        "tier": "local",
        "endpoint_url": GPU_B,
        "model_name": MODEL,
        "max_tokens": 16384,
        "secret_ref": None,
        "capabilities": ["documentation"],
    }
    _write_overlay(tmp_path, monkeypatch, [row])
    with pytest.raises(OverlayBackendIncompleteError) as exc:
        _backends(tmp_path)
    msg = str(exc.value)
    assert "local-second-chat" in msg
    assert "timeout_ms" in msg
    assert GPU_B not in msg
