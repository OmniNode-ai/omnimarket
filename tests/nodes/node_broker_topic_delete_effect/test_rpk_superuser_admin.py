# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev broker superuser route of the topic delete (OMN-23948).

Golden and error chain tests on the in-memory bus, the refusals of the route (lane, target, name,
credential) each with the runner never called, the seam with the broker permission tool's credential
read, and failing controls. The runner is a fake that answers with rpk's real output formats.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.models.model_topic_removal import EnumTopicClass
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.driver_broker_acl import (
    AclConnection,
    build_remote_script,
)
from omnimarket.nodes.node_broker_topic_delete_effect import runtime_broker_topic_delete
from omnimarket.nodes.node_broker_topic_delete_effect.adapters.kafka_admin import (
    KafkaBrokerTopicAdmin,
)
from omnimarket.nodes.node_broker_topic_delete_effect.adapters.rpk_admin import (
    RpkSuperuserTopicAdmin,
)
from omnimarket.nodes.node_broker_topic_delete_effect.handlers.handler_broker_topic_delete import (
    HandlerBrokerTopicDelete,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    EnumTopicDeleteStatus,
    ModelDeleteTarget,
    ModelTopicDeleteCommand,
)
from omnimarket.nodes.node_broker_topic_delete_effect.overlay import (
    ModelSuperuserAccess,
    ModelTopicDeleteOverlay,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import BrokerAdminError
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    InMemoryDeclaredTopics,
)
from tests.nodes.node_broker_topic_delete_effect.delete_support import NOW, overlay

pytestmark = pytest.mark.unit

TARGET = "operator@broker-host"
ENVIRON = {"TEST_SSH_TARGET": TARGET}
ACCESS = {
    "lane": "dev",
    "host": "dev-lane-broker",
    "compose_project": "omnibase-infra",
    "broker_container": "omnibase-infra-redpanda",
    "superuser_source_container": "omnibase-infra-redpanda-sasl-enable",
    "ssh_target_env": "TEST_SSH_TARGET",
    "rpk_brokers": "redpanda:9092",
    "sasl_mechanism": "SCRAM-SHA-256",
    "script_timeout_seconds": 60,
}

#: The formats rpk printed on the live dev broker (columns split by two or more spaces).
TOPIC_LIST = """NAME  PARTITIONS  REPLICAS
fx-empty  1  1
fx-group  2  1
fx-offsets  1  1
fx-records  1  1
"""
DESCRIBE_HEADER = (
    "PARTITION  LEADER  EPOCH  REPLICAS  LOG-START-OFFSET  HIGH-WATERMARK\n"
)
DESCRIBES = {
    "fx-empty": "0  0  3  [0]  0  0\n",
    "fx-group": "0  0  3  [0]  0  0\n1  0  3  [0]  0  0\n",
    "fx-offsets": "0  0  3  [0]  0  0\n",
    "fx-records": "0  0  3  [0]  2  9\n",
    "fx-gone": "",
}
GROUPS = """GROUP                  g-live
COORDINATOR-NODE       0
STATE                  Stable
MEMBERS                1
TOTAL-LAG              0

TOPIC     PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG  MEMBER-ID  CLIENT-ID  HOST
fx-group  0          -               0                 0               0    m-1        c-1        /host-1

GROUP                  g-offsets
COORDINATOR-NODE       0
STATE                  Empty
MEMBERS                0
TOTAL-LAG              0

TOPIC       PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG  MEMBER-ID  CLIENT-ID  HOST
fx-offsets  0          4               0                 4               0
"""
RECORD_OUT = "##REC fx-records 0\n1794476308931\n"
DELETE_OUT = "TOPIC  STATUS\nfx-empty  OK\nfx-offsets  UNKNOWN_TOPIC_OR_PARTITION\n"


class FakeRunner:
    """Answers each remote script with rpk's output; a deleted topic drops out of later reads."""

    def __init__(self, *, delete_out: str | None = DELETE_OUT) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self.deleted: set[str] = set()
        self._delete_out = delete_out

    def __call__(self, target: str, script: str, *, timeout: float) -> str:
        self.calls.append((target, script, timeout))
        if "##REC" in script:
            return RECORD_OUT
        if "topic delete" in script:
            self.deleted |= {
                name
                for name in DESCRIBES
                if f" {name}" in script.split("topic delete")[1]
            }
            return self._delete_out or ""
        listed = "".join(
            line + "\n"
            for line in TOPIC_LIST.splitlines()
            if line.split()[0] not in self.deleted
        )
        text = "##LIST\n" + listed
        for name, body in DESCRIBES.items():
            text += f"##TOPIC {name}\n{DESCRIBE_HEADER}{body}"
        return text + "##GROUPS\n" + GROUPS


class CredentialFailingRunner:
    def __call__(self, target: str, script: str, *, timeout: float) -> str:
        raise RuntimeError(
            "remote ACL command failed (exit 3): no lane superuser credentials in "
            "omnibase-infra-redpanda-sasl-enable"
        )


def superuser_overlay(enforce: bool = False, **access: Any) -> ModelTopicDeleteOverlay:
    return overlay(enforce=enforce).model_copy(
        update={"superuser": ModelSuperuserAccess.model_validate({**ACCESS, **access})}
    )


def admin(runner: Any, **access: Any) -> RpkSuperuserTopicAdmin:
    return RpkSuperuserTopicAdmin(superuser_overlay(**access), ENVIRON, runner=runner)


def command(
    *names: str, approval: str | None = "ruling-row"
) -> ModelTopicDeleteCommand:
    return ModelTopicDeleteCommand(
        correlation_id="rpk-1",
        topics=tuple(
            ModelDeleteTarget(
                name=name, topic_class=EnumTopicClass.UNDECLARED_NEVER_PRODUCED
            )
            for name in names
        ),
        approval_ref=approval,
    )


def handler(runner: Any, *, enforce: bool = True) -> HandlerBrokerTopicDelete:
    return HandlerBrokerTopicDelete(
        overlay=superuser_overlay(enforce=enforce),
        admin=RpkSuperuserTopicAdmin(
            superuser_overlay(enforce=enforce), ENVIRON, runner=runner
        ),
        declared=InMemoryDeclaredTopics({}),
        clock=lambda: NOW,
    )


# -- golden ------------------------------------------------------------------------------------


def test_evidence_parsed_from_rpk_output() -> None:
    runner = FakeRunner()
    names = ["fx-empty", "fx-group", "fx-offsets", "fx-records", "fx-gone"]
    evidence = admin(runner).read_evidence(names)
    assert evidence["fx-gone"].exists is False
    assert evidence["fx-group"].partitions == 2
    assert evidence["fx-group"].consumer_groups == ("g-live",)
    assert evidence["fx-group"].offset_groups == ()
    assert evidence["fx-offsets"].offset_groups == ("g-offsets",)
    assert evidence["fx-offsets"].consumer_groups == ()
    assert evidence["fx-records"].high_watermark_total == 9
    assert evidence["fx-records"].retained_records == 7
    assert evidence["fx-records"].last_record_ms == 1794476308931
    assert evidence["fx-empty"].retained_records == 0
    assert evidence["fx-empty"].last_record_ms is None
    record_scripts = [script for _t, script, _o in runner.calls if "##REC" in script]
    assert len(record_scripts) == 1
    assert "##REC fx-records 0" in record_scripts[0]
    assert "-o 8:9" in record_scripts[0]
    assert "##REC fx-empty" not in record_scripts[0]


def test_delete_parsed() -> None:
    runner = FakeRunner()
    outcome = admin(runner).delete(["fx-empty", "fx-offsets", "fx-group"])
    assert outcome == {
        "fx-empty": "",
        "fx-offsets": "UNKNOWN_TOPIC_OR_PARTITION",
        "fx-group": "delete_unconfirmed",
    }
    target, script, timeout = runner.calls[0]
    assert (target, timeout) == (TARGET, 60)
    assert "R topic delete fx-empty fx-offsets fx-group" in script


def test_seam_script_reads_the_credential_as_the_permission_tool_does() -> None:
    """The prologue is the broker permission tool's own; it fails if that tool's credential read changes shape."""
    runner = FakeRunner()
    admin(runner).read_evidence(["fx-empty"])
    script = runner.calls[0][1]
    connection = AclConnection.from_mapping(
        {
            key: ACCESS[key]
            for key in (
                "lane",
                "host",
                "compose_project",
                "broker_container",
                "superuser_source_container",
            )
        },
        TARGET,
    )
    prologue = build_remote_script(connection, [])
    assert script.startswith(prologue)
    assert "docker inspect omnibase-infra-redpanda-sasl-enable" in script
    assert '-X user="$U" -X pass="$P"' in script
    assert "-X brokers=redpanda:9092" in script
    assert "-X sasl.mechanism=SCRAM-SHA-256" in script


def test_seam_acl_declaration_names_the_same_containers() -> None:
    """The permission tool's shipped declaration and a superuser block built from it give one prologue."""
    node = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_broker_acl_reconcile_effect/contract.yaml"
    )
    declaration = yaml.safe_load(node.read_text(encoding="utf-8"))[
        "broker_acl_declaration"
    ]
    assert declaration["lane"] == "dev"
    built = {
        **ACCESS,
        **{
            key: declaration[key]
            for key in (
                "host",
                "compose_project",
                "broker_container",
                "superuser_source_container",
            )
        },
    }
    runner = FakeRunner()
    RpkSuperuserTopicAdmin(
        overlay().model_copy(
            update={"superuser": ModelSuperuserAccess.model_validate(built)}
        ),
        ENVIRON,
        runner=runner,
    ).read_evidence(["fx-empty"])
    assert (
        f"docker inspect {declaration['superuser_source_container']}"
        in runner.calls[0][1]
    )


def test_chain_handler_deletes_through_the_superuser() -> None:
    runner = FakeRunner()
    result = handler(runner).handle(command("fx-empty", "fx-group"))
    statuses = {entry.name: entry for entry in result.entries}
    assert statuses["fx-group"].status is EnumTopicDeleteStatus.REFUSED
    assert statuses["fx-group"].reasons == ("consumer_group:g-live",)
    assert statuses["fx-empty"].status is EnumTopicDeleteStatus.DELETED
    assert result.status == "enforced"
    assert result.deletes_issued == 1
    delete_scripts = [s for _t, s, _o in runner.calls if "topic delete" in s]
    assert len(delete_scripts) == 1
    assert "fx-group" not in delete_scripts[0].split("topic delete")[1]


def test_chain_report_only_never_calls_delete() -> None:
    runner = FakeRunner()
    result = handler(runner, enforce=False).handle(command("fx-empty", approval=None))
    assert result.status == "report_only"
    assert result.entries[0].status is EnumTopicDeleteStatus.WOULD_DELETE
    assert not any("topic delete" in script for _t, script, _o in runner.calls)


def test_selection_of_the_superuser_adapter(tmp_path: Any) -> None:
    (tmp_path / "example-repo").mkdir()
    (tmp_path / "example-repo" / "contract.yaml").write_text(
        "name: x\n", encoding="utf-8"
    )
    environ = {**ENVIRON, "REGISTRY_ROOT": str(tmp_path)}
    contracts = overlay().contracts.model_copy(
        update={"registry_root_env": "REGISTRY_ROOT"}
    )
    with_superuser = superuser_overlay().model_copy(update={"contracts": contracts})
    without = overlay().model_copy(update={"contracts": contracts})
    assert isinstance(
        runtime_broker_topic_delete.build_handler(with_superuser, environ)._admin,
        RpkSuperuserTopicAdmin,
    )
    assert isinstance(
        runtime_broker_topic_delete.build_handler(without, environ)._admin,
        KafkaBrokerTopicAdmin,
    )


# -- errors: the runner is never called ----------------------------------------------------------


@pytest.mark.parametrize("lane", ["prod", "staging", "judge"])
def test_lane_other_than_dev_refuses_at_the_overlay_load(lane: str) -> None:
    with pytest.raises(ValidationError):
        ModelSuperuserAccess.model_validate({**ACCESS, "lane": lane})


def test_lane_differing_from_the_bus_lane_refuses_at_the_overlay_load() -> None:
    with pytest.raises(ValidationError):
        ModelTopicDeleteOverlay.model_validate(
            {
                **overlay().model_dump(),
                "bus_lane": "other",
                "superuser": ACCESS,
            }
        )


def test_lane_refusals_of_a_hand_built_overlay() -> None:
    runner = FakeRunner()
    prod = overlay().model_copy(
        update={
            "superuser": ModelSuperuserAccess.model_construct(
                **{**ACCESS, "lane": "prod"}
            )
        }
    )
    with pytest.raises(BrokerAdminError, match="superuser_lane_not_dev:prod"):
        RpkSuperuserTopicAdmin(prod, ENVIRON, runner=runner)
    with pytest.raises(BrokerAdminError, match="superuser_not_declared"):
        RpkSuperuserTopicAdmin(overlay(), ENVIRON, runner=runner)
    mismatch = superuser_overlay().model_copy(update={"bus_lane": "other"})
    with pytest.raises(BrokerAdminError, match="superuser_lane_mismatch:dev:other"):
        RpkSuperuserTopicAdmin(mismatch, ENVIRON, runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "ssh_target_unset:TEST_SSH_TARGET"),
        ({"TEST_SSH_TARGET": "-oProxyCommand=x"}, "ssh_target_invalid"),
        ({"TEST_SSH_TARGET": "a b"}, "ssh_target_invalid"),
    ],
)
def test_ssh_target_refusals(environ: dict[str, str], message: str) -> None:
    runner = FakeRunner()
    with pytest.raises(BrokerAdminError, match=message):
        RpkSuperuserTopicAdmin(superuser_overlay(), environ, runner=runner)
    assert runner.calls == []


def test_topic_name_refusals() -> None:
    runner = FakeRunner()
    for call in (
        lambda: admin(runner).read_evidence(["fx;rm -rf"]),
        lambda: admin(runner).delete(["fx;rm -rf"]),
    ):
        with pytest.raises(BrokerAdminError, match="topic_name_invalid:fx;rm -rf"):
            call()
    with pytest.raises(BrokerAdminError, match="delete_names_empty"):
        admin(runner).delete([])
    assert runner.calls == []


def test_credential_absent_on_the_host_refuses_without_leaking() -> None:
    with pytest.raises(BrokerAdminError, match="broker_credential_unresolved"):
        admin(CredentialFailingRunner()).read_evidence(["fx-empty"])

    def boom(_target: str, _script: str, *, timeout: float) -> str:
        raise RuntimeError("boom secret-looking-text")

    with pytest.raises(BrokerAdminError) as caught:
        admin(boom).delete(["fx-empty"])
    assert str(caught.value) == "remote_failed:RuntimeError"


def test_unparsable_output_refuses() -> None:
    with pytest.raises(BrokerAdminError, match="rpk_output_unparsable:list"):
        admin(
            lambda _target, _script, *, timeout: "nothing useful" if timeout else ""
        ).read_evidence(["fx-empty"])


# -- chain events on the in-memory bus -----------------------------------------------------------

TOPICS = load_topics()


async def _publish(runner: Any, payload: bytes) -> list[tuple[str, dict[str, Any]]]:
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()
    delete_handler = handler(runner)

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, delete_handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Any:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic == TOPICS["broker_topic_delete_run_completed"]:
                done.set()

        return collect

    await bus.subscribe(
        TOPICS["broker_topic_delete_requested"], on_message=on_command, group_id="cmd"
    )
    for role in ("reported", "refused", "run_completed", "run_failed"):
        topic = TOPICS[f"broker_topic_delete_{role}"]
        await bus.subscribe(topic, on_message=collector(topic), group_id=f"c-{role}")
    await bus.start()
    await bus.publish(TOPICS["broker_topic_delete_requested"], key=None, value=payload)
    await asyncio.wait_for(done.wait(), timeout=5)
    await bus.close()
    return seen


def test_golden_chain_event_through_the_superuser() -> None:
    seen = asyncio.run(
        _publish(FakeRunner(), command("fx-empty").model_dump_json().encode())
    )
    assert [topic for topic, _ in seen] == [
        TOPICS["broker_topic_delete_reported"],
        TOPICS["broker_topic_delete_run_completed"],
    ]
    assert seen[0][1]["status"] == "enforced"
    assert seen[0][1]["entries"][0]["status"] == "deleted"
    assert seen[1][1]["failed"] == 0


def test_error_chain_event_when_the_credential_is_absent() -> None:
    seen = asyncio.run(
        _publish(
            CredentialFailingRunner(), command("fx-empty").model_dump_json().encode()
        )
    )
    assert [topic for topic, _ in seen] == [
        TOPICS["broker_topic_delete_refused"],
        TOPICS["broker_topic_delete_run_completed"],
    ]
    assert seen[0][1]["refusal_reasons"] == [
        "broker_unreadable:broker_credential_unresolved"
    ]
    assert seen[1][1]["failed"] == 1


# -- failing controls ------------------------------------------------------------------------------


def test_control_a_refused_topic_would_reach_the_delete_script_if_asked() -> None:
    """The check that fx-group is absent from the delete script (chain test) can fail."""
    runner = FakeRunner()
    admin(runner).delete(["fx-group"])
    assert "fx-group" in runner.calls[0][1].split("topic delete")[1]


def test_control_golden_goes_red_on_a_named_break() -> None:
    """With the broker's delete table gone the outcome is unconfirmed, not deleted."""
    outcome = admin(FakeRunner(delete_out="")).delete(["fx-empty"])
    assert outcome == {"fx-empty": "delete_unconfirmed"}
    assert outcome != {"fx-empty": ""}
