# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18012 -- a hermetic Redpanda broker that REQUIRES authentication.

VENDORED COPY (OMN-18205)
-------------------------
This file is a byte-for-byte copy of
``omnibase_infra/tests/integration/customer_path/redpanda_sasl_harness.py``
apart from this note. It is vendored rather than imported because it lives
under ``tests/``, which no distribution ships, so the sibling checkout this
repository's customer-path job already performs cannot reach it.

Before OMN-18205 the copy here was a PRE-FIX fork: 297 lines, ``localhost``
hardcoded in the advertised address, in the bootstrap string and in all three
admin invocations, a floating image tag instead of the digest pin, and no
declared-broker path. omnibase_infra fixed the runner-topology defect on
2026-09-07 in #3286 and this copy never received it, which is why this
repository's required broker gate could not run on a containerized runner four
days after the defect was closed next door. Keep the two files identical: a
divergence here is invisible until a flip exercises it.


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

RUNNER TOPOLOGY (OMN-18012, 2026-09-07)
--------------------------------------
The broker port is published into the **Docker host's** network namespace.
On a GitHub-hosted runner the test process is on that host and ``localhost``
is correct. On this org's self-hosted ``omnibase-ci`` runners it is not: those
runners are containers that reach the daemon through a mounted
``/var/run/docker.sock`` (``docker/docker-compose.runners.yml``, no
``networks:`` block, no ``network_mode: host``), so the published port is
ECONNREFUSED at ``localhost`` and reachable only at the Docker host address --
the container's own default route. 7/7 fleet runs of the required job failed
that way while 6/6 hosted runs passed. Same precedent, same fix as OMN-15567
in ``.github/workflows/reusable-runtime-boot.yml``.

Two consequences are baked in below and must stay:

* the resolved address is used for BOTH ``--advertise-kafka-addr`` and
  ``bootstrap``. Advertised metadata redirects every client, so fixing only
  the bootstrap string sends the client straight back to ``localhost``.
* readiness is proven on the CLIENT path (a TCP connect from THIS process),
  not only by ``rpk`` inside the broker container. The in-container probe
  answers from the one namespace where ``localhost`` is always right, which is
  why an unreachable broker previously reported ready and surfaced as an
  opaque aiokafka error inside three unrelated tests.

WHICH BROKER (operator ruling, 2026-09-07)
-----------------------------------------
This harness does NOT start a container on a host that already runs a broker.
``resolve_broker()`` is the single entry point and it resolves in this order:

1. **A DECLARED broker wins.** When ``OMN18012_BROKER_BOOTSTRAP`` is set the
   harness adopts that broker and starts nothing. The declaration is completed
   by ``OMN18012_BROKER_CONTAINER`` (the container on this Docker host that
   admin ``rpk`` is exec'd into) and the SASL credential triple. A declaration
   that is incomplete, unreachable, or not actually enforcing auth is a
   ``HarnessError`` -- it NEVER falls through to starting a container, because
   silently starting a private broker is how "the gate is green" stops meaning
   "the declared broker works".
2. **Only with nothing declared** does the harness start its own throwaway
   container -- the developer-laptop and GitHub-hosted-runner case -- and even
   then it advertises an address the CALLER can reach (see RUNNER TOPOLOGY
   above), never a hardcoded ``localhost``.

The SASL requirement does not soften in either mode: an adopted broker must
refuse an unauthenticated ``rpk`` before any test runs, and a declared broker
that accepts one fails the gate loudly naming omnibase_infra#3276 (the dev-lane
SASL flip) as the blocker. It is never skipped.

Why a GitHub Actions ``services:`` container is NOT the hosted-runner path:
``services:`` cannot override a container's command, and this image's default
``CMD`` is ``redpanda start --overprovisioned`` with ``enable_sasl`` off.
``ENABLE_DEFAULT_LISTENERS``/``RP_BOOTSTRAP_USER`` shape listeners and seed a
superuser but cannot turn SASL on -- ``--set redpanda.enable_sasl=true`` is
command-line only (verified 2026-09-07 by reading ``/entrypoint.sh`` out of
``redpandadata/redpanda:v24.2.7``). A service container would therefore be a
NO-AUTH broker, i.e. exactly the hole this gate exists to close.

Every credential in this module is a synthetic test constant. No real
credential appears in any test, fixture or compose file.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass, field

# Pinned by digest, not by a floating tag: the same job on two runners must
# resolve the same image. This is the multi-arch manifest-list digest of
# redpandadata/redpanda:v24.2.7 -- `.github/workflows/ci.yml` pre-pulls this
# exact reference and tests/unit/docker/test_omn18012_harness_runner_topology.py
# asserts the two never drift apart.
REDPANDA_IMAGE = (
    "redpandadata/redpanda:v24.2.7@sha256:"
    "82a69763bef8d8b55ea5a520fa1b38f993908ef68946819ca1aed43541824c48"
)

# A second, container-local listener. `rpk` runs with `docker exec` INSIDE the
# broker, so it must not be sent out to the host address and back in: that
# would make every topic/produce call depend on NAT hairpinning. This port is
# never published and lives only in the broker's own namespace.
INTERNAL_PORT = 9092

# Applied to every container this harness starts. Concurrent CI jobs share one
# Docker host; a leaked container has to be attributable.
HARNESS_LABEL = "com.omninode.omn18012-harness"

# How long to wait for teardown removal. Generous rather than tight: the fleet's
# Docker host is shared by up to 88 runner containers, and a removal that is slow
# because the daemon is busy is not a defect in anything this suite tests. A
# timeout here is reported and swallowed -- see ``stop_redpanda``.
REMOVE_TIMEOUT_S = 300

# docker's own wording when a host port is taken. `_free_port()` binds in the
# TEST PROCESS's namespace, which on a containerized runner is not the
# namespace the port is published into, so a collision is possible and is a
# deterministic, self-reported condition with a specific remedy: pick another
# port. Nothing else is retried.
_PORT_TAKEN = ("port is already allocated", "address already in use")
_MAX_PORT_ATTEMPTS = 5

# -- The declared-broker contract (operator ruling, 2026-09-07) -------------
# Setting the bootstrap variable is a statement that a broker ALREADY EXISTS.
# From that point the harness starts nothing and every failure is loud: an
# incomplete, unreachable or unauthenticated declaration raises rather than
# quietly falling back to a private container.
DECLARED_BOOTSTRAP_ENV = "OMN18012_BROKER_BOOTSTRAP"
DECLARED_CONTAINER_ENV = "OMN18012_BROKER_CONTAINER"
DECLARED_INTERNAL_ENV = "OMN18012_BROKER_INTERNAL_BOOTSTRAP"
DECLARED_USERNAME_ENV = "OMN18012_BROKER_SASL_USERNAME"
DECLARED_PASSWORD_ENV = "OMN18012_BROKER_SASL_PASSWORD"
DECLARED_MECHANISM_ENV = "OMN18012_BROKER_SASL_MECHANISM"

# An explicit advertise-host override for the container path, for a topology
# neither branch of `resolve_docker_host_address()` describes. The
# testcontainers name is honoured too so a developer who already exports it
# does not have to learn a second one.
ADVERTISE_HOST_ENV = "OMN18012_ADVERTISE_HOST"
TESTCONTAINERS_HOST_ENV = "TESTCONTAINERS_HOST_OVERRIDE"

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


def _read_cgroup() -> str:
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _self_container_id() -> str | None:
    """The container this process runs in, when it runs in one at all.

    Mirrors "Detect runner network topology" in
    ``.github/workflows/reusable-runtime-boot.yml``: try every 64-hex id in
    the cgroup file, then the hostname (cgroup v2 on these runners exposes no
    id, and the hostname IS the 12-hex container id -- the live failing logs
    show ``Machine name: '8f635a7e3c47'``), and let the daemon adjudicate.
    """
    candidates: list[str] = list(
        dict.fromkeys(re.findall(r"[0-9a-f]{64}", _read_cgroup()))
    )
    with contextlib.suppress(OSError):
        candidates.append(socket.gethostname())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    "{{.Id}}",
                    candidate,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def _default_route_gateway() -> str | None:
    """This container's default route -- the Docker host side of its bridge."""
    try:
        with open("/proc/net/route", encoding="utf-8") as route_file:
            next(route_file)
            for line in route_file:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, StopIteration, ValueError):
        return None
    return None


def resolve_docker_host_address() -> str:
    """An address for the Docker host's published ports, from THIS process.

    Deterministic and evaluated once: an explicit override wins; otherwise a
    bare runner is the Docker host and keeps ``localhost``, and a containerized
    runner gets its default route. When detection finds a container but no
    route to read, the answer stays ``localhost`` and the client-path readiness
    probe below turns that into a loud, topology-naming failure -- the harness
    never gropes for an address through a chain of broker restarts.
    """
    for name in (ADVERTISE_HOST_ENV, TESTCONTAINERS_HOST_ENV):
        override = os.environ.get(name, "").strip()
        if override:
            return override
    if _self_container_id() is None:
        return "localhost"
    return _default_route_gateway() or "localhost"


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Can THIS process open a TCP connection to the broker's external port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


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
    """An auth-required Redpanda broker -- adopted, or started by this harness.

    ``owned`` is the difference and it is not cosmetic: a broker this harness
    did not create is never torn down by it. ``internal_bootstrap`` is the
    address in-container ``rpk`` uses, which is a different namespace from the
    one ``bootstrap`` names.
    """

    container: str
    port: int
    host: str = "localhost"
    internal_bootstrap: str = field(default=f"localhost:{INTERNAL_PORT}")
    username: str = ""
    password: str = ""
    mechanism: str = ""
    owned: bool = True

    def __post_init__(self) -> None:
        # The synthetic constants are the defaults for the container path; a
        # declared broker supplies its own. Set through object.__setattr__
        # because the dataclass is frozen.
        if not self.username:
            object.__setattr__(self, "username", SASL_USERNAME)
        if not self.password:
            object.__setattr__(self, "password", SASL_PASSWORD)
        if not self.mechanism:
            object.__setattr__(self, "mechanism", SASL_MECHANISM)

    @property
    def bootstrap(self) -> str:
        """The address the TEST PROCESS reaches the external listener at."""
        return f"{self.host}:{self.port}"

    # -- rpk -------------------------------------------------------------
    def rpk(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run authenticated ``rpk`` inside the broker container."""
        cmd = [
            "docker",
            "exec",
            self.container,
            "rpk",
            *args,
            # In-container: the internal listener, which never leaves this
            # container's namespace. Never self.bootstrap -- that is the
            # runner-side address.
            "-X",
            f"brokers={self.internal_bootstrap}",
            "-X",
            f"user={self.username}",
            "-X",
            f"pass={self.password}",
            "-X",
            f"sasl.mechanism={self.mechanism}",
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
                f"brokers={self.internal_bootstrap}",
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
                f"brokers={self.internal_bootstrap}",
                "-X",
                f"user={self.username}",
                "-X",
                f"pass={self.password}",
                "-X",
                f"sasl.mechanism={self.mechanism}",
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
            "KAFKA_SASL_MECHANISM": self.mechanism,
            "KAFKA_SASL_USERNAME": self.username,
            "KAFKA_SASL_PASSWORD": self.password,
        }


def declared_bootstrap() -> str:
    """The declared broker's client address, or ``""`` when none is declared."""
    return os.environ.get(DECLARED_BOOTSTRAP_ENV, "").strip()


def _split_hostport(bootstrap: str, source: str) -> tuple[str, int]:
    host, _sep, port_text = bootstrap.rpartition(":")
    if (
        not host
        or not port_text.isdigit()
        or any(ch in bootstrap for ch in ",; \t")
        or ":" in host
    ):
        raise HarnessError(
            f"{source}={bootstrap!r} is not a single host:port. This harness "
            f"probes the declared broker on the client path before any test "
            f"runs, so it needs one address it can open a socket to -- a "
            f"comma-separated list has no single socket to prove."
        )
    return host, int(port_text)


def adopt_declared_broker() -> RedpandaSasl:
    """Adopt the broker named by the environment. Starts nothing, ever.

    Four things are proven before the first test runs, and each failure raises
    a ``HarnessError`` naming what is wrong. None of them falls through to
    starting a container: a declaration that quietly became a private broker
    would make a green gate say nothing about the broker that was declared.
    """
    bootstrap = declared_bootstrap()
    if not bootstrap:
        raise HarnessError(f"{DECLARED_BOOTSTRAP_ENV} is not set")

    # 1. The declaration is complete. Credentials are never guessed: the
    #    harness's own synthetic constants belong to the container it starts,
    #    and silently trying them against somebody else's broker would turn a
    #    misconfiguration into an authentication failure with the wrong cause.
    missing = [
        name
        for name in (
            DECLARED_CONTAINER_ENV,
            DECLARED_USERNAME_ENV,
            DECLARED_PASSWORD_ENV,
        )
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise HarnessError(
            f"{DECLARED_BOOTSTRAP_ENV}={bootstrap!r} declares an existing "
            f"broker, but {', '.join(missing)} is unset. A declared broker is "
            f"completed by the environment or it is an error -- this harness "
            f"does not guess credentials and does not fall back to starting "
            f"its own container. {DECLARED_CONTAINER_ENV} is the container on "
            f"this Docker host that admin `rpk` is exec'd into."
        )

    host, port = _split_hostport(bootstrap, DECLARED_BOOTSTRAP_ENV)
    broker = RedpandaSasl(
        container=os.environ[DECLARED_CONTAINER_ENV].strip(),
        port=port,
        host=host,
        internal_bootstrap=(
            os.environ.get(DECLARED_INTERNAL_ENV, "").strip()
            or f"localhost:{INTERNAL_PORT}"
        ),
        username=os.environ[DECLARED_USERNAME_ENV].strip(),
        password=os.environ[DECLARED_PASSWORD_ENV],
        mechanism=(
            os.environ.get(DECLARED_MECHANISM_ENV, "").strip() or SASL_MECHANISM
        ),
        owned=False,
    )

    # 2. Reachable on the CLIENT path -- from this process, not from inside
    #    the broker. This is the exact check whose absence made OMN-18012's
    #    localhost harness report ready to a caller that could not connect.
    if not _tcp_reachable(broker.host, broker.port, timeout=10.0):
        raise HarnessError(
            f"{DECLARED_BOOTSTRAP_ENV}={bootstrap!r} declares a broker this "
            f"test process cannot open a TCP connection to. A declared broker "
            f"that is unreachable is a hard failure, never a fall-through to a "
            f"private container: the gate would then be green about a broker "
            f"nobody asked for. Resolved Docker host address for reference: "
            f"{resolve_docker_host_address()!r}."
        )

    # 3. The declared broker actually ENFORCES auth. Without this the whole
    #    module is vacuous -- the gate exists to prove the REJECTION exists.
    anonymous = broker.rpk_unauthenticated("cluster", "info")
    if anonymous.returncode == 0:
        raise HarnessError(
            f"the declared broker at {bootstrap} accepted an UNAUTHENTICATED "
            f"rpk client, so it is not enforcing SASL and this gate cannot "
            f"prove that escape 1's plaintext client is refused. This is a "
            f"FAILURE, not a skip: the dev-lane SASL flip is "
            f"omnibase_infra#3276 -- until the declared broker enforces auth, "
            f"declare a broker that does, or unset "
            f"{DECLARED_BOOTSTRAP_ENV} and let the harness start its own. "
            f"anonymous rpk said: {anonymous.stdout}\n{anonymous.stderr}"
        )
    # 4. The supplied credentials actually authenticate.
    authed = broker.rpk("cluster", "info", check=False)
    if authed.returncode != 0:
        raise HarnessError(
            f"the declared broker at {bootstrap} refused the credentials in "
            f"{DECLARED_USERNAME_ENV}/{DECLARED_PASSWORD_ENV} "
            f"(mechanism {broker.mechanism}), or "
            f"{DECLARED_CONTAINER_ENV}={broker.container!r} / "
            f"{DECLARED_INTERNAL_ENV}={broker.internal_bootstrap!r} do not "
            f"name a reachable in-container listener: "
            f"rc={authed.returncode} {authed.stdout}\n{authed.stderr}"
        )

    return broker


def resolve_broker(*, boot_timeout_s: float = 120.0) -> RedpandaSasl:
    """The single entry point: adopt a declared broker, else start one.

    The operator ruling of 2026-09-07 is the whole shape of this function --
    do not start a container on a host that already runs a broker.
    """
    if declared_bootstrap():
        return adopt_declared_broker()
    return start_redpanda_sasl(boot_timeout_s=boot_timeout_s)


def _docker_run(
    container: str, port: int, host: str
) -> subprocess.CompletedProcess[str]:
    """One `docker run` of the auth-required broker on a chosen host port."""
    return subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--label",
            f"{HARNESS_LABEL}=1",
            "-p",
            f"{port}:{port}",
            "-e",
            f"RP_BOOTSTRAP_USER={SASL_USERNAME}:{SASL_PASSWORD}:{SASL_MECHANISM}",
            REDPANDA_IMAGE,
            "redpanda",
            "start",
            # Two listeners, deliberately. `internal` is container-local and is
            # what in-container `rpk` uses. `external` is the published port,
            # and it must ADVERTISE the runner-reachable host: advertised
            # metadata is what redirects every client, so advertising
            # "localhost" here is exactly the OMN-18012 failure regardless of
            # what bootstrap string the caller passes.
            f"--kafka-addr=internal://0.0.0.0:{INTERNAL_PORT},external://0.0.0.0:{port}",
            f"--advertise-kafka-addr=internal://localhost:{INTERNAL_PORT},external://{host}:{port}",
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


def start_redpanda_sasl(
    *, boot_timeout_s: float = 120.0, host: str | None = None
) -> RedpandaSasl:
    """Start an auth-required Redpanda and return it once a CLIENT can reach it.

    Raises rather than skips: a harness that cannot come up is a red test,
    not an absent one. A silently-skipped boundary test is exactly how the
    2026-09-06 escapes stayed invisible.
    """
    resolved_host = host if host is not None else resolve_docker_host_address()

    container = ""
    proc: subprocess.CompletedProcess[str] | None = None
    port = 0
    for _attempt in range(_MAX_PORT_ATTEMPTS):
        port = _free_port()
        container = f"omn18012-rp-{uuid.uuid4().hex[:10]}"
        proc = _docker_run(container, port, resolved_host)
        if proc.returncode == 0:
            break
        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        if not any(marker in combined for marker in _PORT_TAKEN):
            # Not a port collision. Bounded, cause-specific reselection only --
            # a blind retry loop would convert a real defect into a slow flake.
            break
        # _free_port() picked in this process's namespace; the port is
        # published into the Docker HOST's, where it was already taken.
        # Drop the half-created container and reselect.
        subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True,
            timeout=120,
            check=False,
        )

    if proc is None or proc.returncode != 0:
        detail = (
            "no attempt was made" if proc is None else f"{proc.stdout}\n{proc.stderr}"
        )
        raise HarnessError(f"docker run failed: {detail}")

    broker = RedpandaSasl(container=container, port=port, host=resolved_host)
    try:
        return _await_ready(broker, boot_timeout_s)
    except BaseException:
        # Never leak a container on a shared Docker host, on any failure path.
        stop_redpanda(broker)
        raise


def _await_ready(broker: RedpandaSasl, boot_timeout_s: float) -> RedpandaSasl:
    """Ready means BOTH: the broker answers, and this process can connect.

    The `rpk` half runs inside the broker container, which is the one
    namespace where `localhost:<port>` is always right -- on its own it
    reported a broker no client could reach as ready (OMN-18012). The TCP half
    is the client path the tests actually use. Neither substitutes for the
    other: a TCP accept from a half-started broker is not readiness either.
    """
    deadline = time.monotonic() + boot_timeout_s
    last = ""
    broker_answered = False
    while True:
        probe = broker.rpk("cluster", "info", check=False)
        if probe.returncode == 0:
            broker_answered = True
            if _tcp_reachable(broker.host, broker.port):
                return broker
        else:
            last = f"{probe.stdout}\n{probe.stderr}"
        if time.monotonic() >= deadline:
            break
        time.sleep(2)

    logs = subprocess.run(
        ["docker", "logs", "--tail", "40", broker.container],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if broker_answered:
        raise HarnessError(
            f"the broker answered `rpk cluster info` inside its own container, "
            f"but {broker.bootstrap} is not reachable from this test process. "
            f"The broker port is published into the Docker HOST's network "
            f"namespace; this process is in a different network namespace (a "
            f"containerized CI runner reaching the daemon through a mounted "
            f"docker.sock), so the published port is unreachable at that "
            f"address. Resolved Docker host address: {broker.host!r} "
            f"(container id of self: {_self_container_id()!r}, default route: "
            f"{_default_route_gateway()!r}). See OMN-18012, and the same fix "
            f"for the nightly stack in OMN-15567.\n"
            f"container logs: {logs.stdout}\n{logs.stderr}"
        )
    raise HarnessError(
        f"redpanda did not become ready in {boot_timeout_s}s. "
        f"last rpk: {last}\ncontainer logs: {logs.stdout}\n{logs.stderr}"
    )


def stop_redpanda(broker: RedpandaSasl) -> None:
    """Remove a broker THIS harness started. An adopted one is left alone.

    Tearing down a broker the harness did not create is how a test suite takes
    down the lane it was handed.

    CLEANUP NEVER FAILS THE SESSION (OMN-18205). Measured 2026-09-12T01:29:59Z on
    omnimarket fleet run 34664495108: all four boundary tests PASSED and the job
    still went red, because ``docker rm -f`` exceeded 120 s during teardown while
    the shared Docker host was at 88 of 88 runners busy. Removal is not an
    assertion about the system under test, so a slow or failed removal is
    reported and swallowed rather than raised as a teardown error that reads,
    from the check list, exactly like a boundary defect.

    The leak is attributable rather than silent: every container this harness
    starts carries the ``HARNESS_LABEL``, so anything left behind is findable
    with one ``docker ps`` filter, and the message below names both the
    container and that filter.
    """
    if not broker.owned:
        return
    try:
        subprocess.run(
            ["docker", "rm", "-f", broker.container],
            capture_output=True,
            timeout=REMOVE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            f"WARNING: `docker rm -f {broker.container}` exceeded "
            f"{REMOVE_TIMEOUT_S}s and was abandoned. The tests already ran; this "
            "is cleanup, not a result. Find anything left behind with: "
            f"docker ps -a --filter label={HARNESS_LABEL}=1"
        )
    except OSError as error:
        print(
            f"WARNING: `docker rm -f {broker.container}` could not be executed "
            f"({error}). Find anything left behind with: "
            f"docker ps -a --filter label={HARNESS_LABEL}=1"
        )
