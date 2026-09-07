# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18012 -- a hermetic Redpanda broker that REQUIRES authentication.

Why this exists
---------------
Every local proof lane in this org runs a no-auth broker: the dev lane on
.201, ``docker/docker-compose.e2e.yml`` here, and every unit test's mock.
Broker authentication is therefore a boundary that exists only in staging and
prod, and a client that silently opens PLAINTEXT against an auth-required
listener passes every local check and fails in front of a customer. That is
escape 1 of the 2026-09-06 set (omnimarket ``_build_event_bus`` without
``apply_environment_overrides`` -> PLAINTEXT to the IAM-only MSK listener ->
every BYOK registration 503).

RESIDUAL, stated plainly and repeated in every module that uses this harness:
staging and prod are MSK with ``AWS_MSK_IAM`` + ``SASL_SSL``. This harness is
SCRAM-SHA-256 over ``SASL_PLAINTEXT``. What it proves is

    "this client did not silently open PLAINTEXT against an auth-required
    listener"

and NOT "this client speaks IAM". The mechanism differs; the *failure mode*
being pinned -- a client constructed with no credentials at all -- does not.

Every credential in this module is a synthetic test constant. No real
credential appears in any test, fixture or compose file.
"""

from __future__ import annotations

import socket
import subprocess
import time
import uuid
from dataclasses import dataclass

REDPANDA_IMAGE = "redpandadata/redpanda:v24.2.7"

# Synthetic test credentials. Not a secret; never used outside a throwaway
# container that is destroyed at the end of the test session.
SASL_USERNAME = "omn18012-harness"
SASL_PASSWORD = "omn18012-synthetic-not-a-real-secret"
SASL_MECHANISM = "SCRAM-SHA-256"
SECURITY_PROTOCOL = "SASL_PLAINTEXT"


class HarnessError(RuntimeError):
    """The harness could not be brought up. Never swallowed into a skip."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker_available() -> bool:
    """True when a docker daemon is reachable."""
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=30,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class RedpandaSasl:
    """A running, auth-required Redpanda broker."""

    container: str
    port: int

    @property
    def bootstrap(self) -> str:
        return f"localhost:{self.port}"

    # -- rpk -------------------------------------------------------------
    def rpk(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run authenticated ``rpk`` inside the broker container."""
        cmd = [
            "docker",
            "exec",
            self.container,
            "rpk",
            *args,
            "-X",
            f"brokers=localhost:{self.port}",
            "-X",
            f"user={SASL_USERNAME}",
            "-X",
            f"pass={SASL_PASSWORD}",
            "-X",
            f"sasl.mechanism={SASL_MECHANISM}",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
        if check and proc.returncode != 0:
            raise HarnessError(
                f"rpk {' '.join(args)} failed rc={proc.returncode}: "
                f"{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def rpk_unauthenticated(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``rpk`` with NO credentials -- the positive control.

        A harness whose listener is not actually enforcing auth would make
        every 'the client authenticated' assertion in this suite vacuous.
        """
        return subprocess.run(
            [
                "docker",
                "exec",
                self.container,
                "rpk",
                *args,
                "-X",
                f"brokers=localhost:{self.port}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def create_topic(self, name: str, *, partitions: int = 1) -> None:
        self.rpk("topic", "create", name, "-p", str(partitions), "-r", "1")

    def produce(self, topic: str, payloads: list[str]) -> None:
        """Produce one record per payload (newline-delimited)."""
        body = "".join(f"{p}\n" for p in payloads)
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container,
                "rpk",
                "topic",
                "produce",
                topic,
                "-X",
                f"brokers=localhost:{self.port}",
                "-X",
                f"user={SASL_USERNAME}",
                "-X",
                f"pass={SASL_PASSWORD}",
                "-X",
                f"sasl.mechanism={SASL_MECHANISM}",
            ],
            input=body,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            raise HarnessError(
                f"produce to {topic} failed rc={proc.returncode}: "
                f"{proc.stdout}\n{proc.stderr}"
            )

    def offsets(self, topic: str, partition: int = 0) -> tuple[int, int]:
        """Return ``(log_start, high_watermark)`` for one partition via rpk.

        Parsed from the ``rpk topic describe -p`` table; ``rpk`` in
        v24.2.7 has no ``--format json`` for this subcommand. The tests
        additionally re-assert the log start through ``aiokafka`` itself,
        because the client library's view is the one that matters.
        """
        proc = self.rpk("topic", "describe", topic, "-p")
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 6 and fields[0].isdigit() and int(fields[0]) == partition:
                return int(fields[4]), int(fields[5])
        raise HarnessError(
            f"partition {partition} absent from rpk describe of {topic}: {proc.stdout}"
        )

    def trim_prefix(self, topic: str, offset: int, partition: int = 0) -> None:
        """Advance the partition's log start. Verified, never assumed."""
        self.rpk(
            "topic",
            "trim-prefix",
            topic,
            "--offset",
            str(offset),
            "--partitions",
            str(partition),
            "--no-confirm",
        )
        log_start, _high = self.offsets(topic, partition)
        if log_start < offset:
            raise HarnessError(
                f"trim-prefix did not take on {topic}/{partition}: "
                f"log start is {log_start}, expected >= {offset}"
            )

    # -- env ---------------------------------------------------------------
    def env(self) -> dict[str, str]:
        """The KAFKA_* environment a client needs to reach this broker."""
        return {
            "KAFKA_BOOTSTRAP_SERVERS": self.bootstrap,
            "KAFKA_SECURITY_PROTOCOL": SECURITY_PROTOCOL,
            "KAFKA_SASL_MECHANISM": SASL_MECHANISM,
            "KAFKA_SASL_USERNAME": SASL_USERNAME,
            "KAFKA_SASL_PASSWORD": SASL_PASSWORD,
        }


def start_redpanda_sasl(*, boot_timeout_s: float = 120.0) -> RedpandaSasl:
    """Start an auth-required Redpanda and return it once it answers rpk.

    Raises rather than skips: a harness that cannot come up is a red test,
    not an absent one. A silently-skipped boundary test is exactly how the
    2026-09-06 escapes stayed invisible.
    """
    port = _free_port()
    container = f"omn18012-rp-{uuid.uuid4().hex[:10]}"
    proc = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{port}:{port}",
            "-e",
            f"RP_BOOTSTRAP_USER={SASL_USERNAME}:{SASL_PASSWORD}:{SASL_MECHANISM}",
            REDPANDA_IMAGE,
            "redpanda",
            "start",
            f"--kafka-addr=external://0.0.0.0:{port}",
            f"--advertise-kafka-addr=external://localhost:{port}",
            "--mode=dev-container",
            "--smp=1",
            "--memory=1G",
            "--reserve-memory=0M",
            "--reactor-backend=epoll",
            "--default-log-level=warn",
            "--set",
            "redpanda.enable_sasl=true",
            "--set",
            f"redpanda.superusers=['{SASL_USERNAME}']",
            # Small segments so a trim-prefix has something to reclaim and a
            # retention-truncated partition is reachable in a test.
            "--set",
            "redpanda.log_segment_size=1048576",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise HarnessError(f"docker run failed: {proc.stdout}\n{proc.stderr}")

    broker = RedpandaSasl(container=container, port=port)
    deadline = time.monotonic() + boot_timeout_s
    last = ""
    while time.monotonic() < deadline:
        probe = broker.rpk("cluster", "info", check=False)
        if probe.returncode == 0:
            return broker
        last = f"{probe.stdout}\n{probe.stderr}"
        time.sleep(2)
    logs = subprocess.run(
        ["docker", "logs", "--tail", "40", container],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    stop_redpanda(broker)
    raise HarnessError(
        f"redpanda did not become ready in {boot_timeout_s}s. "
        f"last rpk: {last}\ncontainer logs: {logs.stdout}\n{logs.stderr}"
    )


def stop_redpanda(broker: RedpandaSasl) -> None:
    subprocess.run(
        ["docker", "rm", "-f", broker.container],
        capture_output=True,
        timeout=120,
        check=False,
    )
