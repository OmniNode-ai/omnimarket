# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision node takes the identity as input and never resets a user by accident (OMN-26467).

A new username, email and password key id create a new user and a new tenant; the overlay's protected usernames are refused
by name with no request sent; an existing user is refused unless the command carries ``reset_existing``; and with no flags the
overlay's own user is provisioned exactly as before. Every run goes through the fakes: nothing here touches a live host.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dev_identity_provision_effect.handlers import (
    HandlerDevIdentityProvision,
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    EnumDevIdentityOutcome,
    ModelDevIdentityProvisionResult,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)

from .fakes import FakeKeycloakAndApi, FakeKeyGenerator, FakeSettingStore

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_identity_provision_effect"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
OVERLAY_EXAMPLE = NODE_DIR / "overlay.example.yaml"

_R8_USER = "dev-probe-tenant-user"
_NEW_USER = "hermes-test-20261122"
_NEW_EMAIL = "hermes-test-20261122@example.invalid"
_NEW_KEY_ID = "hermes_test_20261122_password"
_NEW_KEY_NAME = "HERMES_TEST_20261122_PASSWORD"
_ADMIN_SECRET = "admin-secret-value"
_PASSWORD = "Dp1!generated"
_BOOTSTRAP_TOKEN = "bootstrap-token-xyz"


def _overlay(*, protected: list[str] | None = None) -> ModelDevIdentityProvisionOverlay:
    return ModelDevIdentityProvisionOverlay(
        lane="dev",
        keycloak={
            "base_url": "https://keycloak.example.invalid",
            "realm": "omninode",
            "timeout_seconds": 10.0,
            "token_path_template": "/realms/{realm}/protocol/openid-connect/token",
            "admin_users_path_template": "/admin/realms/{realm}/users",
            "admin_reset_password_path_template": "/admin/realms/{realm}/users/{user_id}/reset-password",
        },
        admin={
            "client_id": "admin-client",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_ADMIN_CLIENT_SECRET",
        },
        user={
            "username": _R8_USER,
            "email": "dev-probe-tenant@example.invalid",
            "first_name": "Dev",
            "last_name": "Probe",
            "client_id": "public-client",
            "password_key_id": "dev_probe_user_password",
            "password_folder": "/shared/env",
            "password_key": "DEV_PROBE_USER_PASSWORD",
        },
        api={
            "base_url": "https://api.example.invalid",
            "provision_path": "/v1/auth/provision",
            "timeout_seconds": 15.0,
        },
        protected_usernames=[_R8_USER] if protected is None else protected,
        secret_store=None,
    )


def _store() -> FakeSettingStore:
    store = FakeSettingStore()
    store.set("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET", _ADMIN_SECRET)
    store.set("/shared/env", "DEV_PROBE_USER_PASSWORD", _PASSWORD)
    store.set("/shared/env", _NEW_KEY_NAME, _PASSWORD)
    return store


def _run(
    command: ModelDevIdentityProvisionCommand,
    *,
    transport: FakeKeycloakAndApi,
    store: FakeSettingStore,
    gen: FakeKeyGenerator,
    overlay: ModelDevIdentityProvisionOverlay | None = None,
) -> ModelDevIdentityProvisionResult:
    handler = HandlerDevIdentityProvision(
        overlay=overlay or _overlay(),
        transport=transport,
        settings_store=store,
        key_generator=gen,
        contract_path=CONTRACT_PATH,
    )
    return asyncio.run(handler.handle(command))


def _writes(transport: FakeKeycloakAndApi) -> list[str]:
    """Every request that could change something: a PUT, or a POST that is not a token grant."""
    return [
        f"{r.method} {r.url}"
        for r in transport.sent
        if r.method == "PUT"
        or (r.method == "POST" and "/protocol/openid-connect/token" not in r.url)
    ]


def _existing_user_fake(username: str) -> FakeKeycloakAndApi:
    transport = FakeKeycloakAndApi(username=username, password=_PASSWORD)
    transport.users[username] = {"id": "user-existing", "username": username}
    return transport


def test_dev_identity_provision_new_user_creates_user_and_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloakAndApi(username=_NEW_USER, password=_PASSWORD)
    gen = FakeKeyGenerator(outcome="created", value=_PASSWORD)

    result = _run(
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username=_NEW_USER,
            email=_NEW_EMAIL,
            password_key_id=_NEW_KEY_ID,
        ),
        transport=transport,
        store=_store(),
        gen=gen,
    )

    assert result.ok is True
    assert result.outcome is EnumDevIdentityOutcome.PROVISIONED
    assert result.user_name == _NEW_USER
    assert result.tenant_id == "tenant-abc-123"
    assert result.tenant_slug == "probe-tenant"
    # The new user was created with the command's email, once, and the R8 user was never named on the wire.
    assert len(transport.create_bodies) == 1
    assert transport.create_bodies[0]["username"] == _NEW_USER
    assert transport.create_bodies[0]["email"] == _NEW_EMAIL
    assert list(transport.users) == [_NEW_USER]
    assert all(_R8_USER not in str(r) for r in transport.sent)
    # The password key id the command named is the one generated, and no password reaches the result or the logs.
    assert gen.calls == [_NEW_KEY_ID]
    for text in (result.model_dump_json(), repr(result), str(result), caplog.text):
        assert _PASSWORD not in text
        assert _BOOTSTRAP_TOKEN not in text


@pytest.mark.parametrize(
    "fields",
    [
        {"username": _NEW_USER},
        {"username": _NEW_USER, "email": _NEW_EMAIL},
        {"username": _NEW_USER, "password_key_id": _NEW_KEY_ID},
    ],
)
def test_dev_identity_provision_new_user_needs_its_own_email_and_password_key(
    fields: dict[str, str],
) -> None:
    transport = FakeKeycloakAndApi(username=_NEW_USER)
    gen = FakeKeyGenerator()

    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True, **fields),
        transport=transport,
        store=_store(),
        gen=gen,
    )

    assert result.ok is False
    assert result.outcome is EnumDevIdentityOutcome.INVALID_IDENTITY_INPUT
    assert transport.sent == []
    assert gen.calls == []


def test_dev_identity_provision_protected_user_is_refused_by_name_with_no_request() -> (
    None
):
    transport = FakeKeycloakAndApi(username=_R8_USER)
    store = _store()
    gen = FakeKeyGenerator(outcome="exists")

    # No flags: the overlay's own user is the protected one.
    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True),
        transport=transport,
        store=store,
        gen=gen,
    )

    assert result.ok is False
    assert result.outcome is EnumDevIdentityOutcome.USER_PROTECTED
    assert _R8_USER in result.detail
    assert result.user_name == _R8_USER
    assert transport.sent == []
    assert _writes(transport) == []
    assert gen.calls == []
    assert store.calls == []


def test_dev_identity_provision_protected_user_named_on_the_command_is_refused_too() -> (
    None
):
    transport = FakeKeycloakAndApi(username=_R8_USER.upper())
    gen = FakeKeyGenerator(outcome="exists")

    result = _run(
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username=_R8_USER.upper(),
            email=_NEW_EMAIL,
            password_key_id=_NEW_KEY_ID,
            reset_existing=True,
        ),
        transport=transport,
        store=_store(),
        gen=gen,
    )

    assert result.outcome is EnumDevIdentityOutcome.USER_PROTECTED
    assert transport.sent == []
    assert gen.calls == []


def test_dev_identity_provision_protected_user_with_the_explicit_flag_is_provisioned() -> (
    None
):
    transport = FakeKeycloakAndApi(username=_R8_USER)
    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True, allow_protected=True),
        transport=transport,
        store=_store(),
        gen=FakeKeyGenerator(outcome="exists"),
    )

    assert result.ok is True
    assert result.outcome is EnumDevIdentityOutcome.PROVISIONED
    assert result.user_name == _R8_USER
    assert len(transport.create_bodies) == 1


def test_dev_identity_provision_protected_probe_still_answers_ready() -> None:
    transport = FakeKeycloakAndApi()
    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=False),
        transport=transport,
        store=_store(),
        gen=FakeKeyGenerator(),
    )

    assert result.outcome is EnumDevIdentityOutcome.READY
    assert transport.sent == []


def test_dev_identity_provision_existing_refuses_without_reset_existing() -> None:
    transport = _existing_user_fake("hermes-existing")
    gen = FakeKeyGenerator(outcome="exists")

    result = _run(
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username="hermes-existing",
            email=_NEW_EMAIL,
            password_key_id=_NEW_KEY_ID,
        ),
        transport=transport,
        store=_store(),
        gen=gen,
    )

    assert result.ok is False
    assert result.outcome is EnumDevIdentityOutcome.USER_EXISTS_REFUSED
    assert "hermes-existing" in result.detail
    assert result.user_name == "hermes-existing"
    assert _writes(transport) == []
    assert transport.reset_bodies == []
    assert transport.create_bodies == []
    assert not any("/provision" in c for c in transport.call_log)
    # The key generator is create-only: asking it for a key never writes to Keycloak.
    assert gen.calls == [_NEW_KEY_ID]


def test_dev_identity_provision_default_command_refuses_an_existing_overlay_user() -> (
    None
):
    transport = _existing_user_fake("dev-probe-user")
    overlay = _overlay(protected=[])
    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True),
        transport=FakeKeycloakAndApi(username=_R8_USER),
        store=_store(),
        gen=FakeKeyGenerator(outcome="exists"),
        overlay=overlay,
    )
    assert result.outcome is EnumDevIdentityOutcome.PROVISIONED  # first run creates

    second_transport = _existing_user_fake(_R8_USER)
    second = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True),
        transport=second_transport,
        store=_store(),
        gen=FakeKeyGenerator(outcome="exists"),
        overlay=overlay,
    )
    assert second.outcome is EnumDevIdentityOutcome.USER_EXISTS_REFUSED
    assert _writes(second_transport) == []
    assert transport.reset_bodies == []


def test_dev_identity_provision_reset_existing_is_the_only_path_that_puts_a_reset() -> (
    None
):
    # A new user is created, never reset.
    created = FakeKeycloakAndApi(username=_NEW_USER, password=_PASSWORD)
    _run(
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username=_NEW_USER,
            email=_NEW_EMAIL,
            password_key_id=_NEW_KEY_ID,
        ),
        transport=created,
        store=_store(),
        gen=FakeKeyGenerator(),
    )
    assert not [r for r in created.sent if r.method == "PUT"]

    # An existing, non-protected user with reset_existing gets exactly one reset, then the tenant is bound.
    reset = _existing_user_fake("hermes-existing")
    result = _run(
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username="hermes-existing",
            email=_NEW_EMAIL,
            password_key_id=_NEW_KEY_ID,
            reset_existing=True,
        ),
        transport=reset,
        store=_store(),
        gen=FakeKeyGenerator(outcome="exists"),
    )
    assert result.outcome is EnumDevIdentityOutcome.PROVISIONED
    assert len([r for r in reset.sent if r.method == "PUT"]) == 1
    assert len(reset.reset_bodies) == 1
    assert reset.create_bodies == []


def test_dev_identity_provision_reset_existing_on_a_protected_user_needs_allow_protected() -> (
    None
):
    refused = _existing_user_fake(_R8_USER)
    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True, reset_existing=True),
        transport=refused,
        store=_store(),
        gen=FakeKeyGenerator(outcome="exists"),
    )
    assert result.outcome is EnumDevIdentityOutcome.USER_PROTECTED
    assert refused.sent == []

    allowed = _existing_user_fake(_R8_USER)
    result = _run(
        ModelDevIdentityProvisionCommand(
            lane="dev", write=True, reset_existing=True, allow_protected=True
        ),
        transport=allowed,
        store=_store(),
        gen=FakeKeyGenerator(outcome="exists"),
    )
    assert result.outcome is EnumDevIdentityOutcome.PROVISIONED
    assert len(allowed.reset_bodies) == 1


def test_dev_identity_provision_default_overlay_provisions_the_overlay_user() -> None:
    """No flags, the overlay's user not protected: the call order and bodies of the existing golden chain."""
    transport = FakeKeycloakAndApi(username="dev-probe-user", password=_PASSWORD)
    overlay = _overlay(protected=[])
    overlay = overlay.model_copy(
        update={"user": overlay.user.model_copy(update={"username": "dev-probe-user"})}
    )
    gen = FakeKeyGenerator(outcome="created", value=_PASSWORD)

    result = _run(
        ModelDevIdentityProvisionCommand(lane="dev", write=True),
        transport=transport,
        store=_store(),
        gen=gen,
        overlay=overlay,
    )

    assert result.ok is True
    assert result.outcome is EnumDevIdentityOutcome.PROVISIONED
    assert result.user_name == "dev-probe-user"
    assert gen.calls == ["dev_probe_user_password"]
    expected = [
        ("POST", "openid-connect/token"),
        ("GET", "/users"),
        ("POST", "/users"),
        ("POST", "openid-connect/token"),
        ("POST", "/provision"),
    ]
    assert len(transport.call_log) == len(expected)
    for call, (method, fragment) in zip(transport.call_log, expected, strict=True):
        assert call.startswith(method)
        assert fragment in call
    assert transport.create_bodies[0]["email"] == "dev-probe-tenant@example.invalid"


def test_dev_identity_provision_command_defaults_are_the_overlay_values_and_off() -> (
    None
):
    command = ModelDevIdentityProvisionCommand(lane="dev", write=True)
    assert command.username is None
    assert command.email is None
    assert command.password_key_id is None
    assert command.reset_existing is False
    assert command.allow_protected is False
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionCommand.model_validate(
            {"lane": "dev", "write": True, "tenant_name": "x"}
        )


_LAN_PREFIX = ".".join(("192", "168", ""))


def _lan_addresses(document: dict[str, object]) -> list[str]:
    """Every base_url in an overlay document that is a 192.168 LAN address (tailnet only, operator ruling 2026-11-11)."""
    found: list[str] = []
    for block in ("keycloak", "api"):
        section = document.get(block)
        assert isinstance(section, dict)
        base_url = str(section["base_url"])
        if _LAN_PREFIX in base_url:
            found.append(f"{block}.base_url={base_url}")
    return found


def test_dev_identity_provision_overlay_addresses_example_holds_no_lan_address() -> (
    None
):
    document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    assert _lan_addresses(document) == []
    # The example is also read the way the node loads it: through the overlay model, which now carries the protected list.
    overlay = ModelDevIdentityProvisionOverlay.model_validate(document)
    assert overlay.protected_usernames
    assert overlay.keycloak.base_url == document["keycloak"]["base_url"]
    assert overlay.api.base_url == document["api"]["base_url"]


def test_dev_identity_provision_overlay_addresses_refuses_a_192_168_address() -> None:
    document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    lan_url = "http://" + ".".join(("192", "168", "86", "201")) + ":28080"
    document["keycloak"]["base_url"] = lan_url
    assert _lan_addresses(document) == [f"keycloak.base_url={lan_url}"]


def test_dev_identity_provision_protected_usernames_are_declared_by_the_overlay() -> (
    None
):
    document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    overlay = ModelDevIdentityProvisionOverlay.model_validate(document)
    assert overlay.user.username in overlay.protected_usernames
    # A document that predates the list loads with no one protected; the overlays repository's own test holds the R8 user in it.
    del document["protected_usernames"]
    assert (
        ModelDevIdentityProvisionOverlay.model_validate(document).protected_usernames
        == []
    )
