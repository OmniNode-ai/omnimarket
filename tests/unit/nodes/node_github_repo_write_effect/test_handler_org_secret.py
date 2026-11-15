# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""org_secret_set on a recorded fake transport, a fake store and a synthetic key pair.

No real store name, no real organization and no real call: the key pair is generated
here, the value is a sentinel and the transport is a fake. The tests pin the contract
of the operation:

- AC1: an allowlisted name reads the store by its declared reference, seals the value
  to the organization's key and sends one PUT with the key id and the declared
  ``selected`` visibility and repository ids; decrypting the captured
  ``encrypted_value`` with the test key pair returns the sentinel.
- AC2: a name the overlay does not list is refused ``org_secret_not_allowed`` with zero
  store reads and zero transport calls, in either mode.
- AC3: the sentinel, its base64 form and its sealed form appear in no log record, result,
  exception text or chained cause, on success and on every refusal and failure path.
- AC4: the repository-variable path still refuses a secret-shaped name, whatever the
  org secret allowlist says.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from nacl.public import PrivateKey, SealedBox
from pydantic import SecretStr, ValidationError

from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.inference.secret_store_resolver import injected_secret_store
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelOrgSecretSetEntry,
    ModelOrgSecretSetsOverlay,
    ModelPrLabelOverlay,
    ModelRepoVariablesEntry,
    ModelRepoVariablesOverlay,
    ModelRepoWriteOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.org_secret import (
    SEALED_PLACEHOLDER,
    OrgSecretSourceError,
    read_org_secret_value,
    seal_value,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_github_repo_write_effect"
)
_ORG = "example-org"
_REPO = "example-org/example-repo"
_REPO_ID = 424242
_KEY_ID = "568250167242549743"
_CORRELATION = UUID("00000000-0000-4000-8000-000000000024")
_NOW = 1_791_000_000.0
_NAME_USER = "KAFKA_SASL_USERNAME"
_NAME_PASSWORD = "KAFKA_SASL_PASSWORD"
_REF = {
    _NAME_USER: "example/kafka-sasl-username",
    _NAME_PASSWORD: "example/kafka-sasl-password",
}
_SENTINEL = "sentinel-Zq7-lab-sasl-9f31c2d8"
_PUBLIC_KEY_PATH = f"/orgs/{_ORG}/actions/secrets/public-key"
_REPO_PATH = f"/repos/{_REPO}"

_PRIVATE = PrivateKey.generate()
_PUBLIC_B64 = base64.b64encode(bytes(_PRIVATE.public_key)).decode("ascii")


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


def _response(status: int, body: dict[str, Any] | None) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=status, headers=_headers(), body=body)


@dataclass
class _Transport:
    """Answers each request by method and path; keeps every request it was sent."""

    public_key: str = _PUBLIC_B64
    key_status: int = 200
    repo_status: int = 200
    put_status: int = 201
    put_body: dict[str, Any] | None = None
    raise_on: str | None = None
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        if self.raise_on == request.method:
            raise GithubLandingTransportError("connection reset by peer")
        if request.path == _PUBLIC_KEY_PATH:
            body = {"key_id": _KEY_ID, "key": self.public_key}
            return _response(self.key_status, body if self.key_status == 200 else {})
        if request.path == _REPO_PATH:
            body = {"id": _REPO_ID, "full_name": _REPO}
            return _response(self.repo_status, body if self.repo_status == 200 else {})
        if request.method == "PUT":
            return _response(self.put_status, self.put_body)
        raise AssertionError(f"unexpected request {request.method} {request.path}")

    @property
    def puts(self) -> list[ModelGithubHttpRequest]:
        return [r for r in self.sent if r.method == "PUT"]


@dataclass
class _Store:
    """Fake secret store reader: records the references it was asked for."""

    value: str | None = _SENTINEL
    error: Exception | None = None
    reads: list[str] = field(default_factory=list)

    async def read(self, ref: str) -> SecretStr | None:
        self.reads.append(ref)
        if self.error is not None:
            raise self.error
        return None if self.value is None else SecretStr(self.value)


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _entry(
    name: str = _NAME_USER,
    *,
    visibility: str = "selected",
    repositories: tuple[str, ...] = (_REPO,),
) -> ModelOrgSecretSetEntry:
    return ModelOrgSecretSetEntry.model_validate(
        {
            "organization": _ORG,
            "name": name,
            "secret_ref": _REF[name],
            "visibility": visibility,
            "repositories": repositories if visibility == "selected" else (),
        }
    )


def _overlay(
    entries: tuple[ModelOrgSecretSetEntry, ...] | None = None,
    *,
    with_variables: bool = False,
) -> ModelRepoWriteOverlay:
    chosen = (
        entries if entries is not None else (_entry(_NAME_USER), _entry(_NAME_PASSWORD))
    )
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=("lab:ready",)),
        repo_variables=ModelRepoVariablesOverlay(
            entries=(
                ModelRepoVariablesEntry(
                    repository=_REPO, name="LAB_VAR_ONE", source="lab/vars/one.txt"
                ),
            ),
            refuse_name_markers=("SECRET", "TOKEN", "PASSWORD"),
            refuse_value_markers=("SECRET", "TOKEN", "PASSWORD"),
        )
        if with_variables
        else None,
        org_secret_sets=ModelOrgSecretSetsOverlay(entries=chosen),
    )


def _handler(
    transport: _Transport,
    store: _Store,
    overlay: ModelRepoWriteOverlay | None,
    *,
    mode: EnumRepoWriteMode = EnumRepoWriteMode.ENFORCE,
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: mode},
        ),
        clock=lambda: _NOW,
        overlay=overlay,
        org_secret_reader=store.read,
    )


def _request(
    name: str = _NAME_USER, key: str = "org-secret-key-0001"
) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.ORG_SECRET_SET,
            "repo": _REPO,
            "idempotency_key": key,
            "requested_by_lane": "author-lane",
            "secret_name": name,
        }
    )


def _decrypt(encrypted_value: str) -> str:
    return (
        SealedBox(_PRIVATE).decrypt(base64.b64decode(encrypted_value)).decode("utf-8")
    )


def _forms(sealed: str | None = None) -> list[str]:
    forms = [_SENTINEL, base64.b64encode(_SENTINEL.encode()).decode("ascii")]
    if sealed is not None:
        forms.append(sealed)
    return forms


def _assert_value_absent(
    result: ModelRepoWriteCompleted | ModelRepoWriteFailed,
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    *,
    extra: tuple[str, ...] = (),
) -> None:
    """The value, its base64 form and every sealed form appear nowhere a reader looks."""
    sealed = [str((r.body or {}).get("encrypted_value")) for r in transport.puts]
    needles = [*_forms(), *sealed, *extra]
    readable = [
        result.model_dump_json(),
        json.dumps(result.model_dump(mode="json"), default=str),
        repr(result),
        caplog.text,
        *(record.getMessage() for record in caplog.records),
        *(str(record.args) for record in caplog.records),
    ]
    for needle in needles:
        assert needle
        assert needle != "None"
        for text in readable:
            assert needle not in text


# --- AC1: the allowlisted set -------------------------------------------------


@pytest.mark.parametrize("name", [_NAME_USER, _NAME_PASSWORD])
async def test_an_allowlisted_name_seals_the_store_value_and_puts_it_once(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transport, store = _Transport(), _Store()
    result = await _handler(transport, store, _overlay()).handle(_request(name))

    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.ENFORCE
    assert result.secret_name == name
    assert result.secret_key_id == _KEY_ID
    assert store.reads == [_REF[name]]
    assert [(r.method, r.path) for r in transport.sent] == [
        ("GET", _PUBLIC_KEY_PATH),
        ("GET", _REPO_PATH),
        ("PUT", f"/orgs/{_ORG}/actions/secrets/{name}"),
    ]
    (put,) = transport.puts
    body = put.body or {}
    assert body["key_id"] == _KEY_ID
    assert body["visibility"] == "selected"
    assert body["selected_repository_ids"] == [_REPO_ID]
    assert set(body) == {
        "encrypted_value",
        "key_id",
        "visibility",
        "selected_repository_ids",
    }
    assert _decrypt(str(body["encrypted_value"])) == _SENTINEL
    # The result records the PUT with the sealed value withheld.
    recorded_put = result.requests[-1]
    assert recorded_put.method == "PUT"
    assert (recorded_put.body or {})["encrypted_value"] == SEALED_PLACEHOLDER
    assert (recorded_put.body or {})["key_id"] == _KEY_ID
    assert result.http_statuses == (200, 200, 201)
    _assert_value_absent(result, transport, caplog)


async def test_a_204_update_of_an_existing_secret_is_completed() -> None:
    transport = _Transport(put_status=204)
    result = await _handler(transport, _Store(), _overlay()).handle(_request())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.http_statuses[-1] == 204


async def test_the_same_value_is_sealed_differently_each_time() -> None:
    first, second = (
        seal_value(_PUBLIC_B64, _SENTINEL),
        seal_value(_PUBLIC_B64, _SENTINEL),
    )
    assert first != second
    assert _decrypt(first) == _decrypt(second) == _SENTINEL


async def test_private_visibility_reads_no_repository_and_sends_no_ids() -> None:
    transport = _Transport()
    overlay = _overlay((_entry(_NAME_USER, visibility="private"),))
    result = await _handler(transport, _Store(), overlay).handle(_request())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert [r.method for r in transport.sent] == ["GET", "PUT"]
    body = transport.puts[0].body or {}
    assert body["visibility"] == "private"
    assert "selected_repository_ids" not in body


async def test_a_retry_with_the_same_key_is_replayed_without_a_second_read_or_write() -> (
    None
):
    transport, store = _Transport(), _Store()
    handler = _handler(transport, store, _overlay())
    first = await handler.handle(_request())
    second = await handler.handle(_request())
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert second.replayed is True
    assert second.secret_key_id == _KEY_ID
    assert len(transport.puts) == 1
    assert store.reads == [_REF[_NAME_USER]]


async def test_dry_run_plans_the_calls_without_a_store_read_or_a_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport, store = _Transport(), _Store()
    handler = _handler(transport, store, _overlay(), mode=EnumRepoWriteMode.DRY_RUN)
    result = await handler.handle(_request())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert [(r.method, r.path) for r in result.requests] == [
        ("GET", _PUBLIC_KEY_PATH),
        ("GET", _REPO_PATH),
        ("PUT", f"/orgs/{_ORG}/actions/secrets/{_NAME_USER}"),
    ]
    assert (result.requests[-1].body or {})["encrypted_value"] == SEALED_PLACEHOLDER
    assert store.reads == []
    assert transport.sent == []
    _assert_value_absent(result, transport, caplog)


# --- AC2: the allowlist ---------------------------------------------------------


def _not_allowed_cases() -> list[tuple[str, str, ModelRepoWriteOverlay | None]]:
    return [
        ("a name the overlay does not list", "KAFKA_SASL_OTHER", _overlay()),
        (
            "a name of another organization",
            _NAME_USER,
            _overlay(
                (
                    ModelOrgSecretSetEntry(
                        organization="other-org",
                        name=_NAME_USER,
                        secret_ref=_REF[_NAME_USER],
                        visibility="all",
                    ),
                )
            ),
        ),
        (
            "an overlay with no org_secret_sets key",
            _NAME_USER,
            ModelRepoWriteOverlay(
                pr_label=ModelPrLabelOverlay(allowed_labels=("lab:ready",))
            ),
        ),
    ]


@pytest.mark.parametrize("mode", [EnumRepoWriteMode.ENFORCE, EnumRepoWriteMode.DRY_RUN])
@pytest.mark.parametrize(
    ("case", "name", "overlay"), _not_allowed_cases(), ids=lambda v: str(v)[:40]
)
async def test_a_name_not_on_the_allowlist_is_refused_before_any_read_or_call(
    case: str,
    name: str,
    overlay: ModelRepoWriteOverlay,
    mode: EnumRepoWriteMode,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport, store = _Transport(), _Store()
    result = await _handler(transport, store, overlay, mode=mode).handle(_request(name))
    assert isinstance(result, ModelRepoWriteFailed), case
    assert result.reason is EnumRepoWriteRefusal.ORG_SECRET_NOT_ALLOWED
    assert result.terminal_failure_cause is EnumRepoWriteRefusal.ORG_SECRET_NOT_ALLOWED
    assert transport.sent == []
    assert store.reads == []
    _assert_value_absent(result, transport, caplog)


async def test_no_overlay_at_all_is_refused_before_any_read_or_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    transport, store = _Transport(), _Store()
    result = await _handler(transport, store, None).handle(_request())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.ORG_SECRET_NOT_ALLOWED
    assert transport.sent == []
    assert store.reads == []


async def test_an_allowlist_never_reaches_a_request_with_a_value_field() -> None:
    assert "value" not in ModelRepoWriteRequest.model_fields
    assert "encrypted_value" not in ModelRepoWriteRequest.model_fields
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate(
            {
                "correlation_id": _CORRELATION,
                "operation": "org_secret_set",
                "repo": _REPO,
                "idempotency_key": "org-secret-key-0001",
                "requested_by_lane": "author-lane",
                "secret_name": _NAME_USER,
                "value": _SENTINEL,
            }
        )


async def test_secret_name_is_required_and_read_only_by_org_secret_set() -> None:
    base: dict[str, Any] = {
        "correlation_id": _CORRELATION,
        "repo": _REPO,
        "idempotency_key": "org-secret-key-0001",
        "requested_by_lane": "author-lane",
    }
    with pytest.raises(ValidationError, match="secret_name"):
        ModelRepoWriteRequest.model_validate({**base, "operation": "org_secret_set"})
    with pytest.raises(ValidationError, match="read only by org_secret_set"):
        ModelRepoWriteRequest.model_validate(
            {
                **base,
                "operation": "repo_variable_get",
                "variable_name": "LAB_VAR_ONE",
                "secret_name": _NAME_USER,
            }
        )
    other = ModelRepoWriteRequest.model_validate(
        {**base, "operation": "repo_variable_get", "variable_name": "LAB_VAR_ONE"}
    )
    assert "secret_name" not in other.model_dump()


# --- AC3: the value appears nowhere --------------------------------------------


@dataclass
class _FailureCase:
    label: str
    transport: _Transport
    store: _Store
    reason: EnumRepoWriteRefusal
    puts: int


def _failure_cases() -> list[_FailureCase]:
    leaky = "store said: " + _SENTINEL
    return [
        _FailureCase(
            "the store raises an error carrying the value",
            _Transport(),
            _Store(error=OrgSecretSourceError(leaky)),
            EnumRepoWriteRefusal.ORG_SECRET_SOURCE_UNREADABLE,
            0,
        ),
        _FailureCase(
            "the store holds no value",
            _Transport(),
            _Store(value=None),
            EnumRepoWriteRefusal.ORG_SECRET_SOURCE_UNREADABLE,
            0,
        ),
        _FailureCase(
            "the public key read fails",
            _Transport(key_status=500),
            _Store(),
            EnumRepoWriteRefusal.GITHUB_REFUSED,
            0,
        ),
        _FailureCase(
            "the repository read fails",
            _Transport(repo_status=404),
            _Store(),
            EnumRepoWriteRefusal.NOT_FOUND,
            0,
        ),
        _FailureCase(
            "the public key cannot seal",
            _Transport(public_key="not-base64!"),
            _Store(),
            EnumRepoWriteRefusal.GITHUB_REFUSED,
            0,
        ),
        _FailureCase(
            "the PUT is refused",
            _Transport(put_status=422, put_body={"message": "Validation Failed"}),
            _Store(),
            EnumRepoWriteRefusal.GITHUB_REFUSED,
            1,
        ),
        _FailureCase(
            "the PUT transport fails",
            _Transport(raise_on="PUT"),
            _Store(),
            EnumRepoWriteRefusal.TRANSPORT_ERROR,
            1,
        ),
    ]


@pytest.mark.parametrize("case", _failure_cases(), ids=lambda c: c.label)
async def test_the_value_appears_nowhere_on_a_failure_path(
    case: _FailureCase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await _handler(case.transport, case.store, _overlay()).handle(_request())
    assert isinstance(result, ModelRepoWriteFailed), case.label
    assert result.reason is case.reason
    assert len(case.transport.puts) == case.puts
    _assert_value_absent(result, case.transport, caplog)
    for recorded in result.requests:
        assert (recorded.body or {}).get("encrypted_value", SEALED_PLACEHOLDER) == (
            SEALED_PLACEHOLDER
        )


async def test_the_sealed_value_is_in_the_put_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = _Transport()
    result = await _handler(transport, _Store(), _overlay()).handle(_request())
    assert isinstance(result, ModelRepoWriteCompleted)
    sealed = str((transport.puts[0].body or {})["encrypted_value"])
    assert sealed
    assert sealed != SEALED_PLACEHOLDER
    assert sealed not in result.model_dump_json()
    assert sealed not in caplog.text
    assert [
        r for r in transport.sent if sealed in r.model_dump_json()
    ] == transport.puts


class _RaisingStore:
    """A ProtocolSecretStore whose read fails with an error that carries the value."""

    async def get_secret(self, key: str) -> str | None:
        raise RuntimeError(f"backend rejected {key} holding {_SENTINEL}")


class _HoldingStore:
    async def get_secret(self, key: str) -> str | None:
        return _SENTINEL if key == _REF[_NAME_USER] else None


async def test_the_default_reader_hides_a_store_error_and_its_cause() -> None:
    with (
        injected_secret_store(_RaisingStore()),  # type: ignore[arg-type]
        pytest.raises(OrgSecretSourceError) as raised,
    ):
        await read_org_secret_value(_REF[_NAME_USER])
    error = raised.value
    assert _SENTINEL not in str(error)
    assert _SENTINEL not in repr(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


async def test_the_default_reader_reads_the_declared_reference_by_name() -> None:
    with injected_secret_store(_HoldingStore()):  # type: ignore[arg-type]
        value = await read_org_secret_value(_REF[_NAME_USER])
        missing = await read_org_secret_value(_REF[_NAME_PASSWORD])
    assert value is not None
    assert value.get_secret_value() == _SENTINEL
    assert _SENTINEL not in repr(value)
    assert _SENTINEL not in str(value)
    assert missing is None


async def test_the_default_reader_through_the_handler_refuses_a_failing_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = _Transport()
    handler = HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
        clock=lambda: _NOW,
        overlay=_overlay(),
    )
    with injected_secret_store(_RaisingStore()):  # type: ignore[arg-type]
        result = await handler.handle(_request())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.ORG_SECRET_SOURCE_UNREADABLE
    assert transport.sent == []
    _assert_value_absent(result, transport, caplog)


# --- the overlay entry ----------------------------------------------------------


def _entry_fields(**overrides: object) -> dict[str, object]:
    return {
        "organization": _ORG,
        "name": _NAME_USER,
        "secret_ref": _REF[_NAME_USER],
        "visibility": "selected",
        "repositories": (_REPO,),
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"repositories": ()},
        {"visibility": "all"},
        {"visibility": "private"},
        {"repositories": ("other-org/example-repo",)},
        {"repositories": (_REPO, _REPO.upper())},
        {"visibility": "everyone"},
        {"secret_ref": "has space"},
        {"name": "1_BAD"},
    ],
    ids=str,
)
def test_an_overlay_entry_that_is_not_well_shaped_is_refused(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ModelOrgSecretSetEntry.model_validate(_entry_fields(**overrides))


def test_an_org_secret_named_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="named twice"):
        ModelOrgSecretSetsOverlay(entries=(_entry(_NAME_USER), _entry(_NAME_USER)))


def test_the_shipped_overlay_example_lists_the_two_names_with_synthetic_values() -> (
    None
):
    document = yaml.safe_load((_NODE / "overlay.example.yaml").read_text("utf-8"))
    overlay = ModelRepoWriteOverlay.model_validate(
        {
            k: v
            for k, v in document.items()
            if k not in ("event_bus", "comment_author_logins")
        }
    )
    assert overlay.org_secret_sets is not None
    names = [e.name for e in overlay.org_secret_sets.entries]
    assert names == [_NAME_USER, _NAME_PASSWORD]
    assert {e.organization for e in overlay.org_secret_sets.entries} == {"example-org"}
    assert all(e.visibility == "selected" for e in overlay.org_secret_sets.entries)


# --- AC4: the variable path still refuses a secret ------------------------------


def _variable_request(
    name: str, operation: str = "repo_variable_set"
) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": operation,
            "repo": _REPO,
            "idempotency_key": "variable-key-0001",
            "requested_by_lane": "author-lane",
            "variable_name": name,
        }
    )


@pytest.mark.parametrize("operation", ["repo_variable_set", "repo_variable_get"])
@pytest.mark.parametrize("name", [_NAME_USER, _NAME_PASSWORD])
async def test_the_variable_path_still_refuses_a_secret_shaped_name(
    name: str, operation: str
) -> None:
    transport, store = _Transport(), _Store()
    handler = _handler(transport, store, _overlay(with_variables=True))
    result = await handler.handle(_variable_request(name, operation))
    assert isinstance(result, ModelRepoWriteFailed)
    # PASSWORD is a refuse marker; USERNAME is merely not a listed variable.
    assert result.reason in (
        EnumRepoWriteRefusal.SECRET_REFUSED,
        EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED,
    )
    if name == _NAME_PASSWORD:
        assert result.reason is EnumRepoWriteRefusal.SECRET_REFUSED
    assert transport.sent == []
    assert store.reads == []


def test_the_variable_overlay_still_refuses_to_allowlist_a_secret_name() -> None:
    with pytest.raises(ValidationError, match="can never be allowlisted"):
        ModelRepoVariablesOverlay(
            entries=(
                ModelRepoVariablesEntry(
                    repository=_REPO, name=_NAME_PASSWORD, source="lab/vars/x.txt"
                ),
            ),
            refuse_name_markers=("PASSWORD",),
            refuse_value_markers=("PASSWORD",),
        )


async def test_an_org_secret_entry_does_not_allow_the_same_name_as_a_variable() -> None:
    transport = _Transport()
    handler = _handler(transport, _Store(), _overlay(with_variables=True))
    result = await handler.handle(_variable_request(_NAME_USER, "repo_variable_get"))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert transport.sent == []


# --- positive controls: the hygiene check can fail ---------------------------------


@pytest.mark.parametrize(
    "leak", [_SENTINEL, base64.b64encode(_SENTINEL.encode()).decode("ascii")]
)
def test_the_hygiene_check_fails_when_a_result_carries_the_value(
    leak: str, caplog: pytest.LogCaptureFixture
) -> None:
    leaking = ModelRepoWriteFailed(
        correlation_id=_CORRELATION,
        operation=EnumRepoWriteOperation.ORG_SECRET_SET,
        repo=_REPO,
        mode=EnumRepoWriteMode.ENFORCE,
        idempotency_key="org-secret-key-0001",
        requested_by_lane="author-lane",
        reason=EnumRepoWriteRefusal.GITHUB_REFUSED,
        detail=f"refused {leak}",
    )
    with pytest.raises(AssertionError):
        _assert_value_absent(leaking, _Transport(), caplog)


def test_the_hygiene_check_fails_when_a_log_carries_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("lab").info("value %s", _SENTINEL)
    clean = ModelRepoWriteFailed(
        correlation_id=_CORRELATION,
        operation=EnumRepoWriteOperation.ORG_SECRET_SET,
        repo=_REPO,
        mode=EnumRepoWriteMode.ENFORCE,
        idempotency_key="org-secret-key-0001",
        requested_by_lane="author-lane",
        reason=EnumRepoWriteRefusal.GITHUB_REFUSED,
        detail="refused",
    )
    with pytest.raises(AssertionError):
        _assert_value_absent(clean, _Transport(), caplog)
