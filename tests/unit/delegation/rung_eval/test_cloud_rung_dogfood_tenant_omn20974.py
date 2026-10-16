# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cloud rung runs as the dogfood tenant, by reference (OMN-20974).

AC1: the cloud rung is a pinned ``onex delegate`` that resolves the provider key
from the tenant's registered reference at the effect boundary, so the eval holds
no provider key. Its row names the tenant that answered and the credential
reference the run used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date

import pytest

from omnimarket.delegation.rung_eval.cases import load_cases
from omnimarket.delegation.rung_eval.models import (
    EnumRungEvalStatus,
    ModelRungSpec,
)
from omnimarket.delegation.rung_eval.rungs import load_rungs
from omnimarket.delegation.rung_eval.runner import (
    OpenAIChatTransport,
    PinnedDelegateTransport,
    RungTransport,
    RungUnresolvedError,
    run_night,
)

pytestmark = pytest.mark.unit

_TENANT = "dogfood-tenant"
_REF = "cred_dogfood_openrouter_0123456789abcdef0123456789abcdef"
_ENV = {
    "CLOUD_BACKEND": "openrouter-nemotron-ultra",
    "CLOUD_TENANT": _TENANT,
}


def _rung() -> ModelRungSpec:
    return ModelRungSpec(
        rung_id="cloud-openrouter",
        kind="cloud",
        backend_id_env="CLOUD_BACKEND",
        tenant_env="CLOUD_TENANT",
    )


class _Delegate:
    """Stands in for the ``onex delegate`` subprocess; records what it was given."""

    def __init__(self, result: dict[str, object], code: int = 0) -> None:
        self.result, self.code = result, code
        self.argv: list[str] = []
        self.stdin: str = ""
        self.env: Mapping[str, str] = {}

    def __call__(
        self, argv: Sequence[str], stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        self.argv, self.stdin, self.env = list(argv), stdin, env
        body = json.dumps({"status": "success", "result": self.result})
        return self.code, f"{body}\n", ""


def _answer(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "response": "an answer",
        "model_name": "nvidia/nemotron",
        "tenant_id": _TENANT,
        "secret_ref": _REF,
        "metrics": {"latency_ms": 321},
    }
    return {**base, **over}


def _transport(delegate: _Delegate, env: dict[str, str] | None = None):
    return PinnedDelegateTransport(run=delegate, environ=_ENV if env is None else env)


def test_the_cloud_rung_is_configured_with_no_key_and_a_backend_pin() -> None:
    cloud = next(r for r in load_rungs() if r.kind == "cloud")
    assert cloud.api_key_env is None
    assert cloud.backend_id_env is not None
    assert cloud.tenant_env is not None


def test_it_pins_the_backend_and_runs_as_the_tenant_without_a_key() -> None:
    delegate = _Delegate(_answer())
    result = _transport(delegate).complete(_rung(), "classify these")
    assert delegate.argv[1:3] == ["delegate", "--json"]
    assert delegate.argv[delegate.argv.index("--backend-id") + 1] == (
        "openrouter-nemotron-ultra"
    )
    assert delegate.stdin == "classify these"
    assert delegate.env["ONEX_TENANT_ID"] == _TENANT
    assert not any("KEY" in name for name in delegate.env if name != "PATH")
    assert result.text == "an answer"
    assert result.model_id == "nvidia/nemotron"
    assert result.latency_ms == 321
    assert result.tenant_id == _TENANT
    assert result.credential_ref == _REF


def test_a_missing_pin_or_tenant_is_unresolved_not_run_as_someone_else() -> None:
    for missing in ("CLOUD_BACKEND", "CLOUD_TENANT"):
        env = {k: v for k, v in _ENV.items() if k != missing}
        with pytest.raises(RungUnresolvedError, match=missing):
            _transport(_Delegate(_answer()), env).complete(_rung(), "p")


def test_an_answer_from_another_tenant_is_refused() -> None:
    delegate = _Delegate(_answer(tenant_id="omninode"))
    with pytest.raises(ValueError, match="tenant"):
        _transport(delegate).complete(_rung(), "p")


def test_a_house_credential_is_refused() -> None:
    delegate = _Delegate(_answer(secret_ref="llm.openrouter.api_key"))
    with pytest.raises(ValueError, match="tenant credential"):
        _transport(delegate).complete(_rung(), "p")


def test_a_failed_delegation_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="exited with status 1"):
        _transport(_Delegate(_answer(), code=1)).complete(_rung(), "p")


def test_the_row_names_the_tenant_and_credential_reference() -> None:
    delegate = _Delegate(_answer())
    rows = run_night(
        date(2026, 10, 15), [_rung()], load_cases()[:1], _transport(delegate)
    )
    assert rows[0].tenant_id == _TENANT
    assert rows[0].credential_ref == _REF
    assert rows[0].status in {EnumRungEvalStatus.PASSED, EnumRungEvalStatus.FAILED}


def test_the_router_sends_a_pinned_rung_to_the_delegate_and_others_to_chat() -> None:
    delegate = _Delegate(_answer())
    router = RungTransport(_transport(delegate), OpenAIChatTransport())
    router.complete(_rung(), "p")
    assert delegate.argv
    local = ModelRungSpec(
        rung_id="lab-x", kind="local", base_url_env="NOPE_URL", model_env="NOPE_MODEL"
    )
    with pytest.raises(RungUnresolvedError, match="NOPE_URL"):
        router.complete(local, "p")
