# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain: the consumer half of captured secret references (OMN-20926).

A reference resolves only through a store, the store is the caller's, and the
access log names the reference and the accessor and never the value.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from omnimarket.models.captured_secret.model_captured_secret_store_overlay import (
    ModelCapturedSecretStoreOverlay,
    load_captured_secret_store_overlay,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.handlers import (
    handler_captured_secret_resolve as handler_module,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.handlers.handler_captured_secret_resolve import (
    CapturedSecretReaderNotConfiguredError,
    CapturedSecretReferenceError,
    HandlerCapturedSecretResolve,
    find_captured_secret_references,
    reader_for_this_machine,
    store_key_for_reference,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_request import (
    ModelCapturedSecretResolveRequest,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_result import (
    EnumCapturedSecretResolveOutcome,
    ModelCapturedSecretResolveResult,
)
from omnimarket.nodes.node_event_emit_effect.redaction import load_contract

_VALUE = "FAKE-" + "v4lue" * 6
_LOGGER = (
    "omnimarket.nodes.node_captured_secret_resolve_effect.handlers."
    "handler_captured_secret_resolve"
)


def _digest(seed: str = "one") -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _ref(seed: str = "one") -> str:
    reference = load_contract().secret_reference
    assert reference is not None
    return reference.reference_for(_digest(seed))


def _store_key(seed: str = "one") -> str:
    reference = load_contract().secret_reference
    assert reference is not None
    return reference.store_key_for(_digest(seed))


class _FakeStore:
    """The read slice of ProtocolSecretStore, recording what was asked."""

    def __init__(self, values: dict[str, str], *, refuse: bool = False) -> None:
        self.values = values
        self.refuse = refuse
        self.asked: list[str] = []

    async def get_secret(self, key: str) -> str | None:
        self.asked.append(key)
        if self.refuse:
            raise PermissionError("403 from the store")
        return self.values.get(key)


def _resolve(
    store: _FakeStore, reference: str, accessor: str = "reader"
) -> ModelCapturedSecretResolveResult:
    return HandlerCapturedSecretResolve(store=store).handle(
        ModelCapturedSecretResolveRequest(reference=reference, accessor=accessor)
    )


@pytest.mark.unit
def test_store_key_is_the_one_the_contract_names() -> None:
    assert store_key_for_reference(_ref()) == _store_key()


@pytest.mark.unit
@pytest.mark.parametrize("variant", ["short", "embedded", "upper", "namespace"])
def test_store_key_refuses_anything_but_one_whole_reference(variant: str) -> None:
    text = {
        "short": _ref()[:-10],
        "embedded": "x " + _ref(),
        "upper": _ref().upper(),
        "namespace": _ref().replace("captured", "other"),
    }[variant]
    with pytest.raises(CapturedSecretReferenceError):
        store_key_for_reference(text)


@pytest.mark.unit
def test_find_returns_every_reference_in_order() -> None:
    text = f"a {_ref('one')} b {_ref('two')} c {_ref('one')}"
    assert find_captured_secret_references(text) == [
        _ref("one"),
        _ref("two"),
        _ref("one"),
    ]


@pytest.mark.unit
def test_resolve_returns_the_stored_value_and_logs_without_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FakeStore({_store_key(): _VALUE})
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        result = _resolve(store, _ref(), accessor="node_session_content_test")
    assert result.outcome is EnumCapturedSecretResolveOutcome.RESOLVED
    assert result.value is not None
    assert result.value.get_secret_value() == _VALUE
    assert store.asked == [_store_key()]
    assert _ref() in caplog.text
    assert "node_session_content_test" in caplog.text
    assert _VALUE not in caplog.text
    # A dumped result is safe to log or publish: the value is masked.
    assert _VALUE not in result.model_dump_json()


@pytest.mark.unit
def test_resolve_fails_closed_on_a_missing_value() -> None:
    result = _resolve(_FakeStore({}), _ref())
    assert result.outcome is EnumCapturedSecretResolveOutcome.MISSING
    assert result.value is None


@pytest.mark.unit
def test_resolve_fails_closed_when_the_store_refuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        result = _resolve(_FakeStore({_store_key(): _VALUE}, refuse=True), _ref())
    assert result.outcome is EnumCapturedSecretResolveOutcome.REFUSED
    assert result.value is None
    assert result.detail == "PermissionError"
    assert "refused" in caplog.text


@pytest.mark.unit
def test_resolve_refuses_a_malformed_reference_without_asking_the_store() -> None:
    store = _FakeStore({})
    result = _resolve(store, "not-a-reference")
    assert result.outcome is EnumCapturedSecretResolveOutcome.MALFORMED
    assert store.asked == []


@pytest.mark.unit
def test_resolve_requires_an_accessor() -> None:
    with pytest.raises(ValueError, match="accessor"):
        ModelCapturedSecretResolveRequest(reference=_ref(), accessor=" ")


_OVERLAY_BLOCK = (
    "captured_secret_store:\n"
    "  infisical_addr: http://store.example.test:8080\n"
    "  project_id: 00000000-0000-4000-8000-000000000000\n"
    "  environment_slug: dev\n"
    "  secret_path: /captured\n"
    "  writer_client_id: writer-id\n"
    "  writer_client_secret_ref: captured-writer\n"
    "  reference_key_ref: captured-reference-key\n"
)
_READER = "  reader_client_id: reader-id\n  reader_client_secret_ref: captured-reader\n"


def _onex_home(tmp_path: Path, *, reader: bool = True, mode: int = 0o600) -> Path:
    home = tmp_path / "onex"
    home.mkdir()
    (home / "config.yaml").write_text(
        _OVERLAY_BLOCK + (_READER if reader else ""), encoding="utf-8"
    )
    secrets = home / "credentials.json"
    secrets.write_text('{"captured-reader": "FAKE-reader-secret"}', encoding="utf-8")
    secrets.chmod(mode)
    return home


@pytest.mark.unit
def test_overlay_reads_addressing_and_both_identities_by_reference(
    tmp_path: Path,
) -> None:
    import yaml

    home = _onex_home(tmp_path)
    loaded = load_captured_secret_store_overlay(
        yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    )
    assert loaded.secret_path == "/captured"
    assert loaded.reader_client_id == "reader-id"
    # The block carries both identities as names and references only.
    assert loaded.writer_client_id == "writer-id"
    assert loaded.reference_key_ref == "captured-reference-key"


@pytest.mark.unit
def test_overlay_names_missing_fields() -> None:
    with pytest.raises(ValueError, match="infisical_addr"):
        load_captured_secret_store_overlay(
            {"captured_secret_store": {"environment_slug": "dev"}}
        )
    with pytest.raises(ValueError, match="no 'captured_secret_store' block"):
        load_captured_secret_store_overlay({})


@pytest.mark.unit
def test_this_machine_reader_logs_in_as_the_reader_identity_on_first_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore({_store_key(): _VALUE})
    builds: list[dict[str, object]] = []

    def _build(
        overlay: ModelCapturedSecretStoreOverlay, **identity: object
    ) -> _FakeStore:
        builds.append({"overlay": overlay, **identity})
        return store

    monkeypatch.setattr(handler_module, "build_captured_secret_store", _build)
    reader = reader_for_this_machine(_onex_home(tmp_path))
    handler = HandlerCapturedSecretResolve(store=reader)

    malformed = handler.handle(
        ModelCapturedSecretResolveRequest(reference="secret:nope", accessor="t")
    )
    assert malformed.outcome is EnumCapturedSecretResolveOutcome.MALFORMED
    assert builds == [], "a malformed reference cost a login"

    result = handler.handle(
        ModelCapturedSecretResolveRequest(reference=_ref(), accessor="test")
    )
    assert result.outcome is EnumCapturedSecretResolveOutcome.RESOLVED
    assert store.asked == [_store_key()]
    assert len(builds) == 1
    client_id = builds[0]["client_id"]
    secret = builds[0]["client_secret"]
    assert hasattr(client_id, "get_secret_value")
    assert hasattr(secret, "get_secret_value")
    assert client_id.get_secret_value() == "reader-id"
    assert secret.get_secret_value() == "FAKE-reader-secret"


@pytest.mark.unit
def test_this_machine_reader_refuses_without_a_reader_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(CapturedSecretReaderNotConfiguredError, match="reader"):
        reader_for_this_machine(_onex_home(tmp_path, reader=False))
    with pytest.raises(CapturedSecretReaderNotConfiguredError, match="no 'captured"):
        reader_for_this_machine(tmp_path / "absent")


@pytest.mark.unit
def test_this_machine_reader_refuses_a_readable_credentials_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(Exception, match="0600"):
        reader_for_this_machine(_onex_home(tmp_path, mode=0o644))


@pytest.mark.unit
def test_cli_check_captured_prints_the_outcome_never_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from omnimarket.cli import cli_secret

    monkeypatch.setattr(
        cli_secret,
        "reader_for_this_machine",
        lambda _home: _FakeStore({_store_key(): _VALUE}),
    )
    result = CliRunner().invoke(cli_secret.secret_group, ["check-captured", _ref()])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("resolved: " + _store_key())
    assert _VALUE not in result.output

    monkeypatch.setattr(
        cli_secret, "reader_for_this_machine", lambda _home: _FakeStore({})
    )
    missing = CliRunner().invoke(cli_secret.secret_group, ["check-captured", _ref()])
    assert missing.exit_code == 1
    assert missing.output.startswith("missing: ")
