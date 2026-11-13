# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker's topics: from given observations (the in-memory default) or its public metrics over ssh.

The ssh backend reads the broker's Prometheus text inside its container, the same read the broker load
reading performs, and keeps one observation per user topic: its partitions, the sum of the partitions'
high watermarks, the consumer groups that touch it, and the bytes produced to it. A topic without a max
offset line is omitted, and a read that names no topic at all is an unreadable inventory.

The group evidence is the committed offsets: a group that subscribes to a topic and has committed nothing
(an empty topic gives it nothing to commit) is not visible in the public metrics. A real removal therefore
needs the broker's own group listing before it deletes; this read is the dry run's evidence only.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence

from omnimarket.models.model_topic_removal import (
    ModelTopicObservation,
)
from omnimarket.nodes.node_broker_load_reading_effect.adapters.metrics_source import (
    run_process,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelBrokerMetricsSource,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    TopicInventoryError,
)

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]

_LINE = re.compile(r"^(?P<name>[a-z_]+)(?:\{(?P<labels>[^}]*)\})? (?P<value>\S+)$")
_LABEL = re.compile(r'(\w+)="([^"]*)"')
_OFFSETS = "redpanda_kafka_max_offset"
_GROUPS = (
    "redpanda_kafka_consumer_group_committed_offset",
    "redpanda_kafka_consumer_group_topics",
)
_BYTES = "redpanda_kafka_request_bytes_total"


class InMemoryTopicInventory:
    """The broker says given observations, or nothing (an unreadable read)."""

    def __init__(self, observations: Sequence[ModelTopicObservation] | None) -> None:
        self.observations = tuple(observations) if observations is not None else None

    def read(self) -> tuple[ModelTopicObservation, ...]:
        if self.observations is None:
            raise TopicInventoryError("inventory-unreadable")
        return self.observations


def parse_topic_observations(text: str) -> tuple[ModelTopicObservation, ...]:
    """One observation per user topic out of the broker's Prometheus text.

    Raises ``TopicInventoryError`` when no topic carries a max offset line.
    """
    partitions: dict[str, set[str]] = {}
    watermarks: dict[str, int] = {}
    groups: dict[str, set[str]] = {}
    produced: dict[str, int] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        name = match["name"]
        if name not in (_OFFSETS, *_GROUPS, _BYTES):
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        labels = dict(_LABEL.findall(match["labels"] or ""))
        # The group series carry no namespace label; the offset and bytes series do.
        if name in (_OFFSETS, _BYTES) and labels.get("redpanda_namespace") != "kafka":
            continue
        topic = labels.get("redpanda_topic", "")
        if not topic or topic.startswith("_"):
            continue
        if name == _OFFSETS:
            partition = labels.get("redpanda_partition")
            if partition is None:
                continue
            partitions.setdefault(topic, set()).add(partition)
            watermarks[topic] = watermarks.get(topic, 0) + int(value)
        elif name in _GROUPS:
            group = labels.get("redpanda_group")
            if group is not None:
                groups.setdefault(topic, set()).add(group)
        elif labels.get("redpanda_request") == "produce":
            produced[topic] = produced.get(topic, 0) + int(value)
    if not partitions:
        raise TopicInventoryError("metrics-lack-topic-offsets")
    return tuple(
        ModelTopicObservation(
            name=topic,
            partitions=len(partitions[topic]),
            high_watermark_total=watermarks[topic],
            consumer_groups=tuple(sorted(groups.get(topic, ()))),
            produced_bytes=produced.get(topic, 0),
        )
        for topic in sorted(partitions)
    )


class SshPublicMetricsInventory:
    """Read the broker's topics from its public metrics inside its container, over ssh."""

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

    def read(self) -> tuple[ModelTopicObservation, ...]:
        source = self._source
        host = self._environ.get(source.ssh_host_env, "").strip()
        if not host:
            raise TopicInventoryError("ssh-host-variable-unset")
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
            raise TopicInventoryError("metrics-read-timeout") from None
        except OSError as exc:
            raise TopicInventoryError(
                f"metrics-read-unstartable-{type(exc).__name__}"
            ) from None
        if done.returncode:
            raise TopicInventoryError(f"metrics-read-exit-{done.returncode}")
        return parse_topic_observations(done.stdout.decode("utf-8", errors="replace"))


__all__ = [
    "InMemoryTopicInventory",
    "SshPublicMetricsInventory",
    "parse_topic_observations",
]
