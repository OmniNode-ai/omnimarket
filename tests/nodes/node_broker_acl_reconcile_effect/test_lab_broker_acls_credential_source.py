# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker superuser credential comes from its declared secret source, by name (lab_broker_acls).

The prologue every broker ACL, topic-delete and principal-mint call shares used to read the dev
superuser credential from the env of an exited one-shot container, which a prune removed. It now
reads DEV_KAFKA_SASL_USERNAME and DEV_KAFKA_SASL_PASSWORD, by name, from the declared operator env
file on the broker host. These tests run the generated prologue under ``sh`` with a stub ``docker``
and a temp env file. The stub logs every call and answers ``inspect`` with a decoy credential pair, so a
lookup that falls back to a container is seen: no inspect call is made and the decoy is never used. A
missing name fails closed with exit 3 and a fixed marker line, ``run_remote`` raises the typed error only
for exit 3 plus that marker, and no value reaches stdout, stderr or an error message.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any, Literal

import pytest

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    driver_broker_acl as driver,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    handler_broker_acl_reconcile as reconcile,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

SENTINEL_USER = "svc-sentinel-user"
SENTINEL_PASS = "pw-sentinel-0123456789"  # synthetic test value, not a credential
DECOY_USER = "decoy-user-from-container"
DECOY_PASS = "decoy-pass-from-container"  # synthetic test value, not a credential
ARGV = [["security", "acl", "list", "--allow-principal", "User:x"]]

# Logs every call, ``inspect`` included, and answers ``inspect`` with a decoy pair: a lookup that
# falls back to a container leaves an ``inspect`` line in the log and uses the decoy.
STUB_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DOCKER_LOG"
if [ "$1" = "inspect" ]; then
  printf 'DEV_KAFKA_SASL_USERNAME=decoy-user-from-container\\n'
  printf 'DEV_KAFKA_SASL_PASSWORD=decoy-pass-from-container\\n'
  exit 0
fi
exit "${STUB_DOCKER_EXIT:-0}"
"""


def _connection(env_file: str | Path | None = None) -> driver.AclConnection:
    raw = {
        "lane": "dev",
        "host": "dev-lane-broker",
        "compose_project": "omnibase-infra",
        "broker_container": "omnibase-infra-redpanda",
        "superuser_source_container": "omnibase-infra-redpanda-sasl-enable",
    }
    if env_file is not None:
        raw["superuser_env_file"] = str(env_file)
    return driver.AclConnection.from_mapping(raw, "lab-target")


def _run_prologue(
    tmp_path: Path,
    env_text: str | None,
    argvs: list[list[str]],
    *,
    env_file: str | Path | None = "operator.env",
    docker_exit: int = 0,
    script_override: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the generated script locally with no container and a logging stub docker.

    ``env_file`` is a name under ``tmp_path``, an absolute path, or None for the default connection
    (``~/.omnibase/.env`` with ``HOME`` set to ``tmp_path``).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    if env_file is None:
        declared: Path | None = None
        target = tmp_path / ".omnibase" / ".env"
    else:
        declared = tmp_path / env_file
        target = declared
    if env_text is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(env_text, encoding="utf-8", newline="")
    log = tmp_path / "docker.log"
    script = script_override or driver.build_remote_script(_connection(declared), argvs)
    done = subprocess.run(
        ["sh", "-s"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "STUB_DOCKER_LOG": str(log),
            "STUB_DOCKER_EXIT": str(docker_exit),
        },
    )
    return done, log


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _assert_no_container_lookup(log: Path, *outputs: str) -> None:
    """The AC3 falsifier: no ``inspect`` call was made and the decoy never reached an rpk call or output."""
    calls = _calls(log)
    assert not [c for c in calls if c.startswith("inspect")], calls
    joined = "\n".join(calls) + "".join(outputs)
    assert DECOY_USER not in joined
    assert DECOY_PASS not in joined


def _run_remote_locally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_text: str | None,
    argvs: list[list[str]],
    *,
    docker_exit: int = 0,
) -> tuple[str, Path]:
    """Send the generated script through the real ``run_remote``, with ssh replaced by a local ``sh -s``.

    The returncode and stderr are the script's own, not text a test wrote.
    """
    real_run = subprocess.run
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env_file = tmp_path / "operator.env"
    if env_text is not None:
        env_file.write_text(env_text, encoding="utf-8")
    log = tmp_path / "docker.log"

    def local_ssh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "ssh"
        assert argv[-2:] == ["sh", "-s"]
        return real_run(
            ["sh", "-s"],
            **kwargs,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "STUB_DOCKER_LOG": str(log),
                "STUB_DOCKER_EXIT": str(docker_exit),
            },
        )

    monkeypatch.setattr(driver.subprocess, "run", local_ssh)
    script = driver.build_remote_script(_connection(env_file), argvs)
    return script, log


def test_credential_is_read_by_name_from_the_declared_source_with_no_container(
    tmp_path: Path,
) -> None:
    env_text = (
        "UNRELATED=1\n"
        f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\n"
        f"DEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n"
    )
    done, log = _run_prologue(tmp_path, env_text, ARGV)
    assert done.returncode == 0, done.stderr
    call = log.read_text(encoding="utf-8")
    assert f"user={SENTINEL_USER}" in call
    assert f"pass={SENTINEL_PASS}" in call
    assert call.startswith("exec omnibase-infra-redpanda rpk security acl list")
    _assert_no_container_lookup(log, done.stdout, done.stderr)
    assert SENTINEL_PASS not in done.stdout + done.stderr
    assert SENTINEL_USER not in done.stdout + done.stderr


def test_the_stub_sees_a_container_fallback(tmp_path: Path) -> None:
    """Positive control for the AC3 falsifier: a script that falls back to ``docker inspect`` is caught."""
    fallback = (
        "set -eu\n"
        "U=\"$(docker inspect c --format x | sed -n 's/^DEV_KAFKA_SASL_USERNAME=//p')\"\n"
        "P=\"$(docker inspect c --format x | sed -n 's/^DEV_KAFKA_SASL_PASSWORD=//p')\"\n"
        'docker exec b rpk security acl list -X user="$U" -X pass="$P"\n'
    )
    done, log = _run_prologue(tmp_path, None, ARGV, script_override=fallback)
    assert done.returncode == 0, done.stderr
    assert any(call.startswith("inspect") for call in _calls(log))
    assert f"pass={DECOY_PASS}" in "\n".join(_calls(log))
    with pytest.raises(AssertionError):
        _assert_no_container_lookup(log, done.stdout, done.stderr)


def test_script_never_inspects_a_container() -> None:
    script = driver.build_remote_script(_connection(), ARGV)
    assert "docker inspect" not in script
    assert "omnibase-infra-redpanda-sasl-enable" not in script


def test_lab_broker_acls_credential_source_default_connection_runs_the_production_home_form(
    tmp_path: Path,
) -> None:
    """The default ``~/.omnibase/.env`` form is executed with HOME set, not only string-checked."""
    env_text = f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\nDEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n"
    done, log = _run_prologue(tmp_path, env_text, ARGV, env_file=None)
    assert done.returncode == 0, done.stderr
    call = log.read_text(encoding="utf-8")
    assert f"user={SENTINEL_USER}" in call
    assert f"pass={SENTINEL_PASS}" in call
    _assert_no_container_lookup(log, done.stdout, done.stderr)


@pytest.mark.parametrize(
    "declared",
    ["/srv/custom/operator.env", "~/custom-dir/operator.env"],
)
def test_superuser_env_file_declared_path_reaches_the_script(declared: str) -> None:
    script = driver.build_remote_script(_connection(declared), ARGV)
    expected = (
        f"F={declared}" if declared.startswith("/") else f'F="$HOME"/{declared[2:]}'
    )
    assert expected in script.splitlines()
    assert 'F="$HOME"/.omnibase/.env' not in script


@pytest.mark.parametrize(
    ("text", "user", "password"),
    [
        pytest.param(
            "DEV_KAFKA_SASL_USERNAME=u1\r\nDEV_KAFKA_SASL_PASSWORD=p1\r\n",
            "u1",
            "p1",
            id="crlf",
        ),
        pytest.param(
            "DEV_KAFKA_SASL_USERNAME=u1   \nDEV_KAFKA_SASL_PASSWORD=p1\t\n",
            "u1",
            "p1",
            id="trailing-whitespace",
        ),
        pytest.param(
            "DEV_KAFKA_SASL_USERNAME=u1 # the user\nDEV_KAFKA_SASL_PASSWORD=p1\t# the pass\n",
            "u1",
            "p1",
            id="inline-comment",
        ),
        pytest.param(
            "DEV_KAFKA_SASL_USERNAME=\"u 1\" # c\nDEV_KAFKA_SASL_PASSWORD='p#1'  # c\n",
            "u 1",
            "p#1",
            id="quoted-with-comment",
        ),
        pytest.param(
            "export DEV_KAFKA_SASL_USERNAME=u1\n  export   DEV_KAFKA_SASL_PASSWORD=p1\n",
            "u1",
            "p1",
            id="export-prefix",
        ),
        pytest.param(
            "DEV_KAFKA_SASL_USERNAME=old\nDEV_KAFKA_SASL_USERNAME=u1\n"
            "DEV_KAFKA_SASL_PASSWORD=p1\n",
            "u1",
            "p1",
            id="last-assignment-wins",
        ),
        pytest.param(
            "DEV_KAFKA_SASL_USERNAME=\"'u1'\"\nDEV_KAFKA_SASL_PASSWORD='\"p1\"'\n",
            "'u1'",
            '"p1"',
            id="nested-quotes-stripped-once",
        ),
    ],
)
def test_env_file_formats_the_consumer_reads_give_the_value_the_shell_gives(
    tmp_path: Path, text: str, user: str, password: str
) -> None:
    """Parity with the file's consumer, which sources it as shell (CR aside, which the reader strips)."""
    done, log = _run_prologue(tmp_path, text, ARGV)
    assert done.returncode == 0, done.stderr
    call = log.read_text(encoding="utf-8")
    assert call.rstrip().endswith(f"-X user={user} -X pass={password}"), call
    sourced = subprocess.run(
        [
            "sh",
            "-c",
            '. "$1"; printf "%s\\n%s" "$DEV_KAFKA_SASL_USERNAME" "$DEV_KAFKA_SASL_PASSWORD"',
            "sh",
            str(tmp_path / "operator.env"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    if "\r" not in text:
        assert sourced.stdout == f"{user}\n{password}"


def test_assignment_with_spaces_around_equals_is_not_a_shell_assignment_and_fails_closed(
    tmp_path: Path,
) -> None:
    done, log = _run_prologue(
        tmp_path,
        f"DEV_KAFKA_SASL_USERNAME = {SENTINEL_USER}\nDEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n",
        ARGV,
    )
    assert done.returncode == driver.CREDENTIAL_MISSING_EXIT
    assert f"{driver.CREDENTIAL_MISSING_MARKER} DEV_KAFKA_SASL_USERNAME" in done.stderr
    assert not _calls(log)


# Forms the shell consumer reads differently from the reader: each is refused with the exit and the
# marker naming the key, never a guessed value. ``$`` and a backslash escape are decoded or expanded by
# the shell; ``NAME= v`` runs ``v`` as a command with NAME empty.
UNREADABLE_FORMS = [
    pytest.param('"a\\"b"', id="escaped-quote-in-double-quotes"),
    pytest.param(" v1", id="leading-space-after-equals"),
    pytest.param("pa$ss1", id="unexpanded-dollar-unquoted"),
    pytest.param('"pa$ss1"', id="unexpanded-dollar-in-double-quotes"),
    pytest.param("pa`id`ss", id="backtick"),
    pytest.param("pa\\ss1", id="backslash-escape-unquoted"),
]


@pytest.mark.parametrize("name", driver.CREDENTIAL_NAMES)
@pytest.mark.parametrize("value", UNREADABLE_FORMS)
def test_value_forms_the_shell_reads_differently_fail_closed_with_the_marker(
    tmp_path: Path, name: str, value: str
) -> None:
    lines = {
        "DEV_KAFKA_SASL_USERNAME": f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\n",
        "DEV_KAFKA_SASL_PASSWORD": f"DEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n",
    }
    lines[name] = f"{name}={value}\n"
    done, log = _run_prologue(tmp_path, "".join(lines.values()), ARGV)
    assert done.returncode == driver.CREDENTIAL_MISSING_EXIT, done.stderr
    assert f"{driver.CREDENTIAL_MISSING_MARKER} {name}" in done.stderr
    # rpk was never reached with a guessed value, and no value reached an output.
    assert not _calls(log)
    combined = done.stdout + done.stderr
    assert SENTINEL_USER not in combined
    assert SENTINEL_PASS not in combined
    assert value.strip("\"'") not in combined


def test_dollar_and_backslash_inside_single_quotes_are_literal_in_the_shell_and_read_as_such(
    tmp_path: Path,
) -> None:
    """Positive control: single quotes make ``$`` and a backslash literal for the shell, so they are read."""
    text = "DEV_KAFKA_SASL_USERNAME='u$1'\nDEV_KAFKA_SASL_PASSWORD='p\\2'\n"
    done, log = _run_prologue(tmp_path, text, ARGV)
    assert done.returncode == 0, done.stderr
    assert log.read_text(encoding="utf-8").rstrip().endswith("-X user=u$1 -X pass=p\\2")
    sourced = subprocess.run(
        [
            "sh",
            "-c",
            '. "$1"; printf "%s\\n%s" "$DEV_KAFKA_SASL_USERNAME" "$DEV_KAFKA_SASL_PASSWORD"',
            "sh",
            str(tmp_path / "operator.env"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert sourced.stdout == "u$1\np\\2"


@pytest.mark.parametrize(
    "missing", ["DEV_KAFKA_SASL_USERNAME", "DEV_KAFKA_SASL_PASSWORD"]
)
def test_missing_name_fails_closed_naming_the_key_and_no_value(
    tmp_path: Path, missing: str
) -> None:
    lines = {
        "DEV_KAFKA_SASL_USERNAME": f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\n",
        "DEV_KAFKA_SASL_PASSWORD": f"DEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n",
    }
    del lines[missing]
    done, log = _run_prologue(tmp_path, "".join(lines.values()), ARGV)
    assert done.returncode == driver.CREDENTIAL_MISSING_EXIT
    assert f"{driver.CREDENTIAL_MISSING_MARKER} {missing}" in done.stderr
    # rpk was never reached, and the container the decoy sits in was never asked.
    assert not _calls(log)
    _assert_no_container_lookup(log, done.stdout, done.stderr)
    combined = done.stdout + done.stderr
    assert SENTINEL_PASS not in combined
    assert SENTINEL_USER not in combined


def test_empty_value_counts_as_missing(tmp_path: Path) -> None:
    done, log = _run_prologue(
        tmp_path,
        f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\nDEV_KAFKA_SASL_PASSWORD=\n",
        ARGV,
    )
    assert done.returncode == driver.CREDENTIAL_MISSING_EXIT
    assert "DEV_KAFKA_SASL_PASSWORD" in done.stderr
    assert not _calls(log)
    assert SENTINEL_USER not in done.stdout + done.stderr


def test_absent_env_file_fails_closed_naming_the_file_not_a_container(
    tmp_path: Path,
) -> None:
    done, log = _run_prologue(tmp_path, None, ARGV)
    assert done.returncode == driver.CREDENTIAL_MISSING_EXIT
    assert driver.CREDENTIAL_MISSING_MARKER in done.stderr
    assert "operator.env" in done.stderr
    assert "container" not in done.stderr
    assert not _calls(log)
    _assert_no_container_lookup(log, done.stdout, done.stderr)


def test_lab_broker_acls_exit_3_with_the_marker_raises_the_typed_error_through_run_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, log = _run_remote_locally(
        monkeypatch,
        tmp_path,
        f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\n",
        ARGV,
    )
    with pytest.raises(driver.BrokerCredentialMissingError) as raised:
        driver.run_remote("lab-target", script)
    message = str(raised.value)
    assert message.startswith(driver.CREDENTIAL_MISSING_MARKER)
    assert "DEV_KAFKA_SASL_PASSWORD" in message
    assert SENTINEL_USER not in message
    assert SENTINEL_PASS not in message
    assert not _calls(log)


def test_lab_broker_acls_username_missing_with_the_password_present_names_the_username_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The password is in the file here, so a message that carried it would fail this test."""
    script, log = _run_remote_locally(
        monkeypatch,
        tmp_path,
        f"DEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n",
        ARGV,
    )
    with pytest.raises(driver.BrokerCredentialMissingError) as raised:
        driver.run_remote("lab-target", script)
    message = str(raised.value)
    assert message.startswith(driver.CREDENTIAL_MISSING_MARKER)
    assert "DEV_KAFKA_SASL_USERNAME" in message
    assert "DEV_KAFKA_SASL_PASSWORD" not in message
    assert SENTINEL_PASS not in message
    assert not _calls(log)


@pytest.mark.parametrize("mode", ["apply", "dry_run"])
def test_the_reconcile_handler_surfaces_a_missing_credential_as_exit_2_with_the_marker(
    mode: Literal["apply", "dry_run"],
) -> None:
    """The reconcile node answers a missing credential with the marker line and exit 2, never a pass."""

    def missing(target: str, script: str) -> str:
        raise driver.BrokerCredentialMissingError(
            f"{driver.CREDENTIAL_MISSING_MARKER} DEV_KAFKA_SASL_PASSWORD missing or empty in /x/.env"
        )

    result = reconcile.HandlerBrokerAclReconcile(
        environ=ENV, remote_runner=missing
    ).handle(ModelBrokerAclReconcileRequest(mode=mode))
    assert result.exit_code == 2
    assert result.error is not None
    assert result.error.startswith(driver.CREDENTIAL_MISSING_MARKER)
    assert "DEV_KAFKA_SASL_PASSWORD" in result.error


@pytest.mark.parametrize(
    "stderr", ["", "rpk: boom\n", "no lane superuser credentials in x\n"]
)
def test_lab_broker_acls_exit_3_without_the_marker_is_not_a_missing_credential(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=driver.CREDENTIAL_MISSING_EXIT, stdout="", stderr=stderr
        )

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as raised:
        driver.run_remote("lab-target", "true")
    assert not isinstance(raised.value, driver.BrokerCredentialMissingError)
    assert "exit 3" in str(raised.value)


def test_lab_broker_acls_exit_3_from_a_command_after_the_prologue_carries_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tail command that exits 3 with the credential present is a plain failure, by the script's own stderr."""
    script, log = _run_remote_locally(
        monkeypatch,
        tmp_path,
        f"DEV_KAFKA_SASL_USERNAME={SENTINEL_USER}\nDEV_KAFKA_SASL_PASSWORD={SENTINEL_PASS}\n",
        ARGV,
        docker_exit=driver.CREDENTIAL_MISSING_EXIT,
    )
    with pytest.raises(RuntimeError) as raised:
        driver.run_remote("lab-target", script)
    assert not isinstance(raised.value, driver.BrokerCredentialMissingError)
    assert "exit 3" in str(raised.value)
    assert driver.CREDENTIAL_MISSING_MARKER not in str(raised.value)
    assert SENTINEL_PASS not in str(raised.value)
    assert _calls(log)  # the credential was read and rpk was reached


def test_declared_env_file_default_is_the_operator_env_file() -> None:
    assert _connection().superuser_env_file == "~/.omnibase/.env"
    script = driver.build_remote_script(_connection(), ARGV)
    assert '"$HOME"/.omnibase/.env' in script


@pytest.mark.parametrize("mode", ["apply", "dry_run"])
def test_superuser_env_file_declared_cell_reaches_every_reconcile_script(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The reconcile node's contract declaration carries the cell to every script it sends, per mode."""
    real_contract = reconcile.contract
    declared = real_contract()["broker_acl_declaration"]["superuser_env_file"]
    assert declared == "~/.omnibase/.env"  # the shipped declaration

    def with_custom_cell() -> dict[str, object]:
        loaded = real_contract()
        loaded["broker_acl_declaration"]["superuser_env_file"] = (
            "/srv/custom/operator.env"
        )
        return loaded

    remote = FakeRemote()
    monkeypatch.setattr(reconcile, "contract", with_custom_cell)
    reconcile.HandlerBrokerAclReconcile(environ=ENV, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode=mode)
    )
    assert remote.scripts
    for script in remote.scripts:
        assert "F=/srv/custom/operator.env" in script.splitlines()
        assert 'F="$HOME"/.omnibase/.env' not in script
