# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16200: a clean install resolves its shipped defaults and names what is missing.

The end-to-end proof is the ``Customer Clean-Install Delegate Gate`` CI job,
which installs this head's wheel into a fresh venv and delegates under an empty
environment (``scripts/ci/customer_clean_install_delegate_probe.py``). These
are the offline halves: the refusal that tells a customer with no model which
file to write, and the probe's own pieces.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    refuse_undeclared_local_model,
)

pytestmark = pytest.mark.unit

_HOUSE_REFS = frozenset({"llm.gemini.api_key", "llm.glm.api_key"})
_LOOPBACK = (
    "http://127.0.0.1:8000/v1/chat/completions"  # url-authority-ok: test loopback
)
_CUSTOMER = str(uuid4())


def _backend(
    *, backend_id: str = "cloud-gemini-flash", secret_ref: str | None
) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=backend_id,
        model_id="some-model",
        endpoint_ref="https://provider.test/v1/chat/completions",
        tier="cheap_cloud" if secret_ref else "local",
        max_tokens=1024,
        timeout_ms=30000,
        secret_ref=secret_ref,
    )


def _merged(*, local_endpoint: str | None) -> list[dict[str, object]]:
    return [
        {"backend_id": "local-coder", "tier": "local", "endpoint_url": local_endpoint},
        {"backend_id": "local-heavy-reasoning", "tier": "local", "endpoint_url": None},
        {
            "backend_id": "cloud-gemini-flash",
            "tier": "cheap_cloud",
            "endpoint_url": "https://provider.test/v1/chat/completions",
            "secret_ref": "llm.gemini.api_key",
        },
    ]


def test_a_customer_with_no_model_is_told_which_file_to_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.routing import delegation_backend_resolution as resolution_mod

    overlay = tmp_path / ".omninode" / "delegation" / "bifrost_overrides.yaml"
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", overlay)
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)

    with pytest.raises(ProtocolConfigurationError) as exc_info:
        refuse_undeclared_local_model(
            tenant_id=_CUSTOMER,
            backend=_backend(secret_ref="llm.gemini.api_key"),
            house_refs=_HOUSE_REFS,
            backends=_merged(local_endpoint=None),
        )

    message = str(exc_info.value)
    assert "No local model is declared" in message
    assert str(overlay) in message
    assert "local-coder" in message


def test_the_message_survives_the_consume_boundary_sanitizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The customer only ever sees the sanitized text, which collapses to a
    redaction marker on any credential-shaped word."""
    from omnibase_infra.utils import sanitize_error_message

    from omnimarket.routing import delegation_backend_resolution as resolution_mod

    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", tmp_path / "o.yaml")
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    with pytest.raises(ProtocolConfigurationError) as exc_info:
        refuse_undeclared_local_model(
            tenant_id=_CUSTOMER,
            backend=_backend(secret_ref="llm.gemini.api_key"),
            house_refs=_HOUSE_REFS,
            backends=_merged(local_endpoint=None),
        )

    assert "No local model is declared" in sanitize_error_message(exc_info.value)


@pytest.mark.parametrize(
    ("tenant_id", "secret_ref", "local_endpoint"),
    [
        # House work may fall through to a cloud rung; that is not a customer.
        (HOUSE_TENANT_SLUG, "llm.gemini.api_key", None),
        # A customer with a model bound escalated here; they declared one.
        (_CUSTOMER, "llm.gemini.api_key", _LOOPBACK),
        # A customer's own key (substituted by the local BYOK route) is routable.
        (_CUSTOMER, "cred_customer_owned", None),
        # A free local rung is the honest terminus.
        (_CUSTOMER, None, None),
    ],
)
def test_it_never_changes_an_outcome_the_terminus_would_allow(
    tenant_id: str, secret_ref: str | None, local_endpoint: str | None
) -> None:
    refuse_undeclared_local_model(
        tenant_id=tenant_id,
        backend=_backend(secret_ref=secret_ref),
        house_refs=_HOUSE_REFS,
        backends=_merged(local_endpoint=local_endpoint),
    )


def _load_probe() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "ci"
        / "customer_clean_install_delegate_probe.py"
    )
    spec = importlib.util.spec_from_file_location("omn16200_probe", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_probe_writes_the_customer_file_the_refusal_names() -> None:
    probe = _load_probe()
    overlay = yaml.safe_load(probe.customer_overlay_yaml(18741))

    assert {entry["backend_id"] for entry in overlay["backends"]} == {
        "local-coder",
        "local-heavy-reasoning",
    }
    for entry in overlay["backends"]:
        assert entry["model_name"] == probe.SERVED_MODEL
        assert entry["endpoint_url"].startswith("http://127.0.0.1:18741/")
        assert "secret_ref" not in entry


def test_the_probe_runs_under_no_workspace_or_config_binding() -> None:
    probe = _load_probe()

    assert {
        "OMNI_HOME",
        "DELEGATION_ROUTING_TIERS_PATH",
        "BIFROST_OVERLAY_PATH",
    } <= probe.FORBIDDEN_ENV_KEYS


def test_the_probe_refuses_to_run_without_an_installed_onex(tmp_path: Path) -> None:
    probe = _load_probe()

    with pytest.raises(probe.ProbeError):
        probe.probe(tmp_path / "bin" / "onex", timeout=5)
