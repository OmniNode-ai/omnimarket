# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret`` — how a customer puts a provider key on their machine (OMN-18695).

The resolver half of this ticket makes a declared ``secret_ref`` resolvable
only from the local store. That is a refusal with nowhere to go unless there is
a way to write to the store, so the command is part of the same change rather
than a follow-up.

The value never travels on argv. ``ps`` shows another user's command line and
the shell writes it to history, so a ``--value`` flag would put the key in two
places the store exists to keep it out of. Reading it from stdin (or from a
hidden prompt on a terminal) is the same posture ``onex auth login`` already
takes.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
)

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_VALUE = "sk-customer-key"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


def _run(args: list[str], stdin: str | None = None) -> object:
    return CliRunner().invoke(secret_group, args, input=stdin, catch_exceptions=False)


class TestSet:
    def test_reads_the_value_from_stdin(self) -> None:
        result = _run(["set", _REF], stdin=f"{_VALUE}\n")

        assert result.exit_code == 0, result.output
        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) == _VALUE

    def test_does_not_echo_the_value(self) -> None:
        result = _run(["set", _REF], stdin=f"{_VALUE}\n")

        assert _VALUE not in result.output

    def test_names_the_reference_it_stored(self) -> None:
        result = _run(["set", _REF], stdin=f"{_VALUE}\n")

        assert _REF in result.output

    def test_surrounding_whitespace_is_stripped(self) -> None:
        _run(["set", _REF], stdin=f"  {_VALUE}  \n")

        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) == _VALUE

    def test_refuses_a_value_passed_on_argv(self) -> None:
        result = CliRunner().invoke(secret_group, ["set", _REF, _VALUE])

        assert result.exit_code != 0
        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) is None

    def test_refuses_an_empty_stdin(self) -> None:
        result = CliRunner().invoke(secret_group, ["set", _REF], input="\n")

        assert result.exit_code != 0
        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) is None

    def test_refuses_an_overwrite_without_the_force_flag(self) -> None:
        _run(["set", _REF], stdin="first\n")

        result = CliRunner().invoke(secret_group, ["set", _REF], input="second\n")

        assert result.exit_code != 0
        assert "--force" in result.output
        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) == "first"

    def test_force_overwrites(self) -> None:
        _run(["set", _REF], stdin="first\n")

        result = _run(["set", _REF, "--force"], stdin="second\n")

        assert result.exit_code == 0, result.output
        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) == "second"

    def test_the_stored_file_is_owner_only(self, local_store_at_tmp: Path) -> None:
        _run(["set", _REF], stdin=f"{_VALUE}\n")

        mode = stat.S_IMODE(local_store_at_tmp.stat().st_mode)
        assert mode & 0o077 == 0


class TestList:
    def test_prints_references_never_values(self) -> None:
        _run(["set", _REF], stdin=f"{_VALUE}\n")

        result = _run(["list"])

        assert result.exit_code == 0, result.output
        assert _REF in result.output
        assert _VALUE not in result.output

    def test_an_empty_store_says_so_and_names_the_command(self) -> None:
        result = _run(["list"])

        assert result.exit_code == 0, result.output
        assert "onex secret set" in result.output


class TestDelete:
    def test_removes_the_entry(self) -> None:
        _run(["set", _REF], stdin=f"{_VALUE}\n")

        result = _run(["delete", _REF])

        assert result.exit_code == 0, result.output
        assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) is None

    def test_deleting_an_absent_reference_is_an_error_not_a_silent_success(
        self,
    ) -> None:
        result = CliRunner().invoke(secret_group, ["delete", _REF])

        assert result.exit_code != 0


class TestEntryPointRegistration:
    """The command exists because the distribution advertises it (OMN-16967 rule)."""

    def test_declared_in_the_onex_cli_entry_point_group(self) -> None:
        pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(
            encoding="utf-8"
        )

        assert 'secret = "omnimarket.cli.cli_secret:secret_group"' in pyproject

    def test_not_hand_wired_with_add_command(self) -> None:
        source = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "omnimarket"
            / "cli"
            / "cli_secret.py"
        ).read_text(encoding="utf-8")

        assert "add_command" not in source
