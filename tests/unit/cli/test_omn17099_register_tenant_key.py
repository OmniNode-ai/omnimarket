# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant key intake uses the hosted publisher with this runtime's store/bus."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.events.topics import CREDENTIAL_REGISTERED_TOPIC_V1
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.projection.credential_publisher import (
    CredentialPlanUndeterminedError,
    CredentialStoreError,
    register_inference_credential,
)
from omnimarket.routing.byok_plan_detection import ModelByokPlanDetection
from omnimarket.routing.byok_provider_backends import (
    ByokPlanNotPermittedError,
    require_byok_plan_permitted,
)

pytestmark = pytest.mark.unit
_VALUE = "sk-test-tenant-secret-never-published"
_TENANT = "developer-tenant"


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, bytes | None]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        self.events.append((envelope.model_dump_json(), topic, key))


@pytest.fixture
def intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalByokCredentialStore, FakeBus]:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    bus = FakeBus()
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    return store, bus


@pytest.mark.parametrize("named", [False, True])
def test_registers_the_value_and_publishes_only_the_reference(
    intake: tuple[LocalByokCredentialStore, FakeBus], named: bool
) -> None:
    store, bus = intake
    args = ["register-tenant-key", "glm", "--tenant", _TENANT, "--plan", "general_api"]
    if named:
        args += ["--name", "my-provider"]
    result = CliRunner().invoke(cli_secret.secret_group, args, input=f"{_VALUE}\n")
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    ref = output["api_key_ref"]
    assert ref.startswith(f"cred_{_TENANT}_glm_")
    assert len(ref.rsplit("_", 1)[1]) == 32
    assert output == {
        "api_key_ref": ref,
        "provider": "glm",
        "plan": "general_api",
        "tenant": _TENANT,
    }
    assert asyncio.run(store.get_secret(ref)) == _VALUE
    assert _VALUE not in result.output
    assert len(bus.events) == 1
    event_json, topic, key = bus.events[0]
    assert _VALUE not in event_json
    assert topic == CREDENTIAL_REGISTERED_TOPIC_V1
    assert key == ref.encode()
    payload = json.loads(event_json)["payload"]
    assert payload["api_key_ref"] == ref
    assert payload["tenant_id"] == _TENANT
    assert payload["name"] == ("my-provider" if named else "glm")
    assert payload["metadata"] == {"plan": "general_api"}


def test_command_exposes_no_secret_value_or_lane_parameter() -> None:
    command = cli_secret.secret_group.commands["register-tenant-key"]
    assert {parameter.name for parameter in command.params} == {
        "provider",
        "tenant",
        "name",
        "plan",
        "model_option",
    }
    assert all(not getattr(parameter, "envvar", None) for parameter in command.params)


def test_omitted_plan_uses_the_shared_publishers_detection(
    intake: tuple[LocalByokCredentialStore, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def detect(provider: str, api_key: Any) -> ModelByokPlanDetection:
        calls.append(provider)
        assert api_key.get_secret_value() == _VALUE
        return ModelByokPlanDetection(
            provider=provider, plan="general_api", outcome="detected"
        )

    async def register(request: Any, **kwargs: Any) -> Any:
        return await register_inference_credential(
            request, plan_detector=detect, **kwargs
        )

    monkeypatch.setattr(cli_secret, "register_inference_credential", register)
    result = CliRunner().invoke(
        cli_secret.secret_group,
        ["register-tenant-key", "glm", "--tenant", _TENANT],
        input=f"{_VALUE}\n",
    )
    assert result.exit_code == 0, result.output
    assert calls == ["glm"]
    assert json.loads(result.output)["plan"] == "general_api"


def test_default_bus_is_constructed_and_owned_by_the_shared_publisher(
    intake: tuple[LocalByokCredentialStore, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, bus = intake
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: None)
    monkeypatch.setattr(
        "omnimarket.projection.credential_publisher._build_event_bus", lambda: bus
    )
    result = CliRunner().invoke(
        cli_secret.secret_group,
        # OMN-20844: the customer chooses the OpenRouter model their key runs.
        [
            "register-tenant-key",
            "openrouter",
            "--tenant",
            _TENANT,
            "--model",
            "openai/gpt-5-nano",
        ],
        input=f"{_VALUE}\n",
    )
    assert result.exit_code == 0, result.output
    assert bus.started
    assert bus.closed
    assert len(bus.events) == 1
    assert json.loads(result.output)["plan"] is None


@pytest.mark.parametrize("stdin", ["", " \n\t"])
def test_empty_stdin_is_refused(
    intake: tuple[LocalByokCredentialStore, FakeBus], stdin: str
) -> None:
    store, bus = intake
    result = CliRunner().invoke(
        cli_secret.secret_group,
        ["register-tenant-key", "openrouter", "--tenant", _TENANT],
        input=stdin,
    )
    assert result.exit_code != 0
    assert "no value" in result.output.lower()
    assert asyncio.run(store.list_keys()) == []
    assert bus.events == []


def test_provider_outside_customer_catalogue_is_refused(
    intake: tuple[LocalByokCredentialStore, FakeBus],
) -> None:
    store, bus = intake
    result = CliRunner().invoke(
        cli_secret.secret_group,
        ["register-tenant-key", "vertex", "--tenant", _TENANT],
        input=f"{_VALUE}\n",
    )
    assert result.exit_code != 0
    assert "not a provider" in result.output
    assert _VALUE not in result.output
    assert asyncio.run(store.list_keys()) == []
    assert bus.events == []


@pytest.mark.parametrize(
    "error",
    [
        CredentialStoreError("store unavailable"),
        CredentialPlanUndeterminedError("glm", "ambiguous", ("general_api",)),
    ],
)
def test_publisher_refusals_become_click_errors(
    intake: tuple[LocalByokCredentialStore, FakeBus],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(cli_secret, "register_inference_credential", refuse)
    result = CliRunner().invoke(
        cli_secret.secret_group,
        ["register-tenant-key", "glm", "--tenant", _TENANT, "--plan", "general_api"],
        input=f"{_VALUE}\n",
    )
    assert result.exit_code == 1
    assert result.output.strip() == f"Error: {error}"
    assert _VALUE not in result.output


def test_detection_only_plan_is_refused_before_storing(
    intake: tuple[LocalByokCredentialStore, FakeBus],
) -> None:
    store, bus = intake
    with pytest.raises(ByokPlanNotPermittedError) as refused:
        require_byok_plan_permitted("glm", "coding_plan")
    refusal = refused.value
    result = CliRunner().invoke(
        cli_secret.secret_group,
        ["register-tenant-key", "glm", "--tenant", _TENANT, "--plan", "coding_plan"],
        input=f"{_VALUE}\n",
    )
    assert result.exit_code == 1
    assert result.output.strip() == f"Error: {refusal}"
    assert _VALUE not in result.output
    assert asyncio.run(store.list_keys()) == []
    assert bus.events == []
