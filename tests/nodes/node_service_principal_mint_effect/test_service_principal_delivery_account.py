# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The optional `account` of a credential delivery row (OMN-26582).

A delivery row may name the OS account whose home the folder is relative to. The fixed delivery script resolves the home on the
target with `getent passwd <account>`, refuses an unknown account, a uid that differs from the declared owner's uid, a home that
is a symlink and a home that does not exist, and never creates the home. The account name travels on standard input only.

The tests run the real script locally (`local_hosts`) with a stand-in `getent` first on PATH, so each passwd answer is chosen by the
test; no real account, host or credential is involved.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_service_principal_mint_effect.handlers import (
    adapter_ssh_credential_delivery,
)
from omnimarket.nodes.node_service_principal_mint_effect.handlers.adapter_ssh_credential_delivery import (
    SshCredentialDelivery,
)
from omnimarket.nodes.node_service_principal_mint_effect.models.model_service_principal_mint import (
    ModelServicePrincipalDelivery,
)

pytestmark = pytest.mark.unit

ACCOUNT = "acct-zq7"
SECRET = SecretStr("PASSWORD=SENTINEL-ACCOUNT-DELIVERY-VALUE\n")
MY_UID = os.getuid()
MY_GID = os.getgid()
OWNER = f"{MY_UID}:{MY_GID}"

_FAKE_GETENT = r"""#!/bin/sh
echo "$*" >> "$GETENT_FAKE_DB.calls"
[ "$1" = passwd ] || exit 2
while IFS= read -r line; do
    case "$line" in
        "$2":*) printf '%s\n' "$line"; exit 0 ;;
    esac
done < "$GETENT_FAKE_DB"
exit 2
"""


class _Passwd:
    """A stand-in `getent` on PATH, answering from one file the test writes."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = root / "passwd.db"
        self.db.write_text("")
        bindir = root / "fakebin"
        bindir.mkdir()
        getent = bindir / "getent"
        getent.write_text(_FAKE_GETENT)
        getent.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("GETENT_FAKE_DB", str(self.db))
        self.calls = Path(f"{self.db}.calls")

    def add(self, name: str, uid: int, home: Path) -> None:
        with self.db.open("a") as handle:
            handle.write(f"{name}:x:{uid}:{uid}::{home}:/bin/sh\n")

    def lookups(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def passwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Passwd:
    return _Passwd(tmp_path, monkeypatch)


def _row(**cells: Any) -> ModelServicePrincipalDelivery:
    base: dict[str, Any] = {
        "host": "local-test",
        "ssh_target": "unused",
        "folder": ".onex",
        "filename": "agent.env",
        "owner": OWNER,
        "mode": "0400",
        "privilege_argv": (),
    }
    base.update(cells)
    return ModelServicePrincipalDelivery(**base)


def _deliver(row: ModelServicePrincipalDelivery, *, apply: bool = True) -> Any:
    delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))
    return asyncio.run(delivery.deliver(row, SECRET, apply=apply))


# -- the model ---------------------------------------------------------------------------------------------------------


def test_row_without_account_still_loads_and_defaults_to_none() -> None:
    row = _row()
    assert row.account is None


def test_row_with_account_and_relative_folder_loads() -> None:
    row = _row(account=ACCOUNT, folder=".onex")
    assert row.account == ACCOUNT
    assert row.folder == ".onex"


@pytest.mark.parametrize("folder", ["/srv/elsewhere/.onex", "/.onex"])
def test_account_with_an_absolute_folder_is_refused(folder: str) -> None:
    with pytest.raises(ValidationError):
        _row(account=ACCOUNT, folder=folder)


@pytest.mark.parametrize("folder", ["../other/.onex", ".onex/../../other", ".."])
def test_account_with_a_dotdot_folder_is_refused(folder: str) -> None:
    with pytest.raises(ValidationError):
        _row(account=ACCOUNT, folder=folder)


@pytest.mark.parametrize(
    "account", ["", "-x", "1002", "a b", "a\nb", "a/b", "a;b", "a$b", "x" * 65]
)
def test_an_account_that_is_not_a_login_name_is_refused(account: str) -> None:
    with pytest.raises(ValidationError):
        _row(account=account)


# -- the script --------------------------------------------------------------------------------------------------------


def test_account_row_writes_inside_the_account_home(
    tmp_path: Path, passwd: _Passwd
) -> None:
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add(ACCOUNT, MY_UID, home)
    row = _row(account=ACCOUNT)

    would = _deliver(row, apply=False)
    assert would.state == "would-write"
    assert not (home / ".onex").exists()

    written = _deliver(row)
    assert written.state == "written"
    dest = home / ".onex" / "agent.env"
    assert dest.read_text() == SECRET.get_secret_value()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o400
    assert passwd.lookups() == [f"passwd {ACCOUNT}", f"passwd {ACCOUNT}"]

    assert _deliver(row).state == "unchanged"


def test_account_row_ignores_the_login_home(
    tmp_path: Path, passwd: _Passwd, monkeypatch: pytest.MonkeyPatch
) -> None:
    login_home = tmp_path / "login-home"
    login_home.mkdir()
    monkeypatch.setenv("HOME", str(login_home))
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add(ACCOUNT, MY_UID, home)

    assert _deliver(_row(account=ACCOUNT)).state == "written"
    assert (home / ".onex" / "agent.env").exists()
    assert not (login_home / ".onex").exists()


def test_unknown_account_is_refused_and_nothing_is_written(
    tmp_path: Path, passwd: _Passwd
) -> None:
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add("someone-else", MY_UID, home)

    reading = _deliver(_row(account=ACCOUNT))

    assert reading.state == "failed"
    assert "unknown account" in reading.detail
    assert not (home / ".onex").exists()
    assert SECRET.get_secret_value() not in reading.detail


def test_uid_that_differs_from_the_declared_owner_is_refused(
    tmp_path: Path, passwd: _Passwd
) -> None:
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add(ACCOUNT, MY_UID + 1, home)

    reading = _deliver(_row(account=ACCOUNT))

    assert reading.state == "failed"
    assert "uid" in reading.detail
    assert not (home / ".onex").exists()


def test_symlinked_home_is_refused_and_nothing_is_written(
    tmp_path: Path, passwd: _Passwd
) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "homes" / ACCOUNT
    link.parent.mkdir()
    link.symlink_to(real)
    passwd.add(ACCOUNT, MY_UID, link)

    reading = _deliver(_row(account=ACCOUNT))

    assert reading.state == "failed"
    assert "symlink" in reading.detail
    assert not (real / ".onex").exists()


def test_missing_home_is_refused_and_never_created(
    tmp_path: Path, passwd: _Passwd
) -> None:
    home = tmp_path / "homes" / ACCOUNT
    passwd.add(ACCOUNT, MY_UID, home)

    reading = _deliver(_row(account=ACCOUNT))

    assert reading.state == "failed"
    assert "home" in reading.detail
    assert not home.exists()
    assert not home.parent.exists()


def test_script_itself_refuses_an_absolute_folder_with_an_account(
    tmp_path: Path, passwd: _Passwd
) -> None:
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add(ACCOUNT, MY_UID, home)
    elsewhere = tmp_path / "elsewhere"
    row = ModelServicePrincipalDelivery.model_construct(
        host="local-test",
        ssh_target="unused",
        folder=str(elsewhere),
        filename="agent.env",
        owner=OWNER,
        mode="0400",
        privilege_argv=(),
        account=ACCOUNT,
    )

    reading = _deliver(row)

    assert reading.state == "failed"
    assert not elsewhere.exists()


def test_script_itself_refuses_a_dotdot_folder_with_an_account(
    tmp_path: Path, passwd: _Passwd, monkeypatch: pytest.MonkeyPatch
) -> None:
    login_home = tmp_path / "login" / "home"
    login_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(login_home))
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add(ACCOUNT, MY_UID, home)
    row = ModelServicePrincipalDelivery.model_construct(
        host="local-test",
        ssh_target="unused",
        folder="../escaped",
        filename="agent.env",
        owner=OWNER,
        mode="0400",
        privilege_argv=(),
        account=ACCOUNT,
    )

    reading = _deliver(row)

    assert reading.state == "failed"
    assert not (home.parent / "escaped").exists()
    assert not (login_home.parent / "escaped").exists()


def test_script_itself_refuses_a_numeric_account_without_a_lookup(
    tmp_path: Path, passwd: _Passwd
) -> None:
    home = tmp_path / "homes" / ACCOUNT
    home.mkdir(parents=True)
    passwd.add(str(MY_UID), MY_UID, home)
    row = ModelServicePrincipalDelivery.model_construct(
        host="local-test",
        ssh_target="unused",
        folder=".onex",
        filename="agent.env",
        owner=OWNER,
        mode="0400",
        privilege_argv=(),
        account=str(MY_UID),
    )

    reading = _deliver(row)

    assert reading.state == "failed"
    assert passwd.lookups() == []
    assert not (home / ".onex").exists()


def test_row_without_account_never_looks_an_account_up(
    tmp_path: Path, passwd: _Passwd
) -> None:
    folder = tmp_path / "plain"
    reading = _deliver(_row(folder=str(folder)))

    assert reading.state == "written"
    assert (folder / "agent.env").read_text() == SECRET.get_secret_value()
    assert passwd.lookups() == []


def test_row_without_account_keeps_the_login_home_relative_folder(
    tmp_path: Path, passwd: _Passwd, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _deliver(_row(folder=".creds")).state == "written"
    assert (tmp_path / ".creds" / "agent.env").exists()
    assert passwd.lookups() == []


# -- the command line --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("local", [True, False])
def test_account_and_secret_are_on_standard_input_only(
    monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((list(argv), kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, f"written {OWNER} 400\n", "")

    monkeypatch.setattr(adapter_ssh_credential_delivery.subprocess, "run", fake_run)
    row = _row(account=ACCOUNT, host="local-test" if local else "remote-test")
    delivery = SshCredentialDelivery(local_hosts=frozenset({"local-test"}))

    reading = asyncio.run(delivery.deliver(row, SECRET, apply=True))

    assert reading.state == "written"
    assert len(seen) == 1
    argv, stdin = seen[0]
    for word in argv:
        assert ACCOUNT not in word
        assert "SENTINEL-ACCOUNT-DELIVERY-VALUE" not in word
    header = stdin.split("\n")
    assert ACCOUNT in header[: len(header) - 2]
    assert stdin.endswith(SECRET.get_secret_value())
