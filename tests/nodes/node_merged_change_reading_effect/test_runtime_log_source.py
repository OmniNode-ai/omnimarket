# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime log reader: real log lines in, loaded nodes, joined consumers and published results out."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrRunner,
)
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_merged_change_reading_effect.handlers.handler_merged_change_reading_effect import (
    HandlerMergedChangeReadingEffect,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrRunRequest,
    ModelMcrUnitHost,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_in_memory import (
    SourceInMemoryChangeReading,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_runtime_log import (
    SourceRuntimeFacts,
    parse_runtime_log,
    parse_unit_state,
)

from .support import LOG_TEXT, config, labels

OVERLAY = ModelRuntimeHostReadOverlay.model_validate(
    yaml.safe_load(
        """
ssh_host_env: TEST_RUNTIME_SSH
compose_project: proj
fleet_service: svc
provenance_service: svc
provenance_path: /app/p.json
repo_refs:
  - {repo: omnimarket, clone_relpath: omnimarket, ref_path: [a]}
"""
    )
)


def test_parse_real_log_lines() -> None:
    facts = parse_runtime_log(LOG_TEXT, labels())
    assert [n.node for n in facts.loaded_nodes] == [
        "node_landing_canary_scheduled_effect",
        "node_start_path_census_compute",
    ]
    assert [(s.node, s.topic) for s in facts.subscriptions] == [
        (
            "node_landing_canary_scheduled_effect",
            "onex.intent.platform.runtime-tick.v1",
        )
    ]
    by_topic = {p.topic: p for p in facts.published}
    schedule = by_topic["onex.evt.omnimarket.schedule-run.v1"]
    assert schedule.count == 2
    assert schedule.last_at == datetime(2026, 11, 14, 9, 7, 30, tzinfo=UTC)
    assert "onex.evt.omnimarket.lab-alarm-checked.v1" in by_topic
    # The deferred-wiring line mentions a node and a subscription but is no join: the node has none.
    assert all(s.node != "node_start_path_census_compute" for s in facts.subscriptions)


def test_failing_control_a_changed_log_wording_reads_nothing() -> None:
    reworded = LOG_TEXT.replace("Published output event to", "Emitted output event on")
    facts = parse_runtime_log(reworded, labels())
    assert facts.published == ()
    assert len(facts.loaded_nodes) == 2


def test_parse_unit_state() -> None:
    run = parse_unit_state(
        "ExecMainExitTimestamp=Sat 2026-11-14 06:00:30 UTC\nExecMainStatus=4\nResult=exit-code\n",
        "onex-host-drift.service",
        labels(),
    )
    assert run.last_run_at == datetime(2026, 11, 14, 6, 0, 30, tzinfo=UTC)
    assert run.result == "exit 4 exit-code"
    assert run.ok is False
    with pytest.raises(ValueError, match="no exit timestamp"):
        parse_unit_state("ExecMainExitTimestamp=\n", "u.service", labels())


class _Ssh:
    """Records each remote script and answers by what the script asks for."""

    def __init__(self, answers: dict[str, bytes], fail_on: str = "") -> None:
        self.answers = answers
        self.fail_on = fail_on
        self.scripts: list[str] = []
        self.targets: list[str] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        script = (stdin or b"").decode()
        self.scripts.append(script)
        self.targets.append(argv[-2])
        if self.fail_on and self.fail_on in script:
            return subprocess.CompletedProcess(argv, 255, b"", b"refused")
        for needle, answer in self.answers.items():
            if needle in script:
                return subprocess.CompletedProcess(argv, 0, answer, b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def _source(ssh: _Ssh) -> SourceRuntimeFacts:
    return SourceRuntimeFacts(
        overlay=OVERLAY,
        labels=labels(),
        unit_hosts=(ModelMcrUnitHost(host="h051", ssh_host_env="TEST_H051_SSH"),),
        environ={"TEST_RUNTIME_SSH": "runtime-host", "TEST_H051_SSH": "h051-host"},
        run=ssh,
    )


def test_read_through_ssh_golden() -> None:
    ssh = _Ssh(
        {
            "docker logs": LOG_TEXT.encode(),
            "docker inspect": b"2026-11-14T08:56:27.353489371Z\n",
            "systemctl show": b"ExecMainExitTimestamp=Sat 2026-11-14 09:00:30 UTC\nExecMainStatus=0\nResult=success\n",
        }
    )
    runner = ModelMcrRunner(
        repo="omnimarket", number=1, kind="host_unit", unit="u.service", host="h051"
    )
    facts, started = _source(ssh).read([runner])
    assert started == datetime(2026, 11, 14, 8, 56, 27, tzinfo=UTC)
    assert facts.unread == ()
    assert facts.unit_runs[0].result == "exit 0 success"
    assert facts.unit_runs[0].ok is True
    assert len(facts.loaded_nodes) == 2
    assert "runtime-host" in ssh.targets
    assert "h051-host" in ssh.targets
    # The container is found by its compose labels, never by a name typed in code.
    assert any("com.docker.compose.project=proj" in s for s in ssh.scripts)
    assert any("--timestamp=utc" in s for s in ssh.scripts)


def test_error_runtime_host_refused_reads_unread_never_empty() -> None:
    ssh = _Ssh({}, fail_on="docker")
    facts, started = _source(ssh).read([])
    assert started is None
    assert set(facts.unread) == {"loaded_nodes", "subscriptions", "published"}


def test_error_unit_host_without_target_reads_unit_runs_unread() -> None:
    ssh = _Ssh(
        {
            "docker logs": LOG_TEXT.encode(),
            "docker inspect": b"2026-11-14T08:56:27.1Z\n",
        }
    )
    runner = ModelMcrRunner(
        repo="omnimarket", number=1, kind="host_unit", unit="u.service", host="h999"
    )
    facts, _ = _source(ssh).read([runner])
    assert facts.unread == ("unit_runs",)
    assert len(facts.loaded_nodes) == 2


def test_error_unset_ssh_variable_is_unread() -> None:
    source = SourceRuntimeFacts(
        overlay=OVERLAY, labels=labels(), unit_hosts=(), environ={}, run=_Ssh({})
    )
    facts, started = source.read([])
    assert started is None
    assert "published" in facts.unread


LOG_WITHOUT_DISCOVERY = "\n".join(
    line for line in LOG_TEXT.splitlines() if "Discovered contract" not in line
)
MANIFEST = (
    b'{"contracts": [{"name": "node_start_path_census_compute"}, '
    b'{"name": "other_effect", "entry_point_name": "node_other_effect"}], "errors": []}'
)
CENSUS_TOPIC = "onex.cmd.omnimarket.start-path-census-requested.v1"
SUBSCRIPTION_STATE_LINE = (
    "2026-11-17 05:49:43 [INFO] aiokafka.consumer.subscription_state: Updating subscribed topics to: "
    "frozenset({'onex.evt.omnimarket.script-node-src-completed.v1', "
    "'onex.cmd.omnimarket.start-path-census-requested.v1', "
    "'tenant-local-a630ba907baa.onex.evt.omnibase-infra.delegation-completed.v1'})"
)
LOG_WITHOUT_JOINS = "\n".join(
    line
    for line in LOG_WITHOUT_DISCOVERY.splitlines()
    if "Setting newly assigned partitions" not in line
)
SUBSCRIPTION_MANIFEST = json.dumps(
    {
        "contracts": [
            {
                "name": "start_path_census",
                "entry_point_name": "node_start_path_census_compute",
                "event_bus": {"subscribe_topics": [CENSUS_TOPIC]},
            }
        ]
    }
).encode()


def _manifest_source(ssh: _Ssh, url: str) -> SourceRuntimeFacts:
    return SourceRuntimeFacts(
        overlay=OVERLAY,
        labels=labels(),
        unit_hosts=(),
        environ={"TEST_RUNTIME_SSH": "runtime-host"},
        manifest_url=url,
        run=ssh,
    )


def test_loaded_nodes_come_from_the_manifest_when_the_log_rotated() -> None:
    ssh = _Ssh(
        {
            "docker logs": LOG_WITHOUT_DISCOVERY.encode(),
            "docker inspect": b"2026-11-14T09:23:43.8Z\n",
            "curl": MANIFEST,
        }
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    assert {n.node for n in facts.loaded_nodes} == {
        "node_start_path_census_compute",
        "node_other_effect",
        "other_effect",
    }
    assert facts.unread == ()
    assert any(
        "curl -fsS --max-time 30 http://127.0.0.1:8085/m" in s for s in ssh.scripts
    )


def test_failing_control_rotated_log_and_no_manifest_reads_loaded_unread() -> None:
    """The first live read called every node not loaded because the rotated log held no discovery lines."""
    ssh = _Ssh(
        {
            "docker logs": LOG_WITHOUT_DISCOVERY.encode(),
            "docker inspect": b"2026-11-14T09:23:43.8Z\n",
        }
    )
    facts, _ = _manifest_source(ssh, "").read([])
    assert facts.loaded_nodes == ()
    assert "loaded_nodes" in facts.unread
    assert len(facts.subscriptions) == 1


def test_error_manifest_unreachable_keeps_the_log_lines() -> None:
    ssh = _Ssh(
        {
            "docker logs": LOG_TEXT.encode(),
            "docker inspect": b"2026-11-14T09:23:43.8Z\n",
        },
        fail_on="curl",
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    assert len(facts.loaded_nodes) == 2
    # the manifest is the complete loaded list; the log's discovery lines alone may have rotated: unread when it fails
    assert facts.unread == ("loaded_nodes",)


def test_a_runtime_that_is_not_healthy_reads_joins_and_publishes_unread() -> None:
    """The first live read after a roll called every topic node unsubscribed while the runtime was still attaching."""
    degraded = LOG_TEXT.replace("status=HEALTHY", "status=DEGRADED")
    booting = "\n".join(
        line for line in LOG_TEXT.splitlines() if "Runtime health check" not in line
    )
    for text in (degraded, booting):
        facts = parse_runtime_log(text, labels())
        assert set(facts.unread) == {"subscriptions", "published"}
        # what was seen still counts
        assert len(facts.subscriptions) == 1
    assert parse_runtime_log(LOG_TEXT, labels()).unread == ()


def test_a_refused_ssh_is_retried_by_the_contracts_attempt_count() -> None:
    class Flaky(_Ssh):
        def __init__(self) -> None:
            super().__init__(
                {
                    "docker logs": LOG_TEXT.encode(),
                    "docker inspect": b"2026-11-14T09:23:43.8Z\n",
                }
            )
            self.calls = 0

        def __call__(
            self, argv: Sequence[str], stdin: bytes | None, timeout: int
        ) -> subprocess.CompletedProcess[bytes]:
            self.calls += 1
            if self.calls == 1:
                return subprocess.CompletedProcess(argv, 255, b"", b"refused")
            return super().__call__(argv, stdin, timeout)

    ssh = Flaky()
    facts, started = _source(ssh).read([])
    assert started is not None
    assert len(facts.loaded_nodes) == 2
    assert ssh.calls > 2


def test_a_join_under_the_contract_name_counts_for_the_entry_point_name() -> None:
    """Consumer groups carry the contract name (projection_work_ledger); the node map carries node_projection_work_ledger."""
    join = (
        "2026-11-14 11:12:00 [INFO] aiokafka.consumer.group_coordinator: Setting newly assigned partitions "
        "{TopicPartition(topic='onex.evt.omnimarket.work-ledger-claim.v1', partition=0)} for group "
        "local.omnimarket.projection_work_ledger.consume.1.2.0.__i.runtime-main.__t.onex.evt.omnimarket.work-ledger-claim.v1"
    )
    manifest = b'{"contracts": [{"name": "projection_work_ledger", "entry_point_name": "node_projection_work_ledger"}]}'
    ssh = _Ssh(
        {
            "docker logs": (LOG_TEXT + "\n" + join).encode(),
            "docker inspect": b"2026-11-14T09:23:43.8Z\n",
            "curl": manifest,
        }
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    joined = {(s.node, s.topic) for s in facts.subscriptions}
    assert (
        "projection_work_ledger",
        "onex.evt.omnimarket.work-ledger-claim.v1",
    ) in joined
    assert (
        "node_projection_work_ledger",
        "onex.evt.omnimarket.work-ledger-claim.v1",
    ) in joined
    assert facts.unread == ()


def test_parse_subscription_state_keeps_the_last_time_per_exact_topic() -> None:
    text = "\n".join(
        (
            LOG_WITHOUT_JOINS,
            SUBSCRIPTION_STATE_LINE,
            SUBSCRIPTION_STATE_LINE.replace("05:49:43", "05:49:13"),
            "2026-11-17 05:50:13 [INFO] aiokafka.consumer.subscription_state: Updating subscribed topics to: "
            "frozenset({'onex.evt.omnimarket.script-node-src-completed.v1'})",
        )
    )
    facts = parse_runtime_log(text, labels())
    assert {t.topic: t.last_at for t in facts.topics_in_subscription} == {
        "onex.evt.omnimarket.script-node-src-completed.v1": datetime(
            2026, 11, 17, 5, 50, 13, tzinfo=UTC
        ),
        CENSUS_TOPIC: datetime(2026, 11, 17, 5, 49, 43, tzinfo=UTC),
        "tenant-local-a630ba907baa.onex.evt.omnibase-infra.delegation-completed.v1": datetime(
            2026, 11, 17, 5, 49, 43, tzinfo=UTC
        ),
    }
    assert facts.subscriptions == ()
    assert facts.unread == ()


@pytest.mark.parametrize("with_alias", [False, True])
def test_subscriptions_come_from_manifest_and_retained_subscription_state(
    with_alias: bool,
) -> None:
    manifest = (
        SUBSCRIPTION_MANIFEST
        if with_alias
        else SUBSCRIPTION_MANIFEST.replace(
            b'"name": "start_path_census", "entry_point_name": ', b'"name": '
        )
    )
    ssh = _Ssh(
        {
            "docker logs": (
                LOG_WITHOUT_JOINS + "\n" + SUBSCRIPTION_STATE_LINE
            ).encode(),
            "docker inspect": b"2026-11-17T05:30:00Z\n",
            "curl": manifest,
        }
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    expected = {("node_start_path_census_compute", CENSUS_TOPIC)}
    if with_alias:
        expected.add(("start_path_census", CENSUS_TOPIC))
    assert {(s.node, s.topic) for s in facts.subscriptions} == expected
    assert len(facts.subscriptions) == len(expected)
    assert all(
        s.joined_at == datetime(2026, 11, 17, 5, 49, 43, tzinfo=UTC)
        for s in facts.subscriptions
    )
    assert len(facts.topics_in_subscription) == 3
    assert facts.unread == ()
    assert sum("curl" in script for script in ssh.scripts) == 1
    assert any("Updating subscribed topics to: " in script for script in ssh.scripts)


@pytest.mark.parametrize(
    "state_line",
    [
        "",
        SUBSCRIPTION_STATE_LINE.replace(CENSUS_TOPIC, "onex.cmd.other.requested.v1"),
        SUBSCRIPTION_STATE_LINE.replace(
            CENSUS_TOPIC, "tenant-local-a630ba907baa." + CENSUS_TOPIC
        ),
    ],
    ids=["no-state-lines", "undeclared-topics", "tenant-prefix-is-not-an-exact-match"],
)
def test_failing_control_declared_topic_requires_exact_subscription_state_evidence(
    state_line: str,
) -> None:
    ssh = _Ssh(
        {
            "docker logs": (LOG_WITHOUT_JOINS + "\n" + state_line).encode(),
            "docker inspect": b"2026-11-17T05:30:00Z\n",
            "curl": SUBSCRIPTION_MANIFEST,
        }
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    assert facts.subscriptions == ()
    assert facts.unread == ()


@pytest.mark.parametrize("url", ["", "http://127.0.0.1:8085/m"])
def test_failing_control_subscription_state_without_readable_manifest_credits_nothing(
    url: str,
) -> None:
    ssh = _Ssh(
        {
            "docker logs": (
                LOG_WITHOUT_JOINS + "\n" + SUBSCRIPTION_STATE_LINE
            ).encode(),
            "docker inspect": b"2026-11-17T05:30:00Z\n",
        },
        fail_on="curl",
    )
    facts, _ = _manifest_source(ssh, url).read([])
    assert facts.subscriptions == ()
    assert len(facts.topics_in_subscription) == 3
    assert facts.unread == ("loaded_nodes",)


def test_manifest_subscriptions_include_contract_and_entry_point_names() -> None:
    source = _manifest_source(
        _Ssh({"curl": SUBSCRIPTION_MANIFEST}), "http://127.0.0.1:8085/m"
    )
    assert source.manifest_subscriptions() == {
        "start_path_census": (CENSUS_TOPIC,),
        "node_start_path_census_compute": (CENSUS_TOPIC,),
    }


def test_subscription_state_does_not_duplicate_or_replace_an_existing_join() -> None:
    join = (
        "2026-11-17 05:48:00 [INFO] aiokafka.consumer.group_coordinator: Setting newly assigned partitions "
        "{TopicPartition(topic='onex.cmd.omnimarket.start-path-census-requested.v1', partition=0)} for group "
        "local.omnimarket.start_path_census.consume.1.0.0"
    )
    ssh = _Ssh(
        {
            "docker logs": "\n".join(
                (LOG_WITHOUT_JOINS, join, SUBSCRIPTION_STATE_LINE)
            ).encode(),
            "docker inspect": b"2026-11-17T05:30:00Z\n",
            "curl": SUBSCRIPTION_MANIFEST,
        }
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    assert {(s.node, s.topic) for s in facts.subscriptions} == {
        ("start_path_census", CENSUS_TOPIC),
        ("node_start_path_census_compute", CENSUS_TOPIC),
    }
    assert len(facts.subscriptions) == 2
    assert all(
        s.joined_at == datetime(2026, 11, 17, 5, 48, tzinfo=UTC)
        for s in facts.subscriptions
    )


def test_rotated_log_subscription_state_to_running_reading() -> None:
    result_topic = "onex.evt.omnimarket.start-path-census-completed.v1"
    published = (
        "2026-11-17 05:50:00 [INFO] omnibase_infra.runtime.service_dispatch_result_applier: "
        f"Published output event to {result_topic} (correlation_id=aa)"
    )
    ssh = _Ssh(
        {
            "docker logs": "\n".join(
                (LOG_WITHOUT_JOINS, SUBSCRIPTION_STATE_LINE, published)
            ).encode(),
            "docker inspect": b"2026-11-17T05:30:00Z\n",
            "curl": SUBSCRIPTION_MANIFEST,
        }
    )
    runner = ModelMcrRunner(
        repo="omnimarket",
        number=6120,
        kind="topic",
        nodes=("node_start_path_census_compute",),
        trigger_topics=(CENSUS_TOPIC,),
        result_topics=(result_topic,),
    )
    facts, started = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([runner])
    sources = SourceInMemoryChangeReading(
        runners=(runner,),
        build_facts=(
            ModelMcrBuildFact(
                repo="omnimarket", number=6120, in_build=True, source="test"
            ),
        ),
        build_started_at=started,
        runtime=facts,
    )

    class Container:
        def get_service(self, key: Any) -> SourceInMemoryChangeReading:
            return sources

    container: Any = Container()
    result = HandlerMergedChangeReadingEffect(container, config()).handle(
        ModelMcrRunRequest(
            changes=(ModelMcrChange(repo="omnimarket", number=6120),),
            now=datetime(2026, 11, 17, 6, tzinfo=UTC),
        )
    )
    reading = result.reading.readings[0]
    assert (reading.verdict, reading.missing_step) == ("SEEN_RUNNING", "")
    assert reading.nodes[0].subscribed_topics == (CENSUS_TOPIC,)
    assert reading.last_run_at == datetime(2026, 11, 17, 5, 50, tzinfo=UTC)


def test_error_a_malformed_event_bus_block_declares_nothing_and_keeps_the_manifest() -> (
    None
):
    manifest = json.dumps(
        {
            "contracts": [
                {"name": "node_a", "event_bus": "not-an-object"},
                {"name": "node_b", "event_bus": {"subscribe_topics": None}},
                {
                    "name": "node_c",
                    "event_bus": {"subscribe_topics": [CENSUS_TOPIC, 7]},
                },
            ]
        }
    ).encode()
    ssh = _Ssh(
        {
            "docker logs": (
                LOG_WITHOUT_JOINS + "\n" + SUBSCRIPTION_STATE_LINE
            ).encode(),
            "docker inspect": b"2026-11-17T05:30:00Z\n",
            "curl": manifest,
        }
    )
    facts, _ = _manifest_source(ssh, "http://127.0.0.1:8085/m").read([])
    assert {n.node for n in facts.loaded_nodes} == {"node_a", "node_b", "node_c"}
    assert {(s.node, s.topic) for s in facts.subscriptions} == {
        ("node_c", CENSUS_TOPIC)
    }
    assert facts.unread == ()
