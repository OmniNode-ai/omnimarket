# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker superuser credential comes from its declared secret source, by name (lab_broker_acls).

The prologue every broker ACL, topic-delete and principal-mint call shares used to read the dev
superuser credential from the env of an exited one-shot container, which a prune removed. It now
reads DEV_KAFKA_SASL_USERNAME and DEV_KAFKA_SASL_PASSWORD, by name, from the declared operator env
file on the broker host. These tests run the generated prologue under ``sh`` with a stub ``docker``
and a temp env file: no container exists, a missing name fails closed with a typed error naming
the key, and no value reaches stdout, stderr or an error message.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    driver_broker_acl as driver,
)

SENTINEL_USER = "svc-sentinel-user"
SENTINEL_PASS = "pw-sentinel-0123456789"  # synthetic test value, not a credential
ARGV = [["security", "acl", "list", "--allow-principal", "User:x"]]

STUB_DOCKER = """#!/bin/sh
if [ "$1" = "inspect" ]; then
  echo "docker inspect must not be called: no such container" >&2
  exit 1
fi
printf '%s\\n' "$*" >> "$STUB_DOCKER_LOG"
"""


def _connection(env_file: Path | None = None) -> driver.AclConnection:
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
    tmp_path: Path, env_text: str | None, argvs: list[list[str]]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the generated script locally with no container and a stub docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env_file = tmp_path / "operator.env"
    if env_text is not None:
        env_file.write_text(env_text, encoding="utf-8")
    log = tmp_path / "docker.log"
    script = driver.build_remote_script(_connection(env_file), argvs)
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
        },
    )
    return done, log


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
    assert SENTINEL_PASS not in done.stdout + done.stderr


def test_script_never_inspects_a_container() -> None:
    script = driver.build_remote_script(_connection(), ARGV)
    assert "docker inspect" not in script
    assert "omnibase-infra-redpanda-sasl-enable" not in script


def test_export_prefix_and_quotes_in_the_env_file_are_read(tmp_path: Path) -> None:
    env_text = (
        f'export DEV_KAFKA_SASL_USERNAME="{SENTINEL_USER}"\n'
        f"DEV_KAFKA_SASL_PASSWORD='{SENTINEL_PASS}'\n"
    )
    done, log = _run_prologue(tmp_path, env_text, ARGV)
    assert done.returncode == 0, done.stderr
    call = log.read_text(encoding="utf-8")
    assert f"user={SENTINEL_USER}" in call
    assert f"pass={SENTINEL_PASS}" in call


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
    assert missing in done.stderr
    assert not log.exists()  # rpk was never reached
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
    assert not log.exists()


def test_absent_env_file_fails_closed_naming_the_file_not_a_container(
    tmp_path: Path,
) -> None:
    done, log = _run_prologue(tmp_path, None, ARGV)
    assert done.returncode == driver.CREDENTIAL_MISSING_EXIT
    assert "operator.env" in done.stderr
    assert "container" not in done.stderr
    assert not log.exists()


def test_run_remote_raises_the_typed_error_on_a_missing_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=driver.CREDENTIAL_MISSING_EXIT,
            stdout="",
            stderr="DEV_KAFKA_SASL_PASSWORD missing in operator.env",
        )

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    with pytest.raises(driver.BrokerCredentialMissingError) as raised:
        driver.run_remote("lab-target", "true")
    assert "DEV_KAFKA_SASL_PASSWORD" in str(raised.value)


def test_declared_env_file_default_is_the_operator_env_file() -> None:
    assert _connection().superuser_env_file == "~/.omnibase/.env"
    script = driver.build_remote_script(_connection(), ARGV)
    assert '"$HOME"/.omnibase/.env' in script
