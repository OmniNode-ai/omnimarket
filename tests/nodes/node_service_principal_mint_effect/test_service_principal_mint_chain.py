# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged contract of node_service_principal_mint_effect: golden run and error chain (OMN-24256).

The golden chain loads ``contract.yaml`` and asserts the shipped handler binding and the
typed ``handle`` hints, then mints over the in-memory adapters and reads both delivered
env files back. The error chain runs every refusal and watches the SENTINEL: it may
appear in no JSON, no repr and no log record of the run. A deliberately leaking
delivery proves the scan can fail.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

import pytest
import yaml
from pydantic import BaseModel, SecretStr

from omnimarket.nodes.node_service_principal_mint_effect.handlers.adapter_memory_service_principal import (
    MemoryBrokerLoginAdmin,
    MemoryCredentialDelivery,
    MemoryPrincipalVault,
)
from omnimarket.nodes.node_service_principal_mint_effect.handlers.handler_service_principal_mint import (
    HandlerServicePrincipalMint,
)
from omnimarket.nodes.node_service_principal_mint_effect.models.model_service_principal_mint import (
    ModelServicePrincipalDelivery,
    ModelServicePrincipalDeliveryReading,
    ModelServicePrincipalMintRequest,
    ModelServicePrincipalMintResult,
)
from tests.nodes.node_service_principal_mint_effect.test_handler_service_principal_mint import (
    SENTINEL,
    declaration,
    delivered_files,
    mint_handler,
    request,
)

pytestmark = pytest.mark.unit


def leaks(text: str) -> bool:
    """True when the sentinel secret the token source holds appears in ``text``."""
    return SENTINEL in text


def _mentions_secret(annotation: object) -> bool:
    """True when the annotation is, or contains anywhere, ``SecretStr``."""
    if annotation is SecretStr:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(_mentions_secret(arg) for arg in get_args(annotation))


async def test_contract_golden_chain() -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_service_principal_mint_effect"
    )
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == "node_service_principal_mint_effect"
    assert contract["metadata"]["related_tickets"] == ["OMN-24256"]

    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert handler_class is HandlerServicePrincipalMint
    assert "handler_routing" not in contract

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        resolved = getattr(importlib.import_module(module), name)
        assert issubclass(resolved, BaseModel)
        assert resolved.model_config == {"frozen": True, "extra": "forbid"}
        return resolved

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    assert contract["input_model"]["name"] == request_class.__name__
    assert contract["output_model"]["name"] == result_class.__name__
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class

    # A real run over the in-memory adapters, the sentinel standing in for the token.
    delivery = MemoryCredentialDelivery()
    result = await mint_handler(
        MemoryBrokerLoginAdmin(), MemoryPrincipalVault(), delivery
    ).handle(request())
    assert isinstance(result, result_class)
    assert result.outcome == "created"

    payload = result.model_dump_json()
    assert "svc-test" in payload
    assert not leaks(payload)
    assert not any(
        _mentions_secret(hint)
        for hint in get_type_hints(ModelServicePrincipalMintResult).values()
    )
    assert not any(
        _mentions_secret(hint)
        for hint in get_type_hints(ModelServicePrincipalDeliveryReading).values()
    )

    declared = declaration()
    expected = (
        f"{declared.username_env}={declared.name}\n{declared.password_env}={SENTINEL}\n"
    )
    files = delivered_files(delivery)
    assert set(files) == {
        (target.host, target.folder, target.filename) for target in declared.deliveries
    }
    assert all(file.get_secret_value() == expected for file in files.values())
    assert [(r.host, r.state, r.owner, r.mode) for r in result.deliveries] == [
        (target.host, "written", target.owner, target.mode)
        for target in declared.deliveries
    ]


async def test_the_error_chain_refuses_and_leaks_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    cases: list[
        tuple[
            str,
            ModelServicePrincipalMintRequest,
            MemoryBrokerLoginAdmin,
            MemoryPrincipalVault,
        ]
    ] = [
        # broker present + vault empty
        (
            "login_without_secret",
            request(),
            MemoryBrokerLoginAdmin(("svc-test",)),
            MemoryPrincipalVault(),
        ),
        # a principal the overlay does not declare
        (
            "undeclared",
            request(principal="ghost.test"),
            MemoryBrokerLoginAdmin(),
            MemoryPrincipalVault(),
        ),
        # the broker admin fails on list
        (
            "broker_unreachable",
            request(),
            MemoryBrokerLoginAdmin(fail_list=True),
            MemoryPrincipalVault(),
        ),
        # the vault fails on read
        (
            "vault_unavailable",
            request(),
            MemoryBrokerLoginAdmin(),
            MemoryPrincipalVault(unavailable=True),
        ),
    ]
    for refusal_kind, mint_request, admin, vault in cases:
        result = await mint_handler(admin, vault, MemoryCredentialDelivery()).handle(
            mint_request
        )

        payload = result.model_dump_json()
        assert json.loads(payload)["principal"] == mint_request.principal
        assert json.loads(payload)["refusal_kind"] == refusal_kind
        assert result.ok is False
        assert not leaks(payload)
        assert not leaks(repr(result))

    assert not leaks(caplog.text)


class _LeakingDelivery(MemoryCredentialDelivery):
    """Control: a delivery boundary that logs the content it was handed."""

    async def deliver(
        self,
        target: ModelServicePrincipalDelivery,
        content: SecretStr,
        *,
        apply: bool,
    ) -> ModelServicePrincipalDeliveryReading:
        logging.getLogger("x").info(
            "credential file for %s: %s", target.host, content.get_secret_value()
        )
        return await super().deliver(target, content, apply=apply)


async def test_control_a_leaking_delivery_makes_the_secret_scan_fail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await mint_handler(
        MemoryBrokerLoginAdmin(), MemoryPrincipalVault(), _LeakingDelivery()
    ).handle(request())

    assert result.ok
    assert leaks(caplog.text), (
        "control: the sentinel reached a log record, so the scan can fail"
    )
