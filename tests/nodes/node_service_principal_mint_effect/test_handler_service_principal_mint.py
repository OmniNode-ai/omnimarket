# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_service_principal_mint_effect: the mint matrix over the in-memory adapters (OMN-24256).

The real handler runs against an in-memory broker admin, vault and delivery, with an
injected constant token source holding the SENTINEL value, so every world in the mint
matrix is exact: what was created, what was refused and what the delivered env file
holds. The same handlers, fixtures and sentinel are imported by
``test_service_principal_mint_chain.py`` for the golden and error chains.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_service_principal_mint_effect.handlers.adapter_memory_service_principal import (
    MemoryBrokerLoginAdmin,
    MemoryCredentialDelivery,
    MemoryPrincipalVault,
)
from omnimarket.nodes.node_service_principal_mint_effect.handlers.handler_service_principal_mint import (
    HandlerServicePrincipalMint,
)
from omnimarket.nodes.node_service_principal_mint_effect.models.model_service_principal_mint import (
    ModelServicePrincipalBroker,
    ModelServicePrincipalDeclaration,
    ModelServicePrincipalDelivery,
    ModelServicePrincipalMintOverlay,
    ModelServicePrincipalMintRequest,
    ModelServicePrincipalMintResult,
)

pytestmark = pytest.mark.unit

# The one secret in this suite: the constant the injected token source returns.
SENTINEL = "SENTINEL-PASSWORD-VALUE"
# A password already in the vault, distinct from anything the token source returns.
STORED_PASSWORD = "stored-password-omn24256"


def broker_row() -> ModelServicePrincipalBroker:
    """The declared broker addressing; names only, never a credential."""
    return ModelServicePrincipalBroker(
        lane="lane-a",
        host="broker-host.test",
        compose_project="omni",
        broker_container="redpanda",
        superuser_source_container="redpanda",
        ssh_target="ops@broker-host.test",
        mechanism="SCRAM-SHA-256",
    )


def delivery_row(host: str) -> ModelServicePrincipalDelivery:
    """One credential-file delivery target on ``host``."""
    return ModelServicePrincipalDelivery(
        host=host,
        ssh_target=f"ops@{host}",
        folder=f"/srv/{host}/.config/onex",
        filename="svc-test.env",
        owner="svc",
        mode="600",
    )


def declaration() -> ModelServicePrincipalDeclaration:
    """The one declared principal: the sentinel vault key and two delivery hosts."""
    return ModelServicePrincipalDeclaration(
        name="svc-test",
        vault_folder="services",
        password_key="svc-test-password",
        password_bytes=32,
        username_env="SVC_TEST_USERNAME",
        password_env="SVC_TEST_PASSWORD",
        broker=broker_row(),
        deliveries=(delivery_row("host-a.test"), delivery_row("host-b.test")),
    )


def overlay() -> ModelServicePrincipalMintOverlay:
    """The overlay the whole suite runs on: one principal, two deliveries."""
    return ModelServicePrincipalMintOverlay(principals=(declaration(),))


def request(**changes: object) -> ModelServicePrincipalMintRequest:
    return ModelServicePrincipalMintRequest.model_validate(
        {"principal": "svc-test", **changes}
    )


def mint_handler(
    admin: MemoryBrokerLoginAdmin | None = None,
    vault: MemoryPrincipalVault | None = None,
    delivery: MemoryCredentialDelivery | None = None,
    overlay_obj: ModelServicePrincipalMintOverlay | None = None,
) -> HandlerServicePrincipalMint:
    """The real handler over the in-memory adapters, with the sentinel token source."""
    return HandlerServicePrincipalMint(
        overlay_obj if overlay_obj is not None else overlay(),
        admin if admin is not None else MemoryBrokerLoginAdmin(),
        vault if vault is not None else MemoryPrincipalVault(),
        delivery if delivery is not None else MemoryCredentialDelivery(),
        token_source=lambda _length: SENTINEL,
    )


async def seeded_vault(value: str = STORED_PASSWORD) -> MemoryPrincipalVault:
    """A vault that already holds the principal's password key."""
    declared = declaration()
    vault = MemoryPrincipalVault()
    await vault.create(declared.vault_folder, declared.password_key, SecretStr(value))
    return vault


def env_file_content(password: str) -> str:
    """The credential env file the handler must deliver, for the given password."""
    declared = declaration()
    return (
        f"{declared.username_env}={declared.name}\n{declared.password_env}={password}\n"
    )


def states(result: ModelServicePrincipalMintResult) -> dict[str, str]:
    return {reading.host: reading.state for reading in result.deliveries}


def vault_files(vault: MemoryPrincipalVault) -> dict[tuple[str, str], SecretStr]:
    return vault._peek_store()


def delivered_files(
    delivery: MemoryCredentialDelivery,
) -> dict[tuple[str, str, str], SecretStr]:
    return delivery._peek_files()


async def test_an_empty_world_creates_the_login_the_secret_and_both_files() -> None:
    admin = MemoryBrokerLoginAdmin()
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "created"
    assert result.broker_login == "created"
    assert result.secret == "created"
    assert result.ok
    # The broker admin got exactly one create_login call, with the token source's value.
    assert admin._peek_created() == [("svc-test", SecretStr(SENTINEL))]
    declared = declaration()
    assert vault_files(vault) == {
        (declared.vault_folder, declared.password_key): SecretStr(SENTINEL)
    }
    assert states(result) == {"host-a.test": "written", "host-b.test": "written"}
    assert all(
        file.get_secret_value() == env_file_content(SENTINEL)
        for file in delivered_files(delivery).values()
    )


async def test_a_second_run_of_the_same_request_changes_nothing() -> None:
    admin = MemoryBrokerLoginAdmin()
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery()
    handler = mint_handler(admin, vault, delivery)
    first = await handler.handle(request())
    assert first.outcome == "created"
    before = (
        admin._peek_created(),
        vault_files(vault),
        delivered_files(delivery),
    )

    second = await handler.handle(request())

    assert second.outcome == "exists"
    assert second.broker_login == "existed"
    assert second.secret == "existed"
    assert states(second) == {"host-a.test": "unchanged", "host-b.test": "unchanged"}
    assert len(admin._peek_created()) == 1, "no new create_login call"
    assert (admin._peek_created(), vault_files(vault), delivered_files(delivery)) == (
        before
    )


async def test_an_absent_login_with_a_stored_password_is_created_with_it() -> None:
    vault = await seeded_vault()
    admin = MemoryBrokerLoginAdmin()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "created"
    assert result.broker_login == "created"
    assert result.secret == "existed"
    # The stored password is the one used; a new token was never generated.
    assert admin._peek_created() == [("svc-test", SecretStr(STORED_PASSWORD))]
    declared = declaration()
    assert vault_files(vault) == {
        (declared.vault_folder, declared.password_key): SecretStr(STORED_PASSWORD)
    }
    assert all(
        file.get_secret_value() == env_file_content(STORED_PASSWORD)
        for file in delivered_files(delivery).values()
    )


async def test_a_login_without_a_stored_password_is_refused_untouched() -> None:
    admin = MemoryBrokerLoginAdmin(("svc-test",))
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "refused"
    assert result.refusal_kind == "login_without_secret"
    assert not result.ok
    assert admin._peek_created() == []
    assert vault_files(vault) == {}
    assert delivered_files(delivery) == {}
    assert result.deliveries == ()


async def test_an_existing_login_never_gets_its_password_replaced() -> None:
    admin = MemoryBrokerLoginAdmin(("svc-test",))
    vault = await seeded_vault()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "exists"
    assert result.broker_login == "existed"
    assert result.secret == "existed"
    assert admin._peek_created() == [], "a login's password is never replaced"
    assert all(
        file.get_secret_value() == env_file_content(STORED_PASSWORD)
        for file in delivered_files(delivery).values()
    )
    assert states(result) == {"host-a.test": "written", "host-b.test": "written"}


async def test_an_undeclared_principal_is_refused_with_nothing_touched() -> None:
    admin = MemoryBrokerLoginAdmin()
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(
        request(principal="ghost.test")
    )

    assert result.outcome == "refused"
    assert result.refusal_kind == "undeclared"
    assert result.broker_login == "absent"
    assert result.secret == "absent"
    assert result.deliveries == ()
    assert admin._peek_created() == []
    assert vault_files(vault) == {}
    assert delivered_files(delivery) == {}


async def test_an_unreachable_broker_is_refused_before_anything_is_created() -> None:
    admin = MemoryBrokerLoginAdmin(fail_list=True)
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "refused"
    assert result.refusal_kind == "broker_unreachable"
    assert result.broker_login == "absent"
    assert result.secret == "absent"
    assert admin._peek_created() == []
    assert vault_files(vault) == {}
    assert delivered_files(delivery) == {}


async def test_an_unreachable_vault_is_refused_with_no_login_created() -> None:
    admin = MemoryBrokerLoginAdmin()
    vault = MemoryPrincipalVault(unavailable=True)
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "refused"
    assert result.refusal_kind == "vault_unavailable"
    assert result.broker_login == "absent"
    assert result.secret == "absent"
    assert admin._peek_created() == []
    assert delivered_files(delivery) == {}


async def test_check_only_would_create_and_writes_nothing() -> None:
    admin = MemoryBrokerLoginAdmin()
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery()

    result = await mint_handler(admin, vault, delivery).handle(request(check_only=True))

    assert result.outcome == "would-create"
    assert result.broker_login == "would-create"
    assert result.secret == "would-create"
    assert result.ok
    assert states(result) == {
        "host-a.test": "would-write",
        "host-b.test": "would-write",
    }
    assert admin._peek_created() == []
    assert vault_files(vault) == {}
    assert delivered_files(delivery) == {}


async def test_one_failing_delivery_host_fails_the_run_and_not_the_other_host() -> None:
    admin = MemoryBrokerLoginAdmin()
    vault = MemoryPrincipalVault()
    delivery = MemoryCredentialDelivery(fail_hosts=("host-b.test",))

    result = await mint_handler(admin, vault, delivery).handle(request())

    assert result.outcome == "failed"
    assert result.refusal_kind == "delivery_failed"
    assert not result.ok
    assert result.broker_login == "created"
    assert result.secret == "created"
    assert states(result) == {"host-a.test": "written", "host-b.test": "failed"}
    assert len(admin._peek_created()) == 1, "the login itself was minted"


class _AdminWhoseListHidesExistingLogins(MemoryBrokerLoginAdmin):
    """Control: an admin whose ``list_logins`` lies, as a handler that skips the
    existence check would see the world of ``test_an_existing_login_never_gets_...``.
    """

    def __init__(self, logins: Iterable[str]) -> None:
        super().__init__(logins)
        self.create_calls: list[tuple[str, SecretStr]] = []

    async def list_logins(self) -> tuple[str, ...]:
        return ()

    async def create_login(self, name: str, password: SecretStr) -> None:
        self.create_calls.append((name, password))


async def test_control_a_handler_that_skipped_the_existence_check_would_create() -> (
    None
):
    """The same world case (e) guards, seen through an admin whose list hides the
    login: create_login then IS called -- exactly the call (e) asserts never happens,
    so (e) really catches a handler that skips the check.
    """
    admin = _AdminWhoseListHidesExistingLogins(("svc-test",))
    vault = await seeded_vault()

    result = await mint_handler(admin, vault, MemoryCredentialDelivery()).handle(
        request()
    )

    assert admin.create_calls == [("svc-test", SecretStr(STORED_PASSWORD))]
    assert result.outcome == "created", (
        "the run would report a mint that must not happen"
    )
