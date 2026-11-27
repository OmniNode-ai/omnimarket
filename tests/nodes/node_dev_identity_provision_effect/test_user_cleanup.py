# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run deletes the dev user it created when it ends, and the node can read and delete a named dev user (OMN-23563).

A probe that created a Keycloak user and then ended in ``runtime_unreachable`` left the user in the dev realm with no
tenant and nothing that removed it. A run now deletes the user it created on every typed failure after the create, and
on success only when the command declares the user disposable. A user that existed before the run, and a protected
username, is never deleted. The ``lookup`` and ``delete`` verbs read and remove one named user through the same dev-only
path, and a delete is read back gone.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dev_identity_provision_effect.handlers import (
    HandlerDevIdentityProvision,
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)

from .fakes import (
    FakeKeycloakAndApi,
    FakeKeyGenerator,
    FakeSettingStore,
    FakeTenantProvisioner,
)
from .test_golden_chain_dev_identity_provision import CONTRACT_PATH, _overlay, _seed

pytestmark = pytest.mark.unit

USER = "integm-probe-c"
IDENTITY = {
    "username": USER,
    "email": "integm-probe-c@example.invalid",
    "password_key_id": "integm_probe_c_password",
}


def _handler(
    transport: FakeKeycloakAndApi,
    provisioner: FakeTenantProvisioner | None = None,
    overlay: ModelDevIdentityProvisionOverlay | None = None,
) -> HandlerDevIdentityProvision:
    store = FakeSettingStore()
    _seed(store)
    store.set("/shared/env", "INTEGM_PROBE_C_PASSWORD", "Dp1!generated")
    return HandlerDevIdentityProvision(
        overlay=overlay or _overlay(),
        transport=transport,
        settings_store=store,
        key_generator=FakeKeyGenerator(outcome="created"),
        tenant_provisioner=provisioner or FakeTenantProvisioner(),
        contract_path=CONTRACT_PATH,
    )


def _transport(**fields: Any) -> FakeKeycloakAndApi:
    return FakeKeycloakAndApi(username=USER, **fields)


def _run(handler: HandlerDevIdentityProvision, **fields: Any) -> Any:
    command = ModelDevIdentityProvisionCommand(
        lane="dev", write=True, **{**IDENTITY, **fields}
    )
    return asyncio.run(handler.handle(command))


def _verb(handler: HandlerDevIdentityProvision, verb: str, username: str) -> Any:
    command = ModelDevIdentityProvisionCommand(
        lane="dev",
        write=verb == "delete",
        username=username,
        lookup_user=verb == "lookup",
        delete_user=verb == "delete",
    )
    return asyncio.run(handler.handle(command))


# --- cleanup on exit -------------------------------------------------------------------------------------------------


def test_a_run_that_ends_runtime_unreachable_leaves_no_user_in_the_realm() -> None:
    transport = _transport()
    provisioner = FakeTenantProvisioner(unavailable="no answer in 30s")

    result = _run(_handler(transport, provisioner))

    assert result.outcome.value == "runtime_unreachable"
    assert USER not in transport.users
    assert len(transport.deleted_ids) == 1
    assert result.cleanup is not None
    assert result.cleanup.value == "deleted"


def test_a_refused_provision_leaves_no_user_in_the_realm() -> None:
    transport = _transport()

    result = _run(_handler(transport, FakeTenantProvisioner(outcome="slug_taken")))

    assert result.outcome.value == "provision_refused"
    assert USER not in transport.users


def test_an_invalid_runtime_answer_leaves_no_user_in_the_realm() -> None:
    transport = _transport()

    result = _run(_handler(transport, FakeTenantProvisioner(tenant_id=None)))

    assert result.outcome.value == "provision_response_invalid"
    assert USER not in transport.users


def test_a_refused_sign_in_leaves_no_user_in_the_realm() -> None:
    transport = _transport(fail_user_token=True)

    result = _run(_handler(transport))

    assert result.outcome.value == "user_token_refused"
    assert USER not in transport.users


def test_an_exception_after_the_create_still_deletes_the_user_and_is_raised_again() -> (
    None
):
    transport = _transport()

    class BoomError(Exception):
        pass

    class ExplodingProvisioner(FakeTenantProvisioner):
        async def provision(self, request: Any) -> Any:
            raise BoomError

    with pytest.raises(BoomError):
        _run(_handler(transport, ExplodingProvisioner()))

    assert USER not in transport.users


def test_a_successful_run_keeps_the_user_unless_the_command_declares_it_disposable() -> (
    None
):
    kept = _transport()
    kept_result = _run(_handler(kept))
    assert kept_result.outcome.value == "provisioned"
    assert USER in kept.users
    assert kept_result.cleanup is not None
    assert kept_result.cleanup.value == "kept"

    disposable = _transport()
    gone_result = _run(_handler(disposable), disposable=True)
    assert gone_result.outcome.value == "provisioned"
    assert USER not in disposable.users
    assert gone_result.cleanup is not None
    assert gone_result.cleanup.value == "deleted"


def test_a_user_that_existed_before_the_run_is_never_deleted() -> None:
    transport = _transport()
    transport.users[USER] = {"id": "user-preexisting", "username": USER}
    provisioner = FakeTenantProvisioner(unavailable="no answer in 30s")

    result = _run(
        _handler(transport, provisioner), reset_existing=True, disposable=True
    )

    assert result.outcome.value == "runtime_unreachable"
    assert USER in transport.users
    assert transport.deleted_ids == []
    assert result.cleanup is not None
    assert result.cleanup.value == "not_created"


def test_a_refused_existing_user_is_never_deleted() -> None:
    transport = _transport()
    transport.users[USER] = {"id": "user-preexisting", "username": USER}

    result = _run(_handler(transport), disposable=True)

    assert result.outcome.value == "user_exists_refused"
    assert USER in transport.users
    assert transport.deleted_ids == []


def test_a_protected_username_the_run_created_is_never_deleted() -> None:
    document = _overlay().model_dump(mode="json")
    document["protected_usernames"] = [USER]
    protected = ModelDevIdentityProvisionOverlay.model_validate(document)
    transport = _transport()
    provisioner = FakeTenantProvisioner(unavailable="no answer in 30s")

    result = _run(
        _handler(transport, provisioner, protected),
        allow_protected=True,
        disposable=True,
    )

    assert result.outcome.value == "runtime_unreachable"
    assert USER in transport.users
    assert transport.deleted_ids == []
    assert result.cleanup is not None
    assert result.cleanup.value == "kept_protected"


def test_a_cleanup_that_fails_is_a_typed_result_never_a_silent_leftover() -> None:
    transport = _transport(fail_user_delete=True)
    provisioner = FakeTenantProvisioner(unavailable="no answer in 30s")

    result = _run(_handler(transport, provisioner))

    assert result.outcome.value == "runtime_unreachable"
    assert result.cleanup is not None
    assert result.cleanup.value == "delete_failed"
    assert USER in transport.users


def test_a_failure_before_the_create_has_nothing_to_clean_up() -> None:
    transport = _transport(fail_user_create=True)

    result = _run(_handler(transport))

    assert result.outcome.value == "user_create_failed"
    assert transport.deleted_ids == []
    assert result.cleanup is not None
    assert result.cleanup.value == "not_created"


def test_the_cleanup_result_carries_names_and_ids_never_a_secret() -> None:
    transport = _transport()
    result = _run(
        _handler(transport, FakeTenantProvisioner(unavailable="no answer in 30s"))
    )
    for text in (result.model_dump_json(), repr(result), str(result)):
        for secret in ("Dp1!generated", "admin-secret-value", "admin-access-token"):
            assert secret not in text


# --- the lookup and delete verbs -------------------------------------------------------------------------------------


def test_lookup_reads_a_present_user_and_an_absent_one_and_writes_nothing() -> None:
    transport = _transport()
    transport.users[USER] = {"id": "user-9", "username": USER}
    handler = _handler(transport)

    found = _verb(handler, "lookup", USER)
    absent = _verb(handler, "lookup", "integm-probe-d")

    assert found.outcome.value == "user_found"
    assert found.user_id == "user-9"
    assert found.ok is True
    assert absent.outcome.value == "user_absent"
    assert absent.ok is True
    assert [r.method for r in transport.sent if "/admin/" in r.url] == ["GET", "GET"]
    assert USER in transport.users


def test_delete_removes_only_the_named_user_and_reads_it_gone() -> None:
    transport = _transport()
    transport.users[USER] = {"id": "user-9", "username": USER}
    transport.users["integm-probe-d"] = {"id": "user-10", "username": "integm-probe-d"}
    handler = _handler(transport)

    deleted = _verb(handler, "delete", USER)
    gone = _verb(handler, "lookup", USER)

    assert deleted.outcome.value == "user_deleted"
    assert deleted.ok is True
    assert deleted.user_id == "user-9"
    assert transport.deleted_ids == ["user-9"]
    assert "integm-probe-d" in transport.users
    assert gone.outcome.value == "user_absent"


def test_delete_of_a_user_that_never_existed_deletes_nothing_and_says_absent() -> None:
    transport = _transport()

    result = _verb(_handler(transport), "delete", "integm-probe-d")

    assert result.outcome.value == "user_absent"
    assert result.ok is True
    assert transport.deleted_ids == []


def test_delete_refuses_the_overlay_user_and_a_protected_username() -> None:
    document = _overlay().model_dump(mode="json")
    document["protected_usernames"] = ["Some-Protected"]
    protected = ModelDevIdentityProvisionOverlay.model_validate(document)
    transport = _transport()
    transport.users["dev-probe-user"] = {"id": "user-1", "username": "dev-probe-user"}
    transport.users["Some-Protected"] = {"id": "user-2", "username": "Some-Protected"}
    handler = _handler(transport, overlay=protected)

    for name in ("dev-probe-user", "some-protected"):
        result = _verb(handler, "delete", name)
        assert result.outcome.value == "user_protected"
        assert result.ok is False

    assert transport.deleted_ids == []
    assert transport.sent == []


def test_a_delete_that_keycloak_refuses_is_typed_and_the_user_stays() -> None:
    transport = _transport(fail_user_delete=True)
    transport.users[USER] = {"id": "user-9", "username": USER}

    result = _verb(_handler(transport), "delete", USER)

    assert result.outcome.value == "user_delete_failed"
    assert result.ok is False
    assert USER in transport.users


def test_lookup_and_delete_are_refused_outside_the_dev_lane_before_any_call() -> None:
    transport = _transport()
    handler = _handler(transport)
    for verb in ("lookup", "delete"):
        command = ModelDevIdentityProvisionCommand(
            lane="prod",
            write=verb == "delete",
            username=USER,
            lookup_user=verb == "lookup",
            delete_user=verb == "delete",
        )
        result = asyncio.run(handler.handle(command))
        assert result.outcome.value == "refused_not_dev"
    assert transport.sent == []


def test_lookup_and_delete_need_a_username_and_exclude_each_other() -> None:
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionCommand(lane="dev", write=False, lookup_user=True)
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionCommand(lane="dev", write=True, delete_user=True)
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username=USER,
            lookup_user=True,
            delete_user=True,
        )
