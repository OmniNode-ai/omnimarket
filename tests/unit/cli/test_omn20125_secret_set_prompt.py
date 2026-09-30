# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret set`` on a terminal: named masked prompt, confirmation, prefix check.

The piped-stdin form is pinned by ``test_omn18695_secret_cli.py`` and must not
change. These tests drive the terminal path by faking the TTY and ``getpass``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.cli.cli_secret import secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
)

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_GOOD = "sk-or-v1-abcdef0123456789"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


class _Tty:
    """Fake a terminal: scripted getpass answers, recorded prompts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: True)
        monkeypatch.setattr(cli_secret, "getpass", self._getpass)

    def _getpass(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _stored(ref: str = _REF) -> str | None:
    return asyncio.run(LocalByokCredentialStore().get_secret(ref))


def _run(args: list[str]):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(secret_group, args, catch_exceptions=False)


def test_prompt_names_the_reference_and_stores_a_confirmed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tty = _Tty(monkeypatch, [_GOOD, _GOOD])

    result = _run(["set", _REF])

    assert result.exit_code == 0, result.output
    assert _stored() == _GOOD
    assert _REF in tty.prompts[0]
    assert "hidden" in tty.prompts[0]
    assert len(tty.prompts) == 2


def test_a_mismatched_confirmation_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Tty(monkeypatch, [_GOOD, _GOOD + "x"])

    result = _run(["set", _REF])

    assert result.exit_code != 0
    assert "did not match" in result.output
    assert _stored() is None


def test_an_empty_entry_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _Tty(monkeypatch, ["   "])

    result = _run(["set", _REF])

    assert result.exit_code != 0
    assert _stored() is None


def test_a_wrong_prefix_is_refused_naming_the_expected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = "sk-not-an-openrouter-key"
    _Tty(monkeypatch, [bad, bad])

    result = _run(["set", _REF])

    assert result.exit_code != 0
    assert "sk-or-v1-" in result.output
    assert bad not in result.output
    assert _stored() is None
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == []


def test_an_unknown_provider_has_no_prefix_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Tty(monkeypatch, ["whatever-shape", "whatever-shape"])

    result = _run(["set", "llm.glm.api_key"])

    assert result.exit_code == 0, result.output
    assert _stored("llm.glm.api_key") == "whatever-shape"


def test_the_value_never_reaches_output_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = _GOOD + "-SENTINEL"
    _Tty(monkeypatch, [secret, secret])

    with caplog.at_level(logging.DEBUG):
        result = CliRunner().invoke(secret_group, ["set", _REF], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    assert secret not in caplog.text


def test_a_wrong_prefix_on_the_prompt_never_echoes_the_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = "SENTINEL-wrong-prefix"
    _Tty(monkeypatch, [bad, bad])

    with caplog.at_level(logging.DEBUG):
        result = _run(["set", _REF])

    assert bad not in result.output
    assert bad not in caplog.text


def test_the_piped_form_is_unchanged_and_skips_the_prefix_check() -> None:
    result = CliRunner().invoke(
        secret_group, ["set", _REF], input="sk-customer-key\n", catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    assert _stored() == "sk-customer-key"
