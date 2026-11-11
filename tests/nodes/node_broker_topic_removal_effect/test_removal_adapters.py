# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The adapters of the broker topic removal (OMN-23697): the broker's Prometheus text to its
topic observations, the ssh read on a given run, and the contract files to their declared and
published topics.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    ContractFileDeclaredTopics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.topic_inventory import (
    SshPublicMetricsInventory,
    parse_topic_observations,
)
from omnimarket.nodes.node_broker_topic_removal_effect.models.model_broker_topic_removal import (
    ModelTopicObservation,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelContractSource,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
    TopicInventoryError,
)
from tests.nodes.node_broker_topic_removal_effect.removal_support import overlay

pytestmark = pytest.mark.unit

SOURCE = overlay().metrics_source

#: Two user topics and one internal: one produced with two partitions and a consumer group, one
#: empty, and one the parser must skip because it is broker-internal.
METRICS_TEXT = """\
redpanda_kafka_max_offset{redpanda_namespace="kafka", redpanda_partition="0", redpanda_topic="onex.evt.x.v1"} 42
redpanda_kafka_max_offset{redpanda_namespace="kafka", redpanda_partition="1", redpanda_topic="onex.evt.x.v1"} 7
redpanda_kafka_consumer_group_committed_offset{redpanda_group="g1",redpanda_partition="0",redpanda_topic="onex.evt.x.v1",shard="0"} 40
redpanda_kafka_max_offset{redpanda_namespace="kafka", redpanda_partition="0", redpanda_topic="onex.evt.y.v1"} 0
redpanda_kafka_max_offset{redpanda_namespace="kafka", redpanda_partition="0", redpanda_topic="_schemas"} 5
redpanda_kafka_request_bytes_total{redpanda_namespace="kafka", redpanda_request="produce", redpanda_topic="onex.evt.x.v1"} 100
"""

EXPECTED = (
    ModelTopicObservation(
        name="onex.evt.x.v1",
        partitions=2,
        high_watermark_total=49,
        consumer_groups=("g1",),
        produced_bytes=100,
    ),
    ModelTopicObservation(
        name="onex.evt.y.v1",
        partitions=1,
        high_watermark_total=0,
        consumer_groups=(),
        produced_bytes=0,
    ),
)


def test_parse_topic_observations_reads_partitions_watermarks_groups_and_bytes() -> (
    None
):
    assert parse_topic_observations(METRICS_TEXT) == EXPECTED


def test_a_text_without_any_max_offset_line_is_an_unreadable_inventory() -> None:
    with pytest.raises(TopicInventoryError, match="metrics-lack-topic-offsets"):
        parse_topic_observations(
            'redpanda_kafka_consumer_group_committed_offset{redpanda_group="g1",redpanda_partition="0",redpanda_topic="onex.evt.x.v1",shard="0"} 40\n'
        )


class _Run:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[Sequence[str], bytes | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, stdin))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout, b""
        )


def test_the_ssh_inventory_reads_the_brokers_metrics_and_parses_them() -> None:
    run = _Run(METRICS_TEXT.encode("utf-8"))
    inventory = SshPublicMetricsInventory(
        SOURCE, {SOURCE.ssh_host_env: "example-host"}, run
    )
    assert inventory.read() == EXPECTED
    argv, stdin = run.calls[0]
    assert "example-host" in argv
    assert stdin is not None
    assert SOURCE.compose_project.encode() in stdin
    assert SOURCE.service.encode() in stdin
    assert SOURCE.metrics_url.encode() in stdin


def test_a_non_zero_exit_is_a_typed_unreadable_inventory() -> None:
    with pytest.raises(TopicInventoryError, match="metrics-read-exit-1"):
        SshPublicMetricsInventory(
            SOURCE, {SOURCE.ssh_host_env: "host"}, _Run(b"", 1)
        ).read()


def test_a_missing_ssh_host_variable_is_a_typed_unreadable_inventory() -> None:
    with pytest.raises(TopicInventoryError, match="ssh-host-variable-unset"):
        SshPublicMetricsInventory(SOURCE, {}, _Run()).read()


def _source() -> ModelContractSource:
    return overlay().contracts.model_copy(
        update={
            "registry_root_env": "TEST_REGISTRY_ROOT",
            "repositories": ("repo-one", "repo-two"),
        }
    )


def test_the_contract_files_give_the_declared_and_the_published_topics(
    tmp_path: Path,
) -> None:
    one = tmp_path / "repo-one/contracts/one"
    one.mkdir(parents=True)
    (one / "contract.yaml").write_text(
        "name: contract-one\nevent_bus:\n  publish_topics:\n    - onex.evt.one.v1\n",
        encoding="utf-8",
    )
    bad = tmp_path / "repo-one/contracts/bad"
    bad.mkdir(parents=True)
    (bad / "contract.yaml").write_text(
        "name: [unclosed\n  bad: {{{\n", encoding="utf-8"
    )
    two = tmp_path / "repo-two/contracts/two"
    two.mkdir(parents=True)
    (two / "contract.yaml").write_text(
        "name: contract-two\nevent_bus:\n  subscribe_topics:\n    - onex.evt.two.v1\n",
        encoding="utf-8",
    )
    declared = ContractFileDeclaredTopics(
        _source(), {"TEST_REGISTRY_ROOT": str(tmp_path)}
    )
    assert declared.declared() == {
        "onex.evt.one.v1": ("contract-one",),
        "onex.evt.two.v1": ("contract-two",),
    }
    assert declared.producers() == {"onex.evt.one.v1": ("contract-one",)}


def test_a_missing_registry_root_is_a_typed_unreadable_declarations() -> None:
    with pytest.raises(DeclaredTopicsError, match="registry-root-variable-unset"):
        ContractFileDeclaredTopics(_source(), {})


def test_a_group_series_without_a_namespace_label_still_counts_as_a_consumer_group() -> (
    None
):
    """The broker writes its group series with no namespace label; reading only labelled series misses every group."""
    text = (
        'redpanda_kafka_max_offset{redpanda_namespace="kafka",redpanda_partition="0",redpanda_topic="onex.evt.z.v1"} 0\n'
        'redpanda_kafka_consumer_group_committed_offset{redpanda_group="gz",redpanda_partition="0",redpanda_topic="onex.evt.z.v1",shard="0"} 0\n'
    )
    (observation,) = parse_topic_observations(text)
    assert observation.consumer_groups == ("gz",)
    assert observation.high_watermark_total == 0
