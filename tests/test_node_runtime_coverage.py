"""Runtime redundancy is measured per command topic, never from group existence."""

from datetime import UTC, datetime

import pytest

from omnimarket.models.runtime_coverage import (
    ModelCommandSubscription,
    ModelRuntimeCoverageSnapshot,
    ModelRuntimeMember,
)
from omnimarket.nodes.node_runtime_coverage_compute import HandlerRuntimeCoverageCompute

pytestmark = pytest.mark.unit
TOPIC = "onex.cmd.omnimarket.github-repo-write-requested.v1"
NODE = "node_github_repo_write_effect"


def snapshot(members=(), **kwargs):
    return ModelRuntimeCoverageSnapshot(
        bus_lane="dev",
        broker="dev-broker:9092",
        observed_at=datetime.now(UTC),
        commands=(ModelCommandSubscription(node=NODE, topic=TOPIC),),
        members=members,
        **kwargs,
    )


def member(runtime="effects-201", host="h201", topic=TOPIC, node=NODE):
    return ModelRuntimeMember(
        runtime_id=runtime,
        host=host,
        node=node,
        topics=(topic,),
        group_id="local.omnimarket.github_repo_write_effect.consume.1.0.0",
    )


def test_today_h201_down_github_write_has_zero_servers_and_fails():
    result = HandlerRuntimeCoverageCompute().handle(snapshot())
    assert result.status == "FAIL"
    assert result.commands[0].serving_runtimes == 0
    assert result.commands[0].hosts == ()


def test_single_runtime_fails_and_names_host():
    result = HandlerRuntimeCoverageCompute().handle(snapshot((member(),)))
    assert result.status == "FAIL"
    assert result.commands[0].serving_runtimes == 1
    assert "h201" in result.commands[0].detail


def test_two_distinct_runtimes_are_ok():
    result = HandlerRuntimeCoverageCompute().handle(
        snapshot((member(), member("effects-202", "h202")))
    )
    assert result.status == "OK"
    assert result.commands[0].serving_runtimes == 2


def test_duplicate_members_for_one_runtime_do_not_make_redundancy():
    result = HandlerRuntimeCoverageCompute().handle(snapshot((member(), member())))
    assert result.status == "FAIL"
    assert result.commands[0].serving_runtimes == 1


def test_unrelated_node_or_topic_never_counts():
    result = HandlerRuntimeCoverageCompute().handle(
        snapshot((member(node="node_audit"), member(topic="onex.cmd.other.v1")))
    )
    assert result.commands[0].serving_runtimes == 0


def test_unreadable_broker_is_fail_with_unknown_count_not_zero():
    result = HandlerRuntimeCoverageCompute().handle(snapshot(error="broker unreadable"))
    assert result.status == "FAIL"
    assert result.commands[0].serving_runtimes is None
    assert "unreadable" in result.commands[0].detail


def test_empty_contract_inventory_cannot_pass():
    empty = snapshot().model_copy(update={"commands": ()})
    assert HandlerRuntimeCoverageCompute().handle(empty).status == "FAIL"


def test_every_command_of_node_must_have_two_servers():
    s = snapshot((member(), member("effects-202", "h202")))
    s = s.model_copy(
        update={
            "commands": (
                *s.commands,
                ModelCommandSubscription(node=NODE, topic="onex.cmd.other.v1"),
            )
        }
    )
    result = HandlerRuntimeCoverageCompute().handle(s)
    assert result.status == "FAIL"
    assert [r.serving_runtimes for r in result.commands] == [2, 0]


@pytest.mark.asyncio
async def test_effect_reads_only_selected_contract_roots_and_lane(tmp_path):
    from omnimarket.models.runtime_coverage import ModelRuntimeCoverageRequest
    from omnimarket.nodes.node_runtime_coverage_effect import (
        HandlerRuntimeCoverageEffect,
    )

    root = tmp_path / "omnimarket" / "nodes"
    included = root / NODE
    excluded = root / "node_prod_only"
    for directory, lane in [(included, "runtime-a"), (excluded, "production")]:
        directory.mkdir(parents=True)
        (directory / "contract.yaml").write_text(
            f"name: {directory.name}\nruntime_lanes: [{lane}]\nevent_bus:\n  subscribe_topics: [{TOPIC}, onex.evt.other.v1]\n"
        )
    calls = []

    def read(config, prefixes):
        calls.append((config, prefixes))
        return (), ()

    effect = HandlerRuntimeCoverageEffect(
        read, lambda _lane: ("dev-broker:9092", "local", {})
    )
    evidence = await effect.handle(
        ModelRuntimeCoverageRequest(
            bus_lane="dev", runtime_lanes=("runtime-a",), contract_roots=(str(root),)
        )
    )
    assert evidence.error == ""
    assert [c.node for c in evidence.commands] == [NODE]
    assert calls[0][1] == {f"local.omnimarket.{NODE}.consume.": NODE}
    assert (
        HandlerRuntimeCoverageCompute().handle(evidence).commands[0].serving_runtimes
        == 0
    )


@pytest.mark.asyncio
async def test_effect_broker_failure_never_becomes_zero_or_exposes_credentials(
    tmp_path,
):
    from omnimarket.models.runtime_coverage import ModelRuntimeCoverageRequest
    from omnimarket.nodes.node_runtime_coverage_effect import (
        HandlerRuntimeCoverageEffect,
    )

    root = tmp_path / "omnimarket" / "nodes"
    path = root / NODE
    path.mkdir(parents=True)
    (path / "contract.yaml").write_text(
        f"name: {NODE}\nevent_bus:\n  subscribe_topics: [{TOPIC}]\n"
    )

    def broken(config, prefixes):
        raise RuntimeError("password=do-not-print")

    evidence = await HandlerRuntimeCoverageEffect(
        broken, lambda _lane: ("broker", "local", {})
    ).handle(
        ModelRuntimeCoverageRequest(
            bus_lane="dev", runtime_lanes=("runtime-a",), contract_roots=(str(root),)
        )
    )
    result = HandlerRuntimeCoverageCompute().handle(evidence)
    assert result.status == "FAIL"
    assert result.commands[0].serving_runtimes is None
    assert "do-not-print" not in result.model_dump_json()


def test_broker_adapter_counts_warm_static_member_and_deduplicates_runtime():
    from types import SimpleNamespace

    from omnimarket.nodes.node_runtime_coverage_effect.handlers.broker_reader import (
        member_evidence,
    )

    group = f"local.omnimarket.{NODE}.consume.1.0.0.__t.{TOPIC}"
    raw = SimpleNamespace(
        group_instance_id=group + ".runtime-effects-h201",
        host="/h201",
        client_id="aiokafka",
        assignment=SimpleNamespace(topic_partitions=[]),
    )
    evidence = member_evidence(group, NODE, raw)
    assert evidence.runtime_id == "runtime-effects-h201"
    assert evidence.topics == (TOPIC,)
    result = HandlerRuntimeCoverageCompute().handle(snapshot((evidence, evidence)))
    assert result.commands[0].serving_runtimes == 1


@pytest.mark.asyncio
async def test_system_status_fails_for_single_runtime_even_with_green_registration(
    tmp_path, monkeypatch
):
    from omnimarket.nodes.node_platform_diagnostics.handlers import (
        dimension_checks as checks,
    )

    async def coverage(*args):
        return HandlerRuntimeCoverageCompute().handle(snapshot((member(),)))

    monkeypatch.setattr(checks, "read_health_runtime_coverage", coverage, raising=False)
    sweep = tmp_path / ".onex_state" / "runtime-sweep"
    sweep.mkdir(parents=True)
    (sweep / "result.json").write_text('{"total_nodes": 1}')
    result = await checks.check_runtime_nodes(
        checks.DiagnosticsCheckContext(workspace_root=tmp_path)
    )
    assert result.status.value == "FAIL"
    assert any("h201" in line for line in result.actionable_items)


def test_recorded_dev_outage_fixture():
    from pathlib import Path

    fixture = (
        Path(__file__).parents[1]
        / "fixtures/runtime_coverage/dev_h201_down_2026-10-25.json"
    )
    result = HandlerRuntimeCoverageCompute().handle(
        ModelRuntimeCoverageSnapshot.model_validate_json(fixture.read_text())
    )
    assert result.status == "FAIL"
    assert [(r.node, r.serving_runtimes) for r in result.commands] == [
        ("node_github_repo_gateway_effect", 0),
        (NODE, 0),
    ]


@pytest.mark.parametrize(
    ("state", "error", "expected"),
    [
        ("EMPTY", False, 0),
        ("STABLE", True, None),
        ("PREPARING_REBALANCING", False, None),
    ],
)
def test_broker_group_existence_or_failed_describe_never_proves_live_members(
    monkeypatch, state, error, expected
):
    from types import SimpleNamespace

    from omnimarket.nodes.node_runtime_coverage_effect.handlers.broker_reader import (
        read_members,
    )

    group = f"local.omnimarket.{NODE}.consume.1.0.0.__t.{TOPIC}"

    class Future:
        def __init__(self, value, fail=False):
            self.value, self.fail = value, fail

        def result(self, **kwargs):
            if self.fail:
                raise RuntimeError("broker unavailable")
            return self.value

    calls = []

    class Admin:
        def __init__(self, config):
            assert config == {"bootstrap.servers": "dev-broker:9092"}

        def list_consumer_groups(self, **kwargs):
            # Different node, environment, and audit purpose on the SAME
            # command topic must never be treated as servers of this node.
            return Future(
                SimpleNamespace(
                    errors=[],
                    valid=[
                        SimpleNamespace(group_id=g)
                        for g in [
                            group,
                            group.replace(".consume.", ".audit."),
                            group.replace("local.", "prod.", 1),
                            group.replace(NODE, "node_audit"),
                        ]
                    ],
                )
            )

        def describe_consumer_groups(self, groups, **kwargs):
            calls.extend(groups)
            return {
                g: Future(
                    SimpleNamespace(state=SimpleNamespace(name=state), members=[]),
                    error,
                )
                for g in groups
            }

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", Admin)
    members, unreadable = read_members(
        {"bootstrap.servers": "dev-broker:9092"},
        {f"local.omnimarket.{NODE}.consume.": NODE},
    )
    evidence = snapshot(members, unreadable_nodes=unreadable)
    result = HandlerRuntimeCoverageCompute().handle(evidence)
    assert calls == [group]
    assert result.commands[0].serving_runtimes == expected
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_published_manifest_corroborates_broker_membership_but_cannot_create_servers(
    tmp_path,
):
    from omnimarket.models.runtime_coverage import (
        ModelRuntimeCoverageRequest,
        ModelRuntimeRegistryEndpoint,
    )
    from omnimarket.nodes.node_runtime_coverage_effect import (
        HandlerRuntimeCoverageEffect,
    )

    root = tmp_path / "omnimarket" / "nodes"
    path = root / NODE
    path.mkdir(parents=True)
    (path / "contract.yaml").write_text(
        f"name: {NODE}\nevent_bus:\n  subscribe_topics: [{TOPIC}]\n"
    )
    endpoint = ModelRuntimeRegistryEndpoint(
        bus_lane="dev",
        runtime_id="effects-201",
        host="h201",
        manifest_url="http://runtime:8085/v1/introspection/manifest",
    )
    request = ModelRuntimeCoverageRequest(
        bus_lane="dev",
        contract_roots=(str(root),),
        runtime_lanes=("runtime-a",),
        runtime_registries=(endpoint,),
    )
    calls = []

    async def registry(ref):
        calls.append(ref)
        return frozenset({(NODE, TOPIC)})

    effect = HandlerRuntimeCoverageEffect(
        lambda _config, _prefixes: ((), ()),
        lambda _lane: ("broker", "local", {}),
        registry,
    )
    evidence = await effect.handle(request)
    assert calls == [endpoint]
    assert (
        HandlerRuntimeCoverageCompute().handle(evidence).commands[0].serving_runtimes
        == 0
    )
    effect = HandlerRuntimeCoverageEffect(
        lambda _config, _prefixes: ((member(host="container-ip"),), ()),
        lambda _lane: ("broker", "local", {}),
        registry,
    )
    result = HandlerRuntimeCoverageCompute().handle(await effect.handle(request))
    assert result.commands[0].hosts == ("h201",)
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_registry_from_another_lane_is_refused_before_reading(tmp_path):
    from omnimarket.models.runtime_coverage import (
        ModelRuntimeCoverageRequest,
        ModelRuntimeRegistryEndpoint,
    )
    from omnimarket.nodes.node_runtime_coverage_effect import (
        HandlerRuntimeCoverageEffect,
    )

    path = tmp_path / "omnimarket" / "nodes" / NODE
    path.mkdir(parents=True)
    (path / "contract.yaml").write_text(
        f"name: {NODE}\nevent_bus:\n  subscribe_topics: [{TOPIC}]\n"
    )

    def forbidden(*args):
        pytest.fail("cross-lane broker read")

    effect = HandlerRuntimeCoverageEffect(
        forbidden, lambda _lane: ("broker", "local", {})
    )
    evidence = await effect.handle(
        ModelRuntimeCoverageRequest(
            bus_lane="dev",
            contract_roots=(str(path.parent),),
            runtime_lanes=("runtime-a",),
            runtime_registries=(
                ModelRuntimeRegistryEndpoint(
                    bus_lane="another-lane",
                    runtime_id="other",
                    host="other",
                    manifest_url="http://other/manifest",
                ),
            ),
        )
    )
    assert evidence.error


def test_stable_broker_members_prove_two_runtimes_including_unassigned_standby(
    monkeypatch,
):
    from types import SimpleNamespace

    from omnimarket.nodes.node_runtime_coverage_effect.handlers.broker_reader import (
        read_members,
    )

    group = f"local.omnimarket.{NODE}.consume.1.0.0.__t.{TOPIC}"

    class Future:
        def __init__(self, value):
            self.value = value

        def result(self, **kwargs):
            return self.value

    raw_members = [
        SimpleNamespace(
            group_instance_id=group + "." + name,
            host="/" + host,
            client_id="aiokafka",
            assignment=SimpleNamespace(topic_partitions=[]),
        )
        for name, host in [("effects-201", "h201"), ("effects-202", "h202")]
    ]

    class Admin:
        def __init__(self, config):
            pass

        def list_consumer_groups(self, **kwargs):
            return Future(
                SimpleNamespace(errors=[], valid=[SimpleNamespace(group_id=group)])
            )

        def describe_consumer_groups(self, groups, **kwargs):
            return {
                group: Future(
                    SimpleNamespace(
                        state=SimpleNamespace(name="STABLE"), members=raw_members
                    )
                )
            }

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", Admin)
    members, unreadable = read_members({}, {f"local.omnimarket.{NODE}.consume.": NODE})
    result = HandlerRuntimeCoverageCompute().handle(
        snapshot(members, unreadable_nodes=unreadable)
    )
    assert result.status == "OK"
    assert result.commands[0].serving_runtimes == 2
    assert result.commands[0].hosts == ("h201", "h202")


@pytest.mark.asyncio
async def test_system_status_cli_exits_nonzero_for_failed_health(monkeypatch, capsys):
    import argparse
    from types import SimpleNamespace

    from omnimarket.nodes.node_platform_diagnostics import __main__ as cli
    from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
        ModelDiagnosticsResult,
    )
    from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
        EnumReadinessStatus,
    )

    monkeypatch.setattr(
        cli,
        "_parse_args",
        lambda: argparse.Namespace(
            dimensions="RUNTIME_NODES",
            dry_run=False,
            bus_lane="dev",
            freshness_threshold_hours=4,
            coverage_config=None,
        ),
    )

    async def handle(request):
        assert request.bus_lane == "dev"
        return ModelDiagnosticsResult(
            overall_status=EnumReadinessStatus.FAIL,
            dimensions=[],
            run_duration_seconds=0,
            generated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        cli, "HandlerPlatformDiagnostics", lambda: SimpleNamespace(handle=handle)
    )
    with pytest.raises(SystemExit, match="1"):
        await cli._main()
    assert '"overall_status": "FAIL"' in capsys.readouterr().out
