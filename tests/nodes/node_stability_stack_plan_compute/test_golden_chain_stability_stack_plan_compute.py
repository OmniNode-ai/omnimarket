# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain, refusal chain, failing control and overlay load of node_stability_stack_plan_compute (OMN-23968).

The golden chain plans an install with the exact ordered steps and is
byte-equal across two calls. Each refusal kind is built by changing one
field of the golden overlay (or the request), and the failing control
shows the same mutated stack plans once the colliding reserved lane is
swapped for a clean one. The shipped example overlay loads through the
overlay reader.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_stability_stack_plan_compute.handlers.handler_stack_plan import (
    HandlerStackPlan,
)
from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    EnumStackRefusalKind,
    EnumStackStepKind,
    ModelStackPlanOverlay,
    ModelStackPlanRequest,
)
from omnimarket.nodes.node_stability_stack_plan_compute.overlay import load_overlay

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_EXAMPLE = (
    _REPO
    / "src/omnimarket/nodes/node_stability_stack_plan_compute/overlay.example.yaml"
)


def _golden_overlay() -> dict[str, Any]:
    """The synthetic golden overlay: one stack, two clean reserved lanes."""
    return {
        "tag_pattern": r"dev-merge-[0-9]{8}-[0-9a-f]+",
        "stacks": [
            {
                "name": "omni-stack-a",
                "host": "host-a.lan",
                "compose_project": "omni_stack_a",
                "source_dir_env": "OMNI_STACK_A_CHECKOUT",
                "compose_files": [
                    "docker-compose.yaml",
                    "docker-compose.stability.yaml",
                ],
                "name_rewrites": [["omni-dev-", "omni-stab-"]],
                "env": [
                    ["OMNI_IMAGE_TAG", "dev-merge-20260101-abc1234"],
                    ["COMPOSE_PROJECT_NAME", "omni_stack_a"],
                ],
                "ports": [["WEB_PORT", 18080], ["MQTT_PORT", 18883]],
                "container_names": ["omni-stab-web", "omni-stab-broker"],
                "volumes": ["omni-stab-data"],
                "release_tag": "dev-merge-20260101-abc1234",
                "tiers": [
                    {"name": "core", "services": ["web"]},
                    {"name": "events", "services": ["broker"]},
                ],
                "health_reads": [
                    {
                        "container": "omni-stab-web",
                        "port_key": "WEB_PORT",
                        "path": "/healthz",
                        "expect_status": 200,
                    }
                ],
                "event_roundtrip": {
                    "broker_container": "omni-stab-broker",
                    "topic": "stab-a-events",
                    "message": "roundtrip",
                },
                "remove_volumes_on_down": True,
            }
        ],
        "reserved": [
            {
                "name": "lane-edge",
                "host": "host-b.lan",
                "compose_project": "edge_stack",
                "container_names": ["edge-proxy"],
                "volumes": ["edge-cache"],
                "ports": [18080],
            },
            {
                "name": "lane-ci",
                "host": "host-a.lan",
                "compose_project": "ci_stack",
                "container_names": ["ci-runner"],
                "volumes": ["ci-cache"],
                "ports": [18443],
            },
        ],
    }


def _lane(overlay: dict[str, Any], name: str) -> dict[str, Any]:
    for lane in overlay["reserved"]:
        if lane["name"] == name:
            return lane
    raise AssertionError(name)


#: One-field mutations of the golden overlay that raise each collision.
_MUTATE = {
    EnumStackRefusalKind.PROJECT_COLLISION: lambda d: _lane(d, "lane-edge").__setitem__(
        "compose_project", "omni_stack_a"
    ),
    EnumStackRefusalKind.CONTAINER_COLLISION: lambda d: _lane(d, "lane-ci")[
        "container_names"
    ].append("omni-stab-web"),
    EnumStackRefusalKind.VOLUME_COLLISION: lambda d: _lane(d, "lane-edge")[
        "volumes"
    ].append("omni-stab-data"),
    EnumStackRefusalKind.PORT_COLLISION: lambda d: _lane(d, "lane-ci").__setitem__(
        "ports", [18443, 18080]
    ),
}
#: subject and against each refusal names.
_NAMED = {
    EnumStackRefusalKind.UNKNOWN_STACK: ("no-such-stack", ""),
    EnumStackRefusalKind.BAD_RELEASE_TAG: (
        "v1.2.3",
        _golden_overlay()["tag_pattern"],
    ),
    EnumStackRefusalKind.PROJECT_COLLISION: ("omni_stack_a", "lane-edge"),
    EnumStackRefusalKind.CONTAINER_COLLISION: ("omni-stab-web", "lane-ci"),
    EnumStackRefusalKind.VOLUME_COLLISION: ("omni-stab-data", "lane-edge"),
    EnumStackRefusalKind.PORT_COLLISION: ("18080", "lane-ci"),
}
#: which reserved lane carries each collision (the failing control swaps it).
_COLLIDING_LANE = {
    EnumStackRefusalKind.PROJECT_COLLISION: "lane-edge",
    EnumStackRefusalKind.CONTAINER_COLLISION: "lane-ci",
    EnumStackRefusalKind.VOLUME_COLLISION: "lane-edge",
    EnumStackRefusalKind.PORT_COLLISION: "lane-ci",
}


def _handler(mutate: Any = None, drop_lane: str | None = None) -> HandlerStackPlan:
    overlay_dict = _golden_overlay()
    if mutate is not None:
        mutate(overlay_dict)
    if drop_lane is not None:
        overlay_dict["reserved"] = [
            lane for lane in overlay_dict["reserved"] if lane["name"] != drop_lane
        ]
        overlay_dict["reserved"].append(
            {
                "name": "lane-spare",
                "host": "host-c.lan",
                "compose_project": "spare_stack",
                "container_names": ["spare-box"],
                "volumes": ["spare-vol"],
                "ports": [19000],
            }
        )
    return HandlerStackPlan(ModelStackPlanOverlay.model_validate(overlay_dict))


def _request(
    stack: str = "omni-stack-a", action: str = "install"
) -> ModelStackPlanRequest:
    return ModelStackPlanRequest(stack=stack, action=action)


def test_golden_install_chain_plans_the_ordered_steps_and_is_byte_equal() -> None:
    plan = _handler().handle(_request())
    assert (plan.status, plan.stack, plan.host, plan.compose_project) == (
        "planned",
        "omni-stack-a",
        "host-a.lan",
        "omni_stack_a",
    )
    assert plan.refusals == ()
    assert [(s.index, s.kind, s.tier, s.target) for s in plan.steps] == [
        (0, EnumStackStepKind.RENDER, "", ""),
        (1, EnumStackStepKind.SNAPSHOT, "", ""),
        (2, EnumStackStepKind.UP, "core", "web"),
        (3, EnumStackStepKind.UP, "events", "broker"),
        (4, EnumStackStepKind.READ_SERVICES, "", ""),
        (5, EnumStackStepKind.READ_HEALTH, "", "omni-stab-web"),
        (6, EnumStackStepKind.EVENT_ROUNDTRIP, "", "omni-stab-broker"),
    ]
    assert [(s.index, s.kind) for s in plan.rollback_steps] == [
        (0, EnumStackStepKind.DOWN),
        (1, EnumStackStepKind.READ_GONE),
    ]
    assert len(plan.render_digest) == 64
    int(plan.render_digest, 16)
    again = _handler().handle(_request())
    assert again.model_dump_json().encode() == plan.model_dump_json().encode()


def test_golden_remove_chain_is_down_then_read_gone() -> None:
    plan = _handler().handle(_request(action="remove"))
    assert plan.status == "planned"
    assert [(s.index, s.kind) for s in plan.steps] == [
        (0, EnumStackStepKind.DOWN),
        (1, EnumStackStepKind.READ_GONE),
    ]
    assert plan.rollback_steps == ()
    assert plan.refusals == ()


@pytest.mark.parametrize("kind", list(EnumStackRefusalKind))
def test_error_chain_each_refusal_kind_is_named_with_no_steps(
    kind: EnumStackRefusalKind,
) -> None:
    subject, against = _NAMED[kind]
    if kind is EnumStackRefusalKind.UNKNOWN_STACK:
        plan = _handler().handle(_request(stack=subject))
        assert plan.render_digest == ""
    elif kind is EnumStackRefusalKind.BAD_RELEASE_TAG:
        plan = _handler(
            lambda d: d["stacks"][0].__setitem__("release_tag", subject)
        ).handle(_request())
        assert plan.render_digest
    else:
        plan = _handler(_MUTATE[kind]).handle(_request())
        assert plan.render_digest
    assert plan.status == "refused"
    assert plan.host == (
        "" if kind is EnumStackRefusalKind.UNKNOWN_STACK else "host-a.lan"
    )
    assert plan.steps == ()
    assert plan.rollback_steps == ()
    assert [(r.kind, r.subject, r.against) for r in plan.refusals] == [
        (kind, subject, against)
    ]


@pytest.mark.parametrize("kind", list(_MUTATE))
def test_failing_control_the_same_stack_plans_without_the_collision(
    kind: EnumStackRefusalKind,
) -> None:
    plan = _handler(_MUTATE[kind], drop_lane=_COLLIDING_LANE[kind]).handle(_request())
    assert plan.status == "planned"
    assert plan.refusals == ()
    assert len(plan.steps) == 7


def test_overlay_example_loads_through_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "overlays"
    node_dir = root / "node_stability_stack_plan_compute"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        _EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    overlay = load_overlay()
    assert isinstance(overlay, ModelStackPlanOverlay)
    assert overlay.stacks[0].name
    assert overlay.reserved[0].container_names
