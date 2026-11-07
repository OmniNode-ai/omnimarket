# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker's counts: from given values (the in-memory default) or its public metrics over ssh.

The ssh backend asks the broker's own container for its Prometheus text with ``docker exec`` and keeps
only counts: cluster topics and partitions, the shards that report CPU, and the user-topic partitions
whose log end offset is zero. No topic name and no value of any record leaves this module.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence

from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    ModelBrokerMetrics,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelBrokerMetricsSource,
)
from omnimarket.nodes.node_broker_load_reading_effect.protocols import (
    BrokerMetricsError,
)

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]

_LINE = re.compile(r"^(?P<name>[a-z_]+)(?:\{(?P<labels>[^}]*)\})? (?P<value>\S+)$")
_LABEL = re.compile(r'(\w+)="([^"]*)"')


class InMemoryBrokerMetrics:
    """The broker says given counts, or nothing (an unreadable read)."""

    def __init__(self, metrics: ModelBrokerMetrics | None) -> None:
        self.metrics = metrics

    def read(self) -> ModelBrokerMetrics:
        if self.metrics is None:
            raise BrokerMetricsError("metrics-unreadable")
        return self.metrics


def parse_public_metrics(text: str) -> ModelBrokerMetrics:
    """Counts out of the broker's Prometheus text; raises ``BrokerMetricsError`` if one is absent."""
    topics: int | None = None
    partitions: int | None = None
    shards: set[str] = set()
    empty = 0
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        name = match["name"]
        try:
            value = float(match["value"])
        except ValueError:
            continue
        labels = dict(_LABEL.findall(match["labels"] or ""))
        if name == "redpanda_cluster_topics":
            topics = int(value)
        elif name == "redpanda_cluster_partitions":
            partitions = int(value)
        elif name == "redpanda_cpu_busy_seconds_total" and "shard" in labels:
            shards.add(labels["shard"])
        elif (
            name == "redpanda_kafka_max_offset"
            and labels.get("redpanda_namespace") == "kafka"
            and not labels.get("redpanda_topic", "").startswith("_")
            and value == 0
        ):
            empty += 1
    if topics is None or partitions is None:
        raise BrokerMetricsError("metrics-lack-cluster-counts")
    if not shards:
        raise BrokerMetricsError("metrics-lack-shards")
    return ModelBrokerMetrics(
        topics=topics,
        partitions=partitions,
        shards=len(shards),
        empty_partitions=empty,
    )


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this adapter; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


class SshBrokerMetrics:
    """Read the broker's public metrics inside its container, over ssh."""

    def __init__(
        self,
        source: ModelBrokerMetricsSource,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._source = source
        self._environ = environ
        self._run = run

    def _script(self) -> str:
        source = self._source
        return "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={source.compose_project} --filter "
                f"label=com.docker.compose.service={source.service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'docker exec -i "$RT" curl -s --max-time 20 {source.metrics_url}',
            ]
        )

    def read(self) -> ModelBrokerMetrics:
        source = self._source
        host = self._environ.get(source.ssh_host_env, "").strip()
        if not host:
            raise BrokerMetricsError("ssh-host-variable-unset")
        try:
            done = self._run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    host,
                    "bash -s",
                ],
                self._script().encode(),
                source.read_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise BrokerMetricsError("metrics-read-timeout") from None
        except OSError as exc:
            raise BrokerMetricsError(
                f"metrics-read-unstartable-{type(exc).__name__}"
            ) from None
        if done.returncode:
            raise BrokerMetricsError(f"metrics-read-exit-{done.returncode}")
        return parse_public_metrics(done.stdout.decode("utf-8", errors="replace"))


__all__ = [
    "InMemoryBrokerMetrics",
    "SshBrokerMetrics",
    "parse_public_metrics",
    "run_process",
]
